# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Background worker that turns durable trigger jobs into workflow runs."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import re
import socket
import uuid
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import HTTPException

from nexura_runtime.service.triggers.manager import TriggerManager
from nexura_runtime.service.triggers.models import DurableTriggerJob
from nexura_runtime.service.triggers.store import DEFAULT_LEASE_SECONDS, DEFAULT_MAX_ATTEMPTS
from nexura_runtime.workflow import WorkflowSpec, _LeaseBusyError

logger = logging.getLogger("nexura_runtime.triggers.worker")

#: How a reclaimed job is retried. ``"continue"`` (default, A6 S10): the job's run continues
#: under its run lease. ``"new_run"`` (deprecated, removed in 0.6): a new run starts from the
#: entry node and the old one is marked interrupted.
RetryMode = Literal["continue", "new_run"]
RETRY_MODES: tuple[str, ...] = ("continue", "new_run")

#: Run statuses that end a job's work: the run finished, or waits for a person.
_HANDED_OFF = frozenset({"completed", "pending_approval", "waiting_for_input", "paused"})
_ENDED_BADLY = frozenset({"failed", "rejected", "cancelled"})
_CONTINUABLE = frozenset({"running", "interrupted"})
_ABANDONED = "Worker lease expired; run abandoned."
_MAX_UNFINISHED_RECOVERIES = 5

_HEX_RE = re.compile(r"^[0-9a-f]{12,64}$")
_REV_RE = re.compile(r"^(?:rev(?:ision)?[:=-]?)?(\d+)$")
_UNPINNED = {"", "current", "latest"}


@dataclass
class JobOutcome:
    """Result of processing one claimed trigger job."""

    job_id: str
    run_id: str | None
    status: str  # "completed" | "failed"
    error: str | None = None


def check_release_pin(release_id: str, *, revision: int, spec_hash: str) -> str | None:
    """Return an error message when ``release_id`` no longer matches the stored workflow.

    Accepted pins: ``current`` (no pin), a revision (``rev:3``, ``revision:3`` or ``3``), or the
    workflow's canonical spec hash (full SHA-256 hex, optionally ``sha256:`` prefixed, or a
    unique-looking prefix of at least 12 hex characters). Any other value is refused rather than
    silently ignored, so a mistyped pin can never run an unintended workflow version.
    """
    pin = release_id.strip().lower()
    if pin in _UNPINNED:
        return None
    rev_match = _REV_RE.match(pin)
    if rev_match:
        pinned_rev = int(rev_match.group(1))
        if pinned_rev != revision:
            return (
                f"Pinned release '{release_id}' (revision {pinned_rev}) no longer matches "
                f"workflow revision {revision}."
            )
        return None
    hash_pin = pin.removeprefix("sha256:")
    if _HEX_RE.match(hash_pin):
        if not spec_hash.startswith(hash_pin):
            return (
                f"Pinned release '{release_id}' no longer matches the workflow spec hash "
                f"{spec_hash[:12]}."
            )
        return None
    return (
        f"Unrecognised release pin '{release_id}'; use 'current', a revision "
        "(e.g. 'rev:3') or a spec hash."
    )


def _http_message(exc: HTTPException) -> str:
    detail = exc.detail
    if isinstance(detail, dict):
        code = detail.get("code")
        message = detail.get("message", "")
        return f"{code}: {message}" if code else str(message)
    return str(detail)


