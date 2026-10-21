# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""The execution journal of one top-level workflow run (Execution Contract v1, A6 S6).

Internal to ``WorkflowEngine``. A workflow store that offers ``open_execution_store()``
(``MemoryWorkflowStore`` and ``SQLiteWorkflowStore``) gets, next to its checkpoints:

- a lease on the run from its first write (``kind="workflow"``, ``definition_digest`` =
  the spec's ``canonical_hash()``), renewed by a heartbeat every ``ttl / 3``, with a
  fencing token that only goes up; a stale owner's journal writes raise
  ``LeaseLostError``;
- one ``StepIntent`` plus a ``pending`` effect record per non-pure node visit, committed
  just before the node dispatches (with the approval it runs under, recorded as consumed
  in the same fenced transaction);
- the node's ``StepOutcome`` committed with the new cursor before the checkpoint moves;
- the run status (``paused`` with ``pause_reason``, ``completed``, ``failed``).

The checkpoint stays the state the graph reads; its saves are fenced by a revision. The
journal records the lease and what was dispatched.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from nexura_agents.contracts.execution import (
    EffectRecord,
    EffectResolution,
    EffectStatus,
    ExecutionId,
    LeaseLostError,
    StepIntent,
    StepKey,
    StepOutcome,
)

if TYPE_CHECKING:
    from nexura_agents.execution_store import (
        ExecutionLease,
        ExecutionSnapshot,
        ExecutionStep,
        ExecutionStore,
    )

TERMINAL = ("completed", "failed")


def journal_capable(store: Any) -> bool:
    """A workflow store the engine can keep an execution journal for."""
    return store is not None and callable(getattr(store, "open_execution_store", None))


class WorkflowJournal:
    """Lease, heartbeat, intents and outcomes of one run; one instance per engine call."""

    def __init__(
        self, store: ExecutionStore, workspace_id: str, run_id: str, digest: str, ttl: float
    ) -> None:
        self.store = store
        self.run_id = run_id
        self.eid = ExecutionId(workspace_id or "default", run_id)
        self.digest = digest
        self.ttl = ttl
        self.owner: str | None = None
        self.lease: ExecutionLease | None = None
        self.lost: LeaseLostError | None = None
        self.status = "running"
        self.state: dict[str, Any] = {}
        self.steps: dict[str, ExecutionStep] = {}
        self.effects: dict[str, EffectRecord] = {}  # by step key (one effect per step)
        #: Revision of the workflow checkpoint this run last read or wrote (fences saves).
        self.cp_revision: int | None = None
        self._heartbeat: asyncio.Task[None] | None = None
        self._closed = False

    @classmethod
    async def open(
        cls, checkpoints: Any, workspace_id: str, run_id: str, digest: str, ttl: float
    ) -> WorkflowJournal:
        store = await checkpoints.open_execution_store()
        return cls(store, workspace_id, run_id, digest, ttl)

    # ------------------------------------------------------------------ taking the run

    @property
    def held(self) -> bool:
        return self.lease is not None and self.lost is None

    @property
    def fencing_token(self) -> int | None:
        return self.lease.fencing_token if self.lease is not None else None

    async def snapshot(self) -> ExecutionSnapshot | None:
        try:
            return await self.store.get(self.eid)
        except KeyError:
            return None

    def _load(self, snapshot: ExecutionSnapshot) -> None:
        self.status = snapshot.run.status
        self.state = dict(snapshot.run.state)
        self.steps = dict(snapshot.steps)
        self.effects = {str(e.intent.step_key): e for e in snapshot.effects.values()}

    @staticmethod
    def _initial_state() -> dict[str, Any]:
        return {"pause_reason": None, "pending_effect": None, "consumed_approvals": []}

    async def create(self, owner: str, origin: dict[str, Any] | None = None) -> None:
        """Journal a new run with its lease already held (raises if it exists)."""
        run = await self.store.create(
            self.eid,
            owner=owner,
            definition_digest=self.digest,
            state=self._initial_state(),
            ttl=self.ttl,
            kind="workflow",
            origin=origin,
        )
        self.status = run.status
        self.state = dict(run.state)
        self.owner, self.lease = owner, run.lease

    async def take(self, owner: str) -> bool:
        """Claim a stored run (resume, recover); False while another live owner holds it.

        A run whose lease owner is a process on this host that has exited is released on
        its behalf first, so a restart after a crash takes over at once. A run that was
        never journaled (started before the journal, or its first write was lost) is
        journaled now. A finished journal run is loaded without a lease.
        """
        from nexura_agents.durable import lease_owner_is_dead
        from nexura_agents.execution_store import ExecutionConflictError, ExecutionLease

        snapshot = await self.snapshot()
        if snapshot is None:
            try:
                await self.create(owner, origin={"source": "sdk", "journaled_late": True})
                return True
            except ExecutionConflictError:
                snapshot = await self.snapshot()
                if snapshot is None:
                    raise
        if snapshot.run.status in TERMINAL:
            self._load(snapshot)
            return True
        run = snapshot.run
        dead = run.lease_owner
        if dead is not None and run.lease_expires_at is not None and lease_owner_is_dead(dead):
            handle = ExecutionLease(self.eid, dead, run.fencing_token, run.lease_expires_at)
            with contextlib.suppress(Exception):
                await self.store.release(handle)
        try:
            lease = await self.store.claim(self.eid, owner, self.ttl, definition_digest=self.digest)
        except ExecutionConflictError:
            # Finished between the read and the claim.
            snapshot = await self.snapshot()
            assert snapshot is not None
            self._load(snapshot)
            return True
        if lease is None:
            return False
        snapshot = await self.snapshot()
        assert snapshot is not None
        self._load(snapshot)
        self.owner, self.lease = owner, lease
        return True

    # ------------------------------------------------------------------ lease upkeep

    def check(self) -> None:
        """Raise ``LeaseLostError`` once the lease is known to be lost (``assert_lease``)."""
        if self.lost is not None:
            raise LeaseLostError(
                f"Workflow run '{self.run_id}': the run lease was lost (another worker took "
                "the run over); this worker stops without writing."
            )

    async def renew(self) -> None:
        if self.lease is None:
            return
        self.check()
        try:
            self.lease = await self.store.renew(self.lease, self.ttl)
        except LeaseLostError as exc:
            self.lost = exc
            raise

    async def _beat(self) -> None:
        while True:
            await asyncio.sleep(self.ttl / 3)
            try:
                await self.renew()
            except LeaseLostError:
                return
            except Exception:  # noqa: S112 - a transient store error: try again next beat
                continue

    def start_heartbeat(self) -> None:
        if self._heartbeat is None and self.held:
            self._heartbeat = asyncio.get_running_loop().create_task(self._beat())

    async def stop_heartbeat(self) -> None:
        task, self._heartbeat = self._heartbeat, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task

    async def close(self) -> None:
        """Stop the heartbeat and give the lease back (best effort, idempotent)."""
        if self._closed:
            return
        self._closed = True
        await self.stop_heartbeat()
        lease, self.lease = self.lease, None
        if lease is not None:
            with contextlib.suppress(Exception):
                await self.store.release(lease)

    # ------------------------------------------------------------------ journal writes

    async def _commit(
        self,
        *,
        status: str = "running",
        cursor: str | None = None,
        steps: Sequence[StepIntent | StepOutcome] = (),
        effects: Sequence[EffectRecord] = (),
        events: Sequence[dict[str, Any]] = (),
    ) -> None:
        self.check()
        if self.lease is None:
            raise LeaseLostError(f"Workflow run '{self.run_id}': no run lease is held.")
        try:
            await self.store.commit(
                self.lease,
                state=self.state,
                cursor=cursor,
                status=status,
                steps=steps,
                effects=effects,
                events=events,
            )
        except LeaseLostError as exc:
            self.lost = exc
            raise
        from nexura_agents.execution_store import ExecutionStep

        for step in steps:
            key = str(step.step_key)
            old = self.steps.get(key, ExecutionStep())
            if isinstance(step, StepIntent):
                self.steps[key] = dataclasses.replace(old, intent=step)
            else:
                self.steps[key] = dataclasses.replace(old, outcome=step)
        for effect in effects:
            self.effects[str(effect.intent.step_key)] = effect
        self.status = status
        if status in ("paused", *TERMINAL):
            self.lease = None  # the store released it with this commit
            with contextlib.suppress(Exception):
                await self.stop_heartbeat()

    async def ensure_running(self, cursor: str | None, event: dict[str, Any]) -> None:
        """A resumed or recovered run is ``running`` again before it dispatches anything."""
        self.state["pause_reason"] = None
        self.state["pending_effect"] = None
        await self._commit(cursor=cursor, events=[event])

    def latest(self, node_id: str, step: int) -> tuple[int, StepKey, ExecutionStep | None]:
        """The newest attempt of a node visit: (attempt, step key, journal step or None)."""
        attempt, key = 1, StepKey.workflow(node_id, step)
        if str(key) not in self.steps:
            return attempt, key, None
        while str(StepKey.workflow(node_id, step, attempt + 1)) in self.steps:
            attempt += 1
        key = StepKey.workflow(node_id, step, attempt)
        return attempt, key, self.steps[str(key)]

    def dispatch_key(self, node_id: str, step: int) -> StepKey:
        """The step key for the next dispatch of a node visit.

        The same key (and so the same idempotency key) while the newest attempt is still
        open (a re-run of a pure, idempotent or reconcilable node, or a node that asked
        for input); a new attempt once a person resolved it as ``not_applied``.
        """
        attempt, key, step_record = self.latest(node_id, step)
        if step_record is None or step_record.intent is None:
            return key
        effect = self.effects.get(str(key))
        if step_record.outcome is None and (
            effect is None or effect.status == EffectStatus.PENDING
        ):
            return key
        return StepKey.workflow(node_id, step, attempt + 1)

    def _consume(self, approval_id: str | None, key: StepKey) -> None:
        if approval_id is None:
            return
        consumed = list(self.state.get("consumed_approvals") or [])
        if not any(item.get("approval_id") == approval_id for item in consumed):
            consumed.append({"approval_id": approval_id, "step_key": str(key)})
        self.state["consumed_approvals"] = consumed

    async def record_intent(self, intent: StepIntent, node_id: str) -> None:
        """Claim for dispatch: intent, ``pending`` effect and approval consumed, one commit."""
        self.check()
        step = self.steps.get(str(intent.step_key))
        if step is not None and step.intent is not None:
            # A re-run of the same attempt keeps its intent and key; a fresh approval for
            # it (reconcile said NotApplied) is still recorded as consumed.
            if intent.approval_id is not None and intent.approval_id != step.intent.approval_id:
                self._consume(intent.approval_id, intent.step_key)
                await self._commit(
                    cursor=node_id,
                    events=[
                        {
                            "type": "approval_consumed",
                            "step_key": str(intent.step_key),
                            "approval_id": intent.approval_id,
                        }
                    ],
                )
            return
        self._consume(intent.approval_id, intent.step_key)
        await self._commit(
            cursor=node_id,
            steps=[intent],
            effects=[EffectRecord(self.eid, intent)],
            events=[
                {
                    "type": "node_dispatched",
                    "step_key": str(intent.step_key),
                    "node_id": node_id,
                    "effect_class": intent.effect_class.value,
                    "approval_id": intent.approval_id,
                }
            ],
        )

    async def record_outcome(
        self,
        key: StepKey,
        output: dict[str, Any],
        *,
        cursor: str | None,
        terminal: bool,
        approval_id: str | None = None,
    ) -> None:
        """The node's outcome, effect confirmed and the new cursor, in one fenced commit."""
        steps: list[StepIntent | StepOutcome] = []
        effects: list[EffectRecord] = []
        step = self.steps.get(str(key))
        if step is None or step.outcome is None:
            steps.append(StepOutcome(key, "completed", output))
        effect = self.effects.get(str(key))
        if effect is not None and effect.status == EffectStatus.PENDING:
            effects.append(
                dataclasses.replace(effect, status=EffectStatus.CONFIRMED, result=output)
            )
        self._consume(approval_id, key)  # a pure gated node writes no intent
        self.state["pause_reason"] = None
        self.state["pending_effect"] = None
        await self._commit(
            status="completed" if terminal else "running",
            cursor=cursor,
            steps=steps,
            effects=effects,
            events=[{"type": "node_completed", "step_key": str(key), "next": cursor}],
        )

    def _indeterminate(self, keys: Sequence[StepKey | str]) -> list[EffectRecord]:
        changed = []
        for key in keys:
            effect = self.effects.get(str(key))
            if effect is not None and effect.status == EffectStatus.PENDING:
                changed.append(dataclasses.replace(effect, status=EffectStatus.INDETERMINATE))
        return changed

    async def pause(
        self, pause_reason: str, cursor: str | None, keys: Sequence[StepKey | str] = ()
    ) -> None:
        """``paused`` with ``pause_reason``; in-doubt effects become indeterminate."""
        effects = self._indeterminate(keys)
        self.state["pause_reason"] = pause_reason
        self.state["pending_effect"] = (
            self.effects[str(keys[0])].effect_id if keys and str(keys[0]) in self.effects else None
        )
        await self._commit(
            status="paused",
            cursor=cursor,
            effects=effects,
            events=[{"type": "run_paused", "pause_reason": pause_reason}],
        )

    async def finish(self, status: str, cursor: str | None, reason: str | None = None) -> None:
        """Terminal status; a failed run's dispatched-but-unfinished effects are indeterminate."""
        effects: list[EffectRecord] = []
        if status == "failed":
            open_keys = [k for k, s in self.steps.items() if s.intent and s.outcome is None]
            effects = self._indeterminate(open_keys)
        self.state["pause_reason"] = None
        self.state["pending_effect"] = None
        event: dict[str, Any] = {"type": f"run_{status}"}
        if reason:
            event["reason"] = reason
        await self._commit(status=status, cursor=cursor, effects=effects, events=[event])

    async def resolve(
        self, effect_id: str, resolution: EffectResolution, *, actor: str, result: Any
    ) -> EffectRecord:
        """Record a person's resolution of an in-doubt effect (once)."""
        self.check()
        if self.lease is None:
            raise LeaseLostError(f"Workflow run '{self.run_id}': no run lease is held.")
        try:
            record = await self.store.resolve_effect(
                self.lease, effect_id, resolution, actor=actor, result=result
            )
        except LeaseLostError as exc:
            self.lost = exc
            raise
        self.effects[str(record.intent.step_key)] = record
        if self.status == "paused":
            self.lease = None  # resolving a paused run's effect releases the lease
        return record