class TriggerWorker:
    """Ticks schedules and executes trigger jobs through the shared run path.

    Each claimed job runs as its own asyncio task (at most ``max_concurrent_jobs`` at once), so a
    long run never blocks schedule ticks. A claim carries a queue lease that a heartbeat renews
    every ``lease_seconds / 3`` while the run executes. If the worker dies hard, another worker
    reclaims the expired job (up to ``max_attempts`` claims).

    A job owns one run for its life (Execution Contract v1, A6 S10). A reclaimed job continues
    that run with ``WorkflowEngine.recover``: completed steps are not run again, a step whose
    outcome is unknown follows its effect class (a ``non_idempotent`` one pauses the run
    ``effect_unknown`` for a person), and a run that already finished is never run again (the
    job is completed or failed from it). The job lease only coordinates the queue; the run
    lease in the execution journal decides who executes the run, so the worker waits while
    another live process still holds it. ``retry_mode="new_run"`` keeps the old behaviour
    (deprecated, removed in 0.6): the old run is marked interrupted and a new run starts from
    the entry node.

    ``clock``: a time source for leases (tests). By default each store uses its own clock
    (the database clock for ``PostgresTriggerStore``).
    """

    def __init__(
        self,
        app_state: Any,
        *,
        worker_id: str | None = None,
        poll_interval: float = 1.0,
        max_jobs_per_tick: int = 20,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        max_concurrent_jobs: int = 4,
        shutdown_grace_seconds: float = 20.0,
        clock: Callable[[], float] | None = None,
        retry_mode: RetryMode = "continue",
    ) -> None:
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be a positive finite number")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if max_concurrent_jobs < 1:
            raise ValueError("max_concurrent_jobs must be at least 1")
        if retry_mode not in RETRY_MODES:
            raise ValueError("retry_mode must be 'continue' or 'new_run'")
        if retry_mode == "new_run":
            warnings.warn(
                'TriggerWorker(retry_mode="new_run") is deprecated and will be removed in 0.6: '
                "a reclaimed job then starts a new run from the entry node, repeating effects "
                "that already happened. The default continues the job's run.",
                DeprecationWarning,
                stacklevel=2,
            )
        self.retry_mode: RetryMode = retry_mode
        self.app_state = app_state
        self.worker_id = worker_id or f"worker-{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
        self.poll_interval = poll_interval
        self.max_jobs_per_tick = max_jobs_per_tick
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.max_concurrent_jobs = max_concurrent_jobs
        self.shutdown_grace_seconds = shutdown_grace_seconds
        self._clock = clock
        self._active: dict[str, asyncio.Task[JobOutcome]] = {}
        self._lease_lost: set[str] = set()
        self._reclaimed_backlog: list[DurableTriggerJob] = []
        self._background = False

    def _now(self) -> float | None:
        """The injected clock's time, or None to let the store use its own clock."""
        return self._clock() if self._clock is not None else None

    @property
    def manager(self) -> TriggerManager:
        manager: TriggerManager = self.app_state.trigger_manager
        return manager

    @property
    def active_job_ids(self) -> list[str]:
        return list(self._active)

    async def run_once(self, now: float | None = None) -> list[JobOutcome]:
        """Tick schedules, recover expired leases, then run pending jobs and await them.

        Jobs run concurrently up to ``max_concurrent_jobs``; at most ``max_jobs_per_tick`` jobs
        are started per call. Outcomes are returned in start order. While ``run_forever`` is
        driving the worker, jobs are left running in the background and ``[]`` is returned so a
        long run never delays the next schedule tick.
        """
        await self._housekeeping(now)
        outcomes: list[JobOutcome] = []
        remaining = self.max_jobs_per_tick
        while remaining > 0:
            tasks = self._claim_and_spawn(remaining)
            if not tasks:
                break
            remaining -= len(tasks)
            if self._background:
                return outcomes
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, JobOutcome):
                    outcomes.append(result)
                elif isinstance(result, asyncio.CancelledError):
                    # Job lost its lease (or hit shutdown): its outcome belongs to another worker.
                    continue
                elif isinstance(result, BaseException):
                    raise result
        return outcomes

    async def _housekeeping(self, now: float | None) -> list[DurableTriggerJob]:
        self.manager.tick_schedules(now)
        return await self._reclaim_expired()

    async def _reclaim_expired(self) -> list[DurableTriggerJob]:
        """Take over jobs whose queue lease expired (to continue their runs, or retry them)."""
        store = self.manager.store
        slots = max(0, self.max_concurrent_jobs - len(self._active))
        # Reclaimed retries get no heartbeat until spawned, so never reclaim more than the next
        # claim batch can start immediately (backlog is spawned first).
        budget = min(self.max_jobs_per_tick, slots) - len(self._reclaimed_backlog)
        if budget <= 0:
            return []
        new_run = self.retry_mode == "new_run"
        reclaimed = store.reclaim_expired_jobs(
            self.worker_id,
            lease_seconds=self.lease_seconds,
            max_attempts=self.max_attempts,
            limit=budget,
            now=self._now(),
            exclude_job_ids=set(self._active),
            keep_run=not new_run,
        )
        retries: list[DurableTriggerJob] = []
        for job in reclaimed:
            if job.status == "failed":
                # Out of attempts: the job fails and keeps its run. That run is marked
                # interrupted only once nobody executes it (journaled runs: its run lease).
                dead_run = job.run_id
            elif new_run:
                # Deprecated: the old run moved to previous_run_ids; a new run starts.
                dead_run = job.previous_run_ids[-1] if job.previous_run_ids else None
                retries.append(job)
            else:
                dead_run = None  # the job's run is continued when the job is processed
                retries.append(job)
            logger.warning(
                "Trigger job %s lease expired (attempt %s); %s",
                job.job_id,
                job.attempts,
                "failed"
                if job.status == "failed"
                else ("retrying as a new run" if new_run else "continuing its run"),
            )
            if dead_run:
                await self._interrupt_run(dead_run, _ABANDONED)
        # Retried jobs are already claimed by this worker; start them with the next batch.
        self._reclaimed_backlog.extend(retries)
        return retries

    async def _interrupt_run(self, run_id: str, reason: str) -> None:
        """Mark a run left 'running' by a dead worker as interrupted (other states untouched).

        A journaled run is marked only when its run lease says nobody executes it (A6 S9);
        a legacy run (no journal) is marked at once, as before.
        """
        from nexura_runtime.service.run_liveness import journal_decides, reconcile_quietly

        try:
            store = self.app_state.store
            run = await store.get_run(run_id)
            if run is None or run.get("status") != "running":
                return
            if await journal_decides(self.app_state, run_id):
                await reconcile_quietly(self.app_state, run_ids=[run_id])
                return
            await store.record_run_event(run_id, "RUN_INTERRUPTED", data={"reason": reason})
            await store.update_run(
                run_id=run_id,
                status="interrupted",
                current_node_id=run.get("current_node_id"),
                current_state=run.get("current_state", {}),
                step_count=run.get("step_count", 0),
                error=reason,
            )
        except Exception:
            logger.exception("Could not mark run %s interrupted", run_id)

    def _claim_and_spawn(self, limit: int) -> list[asyncio.Task[JobOutcome]]:
        """Claim up to ``limit`` jobs (bounded by free slots) and start one task per job."""
        store = self.manager.store
        slots = self.max_concurrent_jobs - len(self._active)
        budget = min(limit, slots)
        claimed: list[DurableTriggerJob] = []
        reclaimed: set[str] = set()
        while self._reclaimed_backlog and len(claimed) < budget:
            backlogged = self._reclaimed_backlog.pop(0)
            reclaimed.add(backlogged.job_id)
            claimed.append(backlogged)
        if len(claimed) < budget:
            for pending in store.list_pending_jobs(limit=budget - len(claimed)):
                job = store.claim_pending_job(
                    pending.job_id,
                    self.worker_id,
                    lease_seconds=self.lease_seconds,
                    now=self._now(),
                )
                if job is None:
                    continue  # another worker won the claim
                claimed.append(job)
        tasks: list[asyncio.Task[JobOutcome]] = []
        for job in claimed:
            task = asyncio.create_task(
                self._run_job(job, reclaimed=job.job_id in reclaimed),
                name=f"trigger-job-{job.job_id}",
            )
            self._active[job.job_id] = task
            task.add_done_callback(self._make_release(job.job_id))
            task.add_done_callback(_log_task_result)
            tasks.append(task)
        return tasks

    def _make_release(self, job_id: str) -> Callable[[asyncio.Task[JobOutcome]], None]:
        def _release(_task: asyncio.Task[JobOutcome]) -> None:
            self._active.pop(job_id, None)

        return _release

    async def _run_job(self, job: DurableTriggerJob, *, reclaimed: bool = False) -> JobOutcome:
        """Run one claimed job while a heartbeat keeps its lease alive."""
        job_task = asyncio.current_task()
        heartbeat = asyncio.create_task(self._heartbeat(job.job_id, job_task))
        try:
            return await self._process(job, reclaimed=reclaimed)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            self._lease_lost.discard(job.job_id)

    async def _heartbeat(self, job_id: str, job_task: asyncio.Task[Any] | None) -> None:
        """Renew the lease every ``lease_seconds / 3``; cancel the run if ownership was lost."""
        interval = self.lease_seconds / 3.0
        while True:
            await asyncio.sleep(interval)
            try:
                owned = self.manager.store.renew_job_lease(
                    job_id, self.worker_id, lease_seconds=self.lease_seconds, now=self._now()
                )
            except Exception:
                logger.exception("Lease renewal for job %s failed", job_id)
                continue  # transient store error: retry before the lease runs out
            if not owned:
                logger.error("Trigger job %s lost its lease; cancelling local run", job_id)
                self._lease_lost.add(job_id)
                if job_task is not None:
                    job_task.cancel()
                return

    async def _process(self, job: DurableTriggerJob, *, reclaimed: bool = False) -> JobOutcome:
        started: list[str] = []
        try:
            # A reclaimed job continues the run it owns; a newly claimed one starts its run.
            if reclaimed and job.run_id and self.retry_mode == "continue":
                run_id, run_error = await self._continue(job, started)
            else:
                run_id, run_error = await self._start(job, started)
        except asyncio.CancelledError:
            if job.job_id in self._lease_lost:
                # Another worker owns the job now; it must not be failed from here.
                raise
            # Clean cancel (e.g. shutdown timeout): the run is marked interrupted by its owner, so
            # fail the job rather than leave it claimed forever, then propagate the cancel.
            self.manager.store.complete_job(
                job.job_id,
                error_message="worker cancelled",
                run_id=started[0] if started else None,
                expected_worker=self.worker_id,
            )
            raise
        except _JobRefused as exc:
            return self._fail(job, str(exc), run_id=started[0] if started else None)
        except HTTPException as exc:
            return self._fail(job, _http_message(exc), run_id=started[0] if started else None)
        except Exception as exc:
            logger.exception("Trigger job %s failed", job.job_id)
            return self._fail(
                job, f"{type(exc).__name__}: {exc}", run_id=started[0] if started else None
            )
        if run_error is not None:
            return self._fail(job, run_error, run_id=run_id)
        self.manager.store.complete_job(job.job_id, run_id=run_id, expected_worker=self.worker_id)
        return JobOutcome(job_id=job.job_id, run_id=run_id, status="completed")

    def _fail(self, job: DurableTriggerJob, error: str, run_id: str | None = None) -> JobOutcome:
        self.manager.store.complete_job(
            job.job_id, error_message=error, run_id=run_id, expected_worker=self.worker_id
        )
        return JobOutcome(job_id=job.job_id, run_id=run_id, status="failed", error=error)

    def _record_run(self, job: DurableTriggerJob, started: list[str], run_id: str) -> None:
        started.append(run_id)
        self.manager.store.set_job_run(job.job_id, self.worker_id, run_id)

    async def _start(self, job: DurableTriggerJob, started: list[str]) -> tuple[str, str | None]:
        from nexura_runtime.service.api.runs import start_workflow_run

        state = self.app_state
        store = state.store

        # Workspace ownership mirrors POST /runs: a workflow must belong to the job's workspace
        # (legacy unowned workflows belong to the default workspace).
        owner = state.workspace_repo.get_resource_workspace("workflow", job.workflow_id)
        if owner is None and job.workspace_id == "default":
            owner = "default"
        wf = await store.get_workflow(job.workflow_id)
        if wf is None:
            raise _JobRefused(f"Workflow '{job.workflow_id}' not found.")
        if owner != job.workspace_id:
            raise _JobRefused(
                f"Workflow '{job.workflow_id}' does not belong to workspace '{job.workspace_id}'."
            )

        release_error = check_release_pin(
            job.release_id,
            revision=int(wf["revision"]),
            spec_hash=str(wf["spec_hash"]),
        )
        if release_error is not None:
            raise _JobRefused(release_error)

        try:
            initial_state = json.loads(json.dumps(job.payload, allow_nan=False))
        except (TypeError, ValueError) as exc:
            raise _JobRefused(f"Job payload is not valid JSON: {exc}") from exc
        if not isinstance(initial_state, dict):
            raise _JobRefused("Job payload must be a JSON object.")

        spec = WorkflowSpec.from_dict(wf["spec"])
        snapshot = await start_workflow_run(
            state,
            spec=spec,
            workflow_id=job.workflow_id,
            workspace_id=job.workspace_id,
            principal_id=f"trigger:{job.trigger_type}:{job.trigger_source_id}",
            initial_state=initial_state,
            on_run_id=lambda rid: self._record_run(job, started, rid),
            trigger={
                "trigger_type": job.trigger_type,
                "trigger_source_id": job.trigger_source_id,
                "job_id": job.job_id,
                "release_id": job.release_id,
                "attempt": job.attempts,
            },
        )
        run_id = str(snapshot["run_id"])
        if snapshot.get("status") == "failed":
            err = snapshot.get("error") or "run failed"
            return run_id, f"Run {run_id} failed: {err}"
        return run_id, None

    async def _continue(self, job: DurableTriggerJob, started: list[str]) -> tuple[str, str | None]:
        """Finish a reclaimed job from the run it already owns (A6 S10).

        A finished run is never run again: the job is completed (or failed) from it. A run
        waiting for a person (approval, input, or an ``effect_unknown`` pause) counts as
        started, as on the first attempt. A ``running`` / ``interrupted`` run is continued
        with ``WorkflowEngine.recover`` once its run lease is free; while another live
        process holds that lease, this worker waits (its job heartbeat keeps the job).
        A run without an execution journal cannot be continued safely: it is marked
        interrupted and a new run is started, as before A6 S10.
        """
        from nexura_runtime.service.api.runs import recover_workflow_run

        run_id = str(job.run_id)
        started.append(run_id)
        state = self.app_state
        run = await state.store.get_run(run_id)
        if run is None:
            raise _JobRefused(f"Run '{run_id}' of job '{job.job_id}' was not found.")
        unfinished_recoveries = 0
        while True:
            status = str(run.get("status"))
            if status in _HANDED_OFF:
                return run_id, None
            if status in _ENDED_BADLY:
                err = run.get("error") or f"run {status}"
                return run_id, f"Run {run_id} {status}: {err}"
            if status not in _CONTINUABLE:
                raise _JobRefused(f"Run '{run_id}' has status '{status}' and cannot continue.")
            verdict = await self._run_lease(run_id, job.workspace_id)
            if verdict is None:
                logger.warning(
                    "Trigger job %s: run %s has no execution journal; starting a new run.",
                    job.job_id,
                    run_id,
                )
                await self._interrupt_run(run_id, _ABANDONED)
                started.clear()
                return await self._start(job, started)
            if verdict == "recoverable":
                try:
                    run = await recover_workflow_run(
                        state,
                        run_id=run_id,
                        workspace_id=job.workspace_id,
                        event_data={
                            "job_id": job.job_id,
                            "attempt": job.attempts,
                            "trigger_type": job.trigger_type,
                            "trigger_source_id": job.trigger_source_id,
                        },
                    )
                except _LeaseBusyError:
                    pass  # another worker took the run lease first: wait for it
                else:
                    if str(run.get("status")) not in _CONTINUABLE:
                        continue  # finished, failed or waiting for a person
                    # The run lease was lost to another process meanwhile: wait for it.
                    unfinished_recoveries += 1
                    if unfinished_recoveries >= _MAX_UNFINISHED_RECOVERIES:
                        raise _JobRefused(
                            f"Run '{run_id}' did not finish after "
                            f"{unfinished_recoveries} recover attempts."
                        )
            # The run lease is live: another process executes the run. Wait for it.
            await asyncio.sleep(self._lease_wait)
            fresh = await state.store.get_run(run_id)
            if fresh is None:
                raise _JobRefused(f"Run '{run_id}' of job '{job.job_id}' was not found.")
            run = fresh

    @property
    def _lease_wait(self) -> float:
        return max(0.05, min(self.poll_interval, self.lease_seconds / 3.0))

    async def _run_lease(self, run_id: str, workspace_id: str) -> str | None:
        """``"live"`` / ``"recoverable"`` from the run's lease; None without a journal."""
        from nexura_agents.contracts.execution import ExecutionId

        from nexura_runtime.service.run_liveness import run_journal, run_liveness

        state = self.app_state
        journal = await run_journal(state)
        if journal is None:
            return None
        rows = await state.store.list_journaled_runs(("running", "interrupted"), run_ids=[run_id])
        workspace = str(rows[0]["journal_workspace"]) if rows else workspace_id
        try:
            snapshot = await journal.get(ExecutionId(workspace, run_id))
        except KeyError:
            return None
        if snapshot.run.status not in ("running", "interrupted"):
            # Paused or finished in the journal while the run row lags: nobody holds the run
            # lease, and recover() only projects the stored result (nothing runs again).
            return "recoverable"
        if not rows:
            rows = [{"run_id": run_id, "journal_workspace": workspace, "owner_process": None}]
        verdict: str = (await run_liveness(journal, rows)).get(run_id, "live")
        return verdict

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Process jobs until ``stop`` is set; per-iteration errors are logged, not fatal.

        Jobs run in background tasks so schedule ticks and lease recovery continue while a long
        run executes. On stop, running jobs get ``shutdown_grace_seconds`` to finish and are then
        cancelled (the cancel path marks the run interrupted and fails the job).
        """
        self._background = True
        try:
            while not stop.is_set():
                try:
                    await self.run_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Trigger worker %s iteration failed", self.worker_id)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self.poll_interval)
        finally:
            self._background = False
            await self._drain()

    async def _drain(self) -> None:
        """Wait up to the grace period for active jobs, then cancel what is left."""
        tasks = list(self._active.values())
        if not tasks:
            return
        _done, pending = await asyncio.wait(tasks, timeout=self.shutdown_grace_seconds)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


def _log_task_result(task: asyncio.Task[JobOutcome]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Trigger job task failed", exc_info=exc)


class _JobRefused(Exception):
    """Job cannot run (missing workflow, wrong workspace, release mismatch, bad payload)."""
