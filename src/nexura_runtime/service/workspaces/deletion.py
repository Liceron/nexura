# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Workspace deletion (P46): retire the id, end access at once, then purge the content.

``DELETE /api/v1/workspaces/{workspace_id}`` (``nexura_runtime.service.api.workspaces``) runs
three phases:

1. **Retire.** ``WorkspaceRepository.retire_workspace`` writes a tombstone (``deleted_at``; the
   row stays, so the id can never be created again by any path) and a deletion record
   (``nexura_workspace_deletions``, status ``purging``) in one transaction. A deleted workspace
   is hidden from every listing, ``get_workspace`` no longer finds it, and requests into it are
   refused.
2. **End access** (:func:`end_workspace_access`, in the request): the hooks of the edition
   (``WorkspaceDeletionHooks.end_access``: the hosted edition removes members, the personal
   workspace row and the organisation link, revokes API keys and publications), then the
   workspace's webhooks, delivery tokens, schedules and trigger jobs are deleted and every live
   run (ownership ``run`` and runs journaled under the workspace) is cancelled through the one
   cancel path.
3. **Purge** (:func:`purge_workspace`, idempotent and resumable): every store's rows of the
   workspace and its blobs; the resource-ownership rows go last, so an interrupted pass finds
   the same runs, workflows and sessions again. :func:`run_workspace_purge` runs one pass and
   counts it on the deletion record; a pass that starts at least the workflow lease TTL after
   the request (``final_purge_delay``) and succeeds marks the record ``done``: by then a run
   still finishing on another instance has stopped writing. The first pass runs in the
   background right after the request; :func:`sweep_workspace_deletions` (the trigger tick
   and the in-process trigger poller call it) repeats passes until every record is ``done``.

Kept on purpose: the usage ledger (a billing record) and the audit trails of the hosted
edition (membership, API key, publication and organisation audit). Caches of this process
are dropped (BYOK models, integration tools of the module registry); another instance keeps
the workspace's integration tools in its registry until it restarts (they cannot be used:
nobody can authenticate into the workspace).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from nexura_runtime.service.workspaces.models import WorkspaceDeletion

logger = logging.getLogger("nexura.service.workspaces")

#: Workspaces that can never be deleted.
PROTECTED_WORKSPACES = frozenset({"default"})
#: ``cancel_reason`` of runs cancelled because their workspace was deleted.
WORKSPACE_DELETED = "workspace_deleted"
#: Seconds between two sweeps of the in-process trigger poller.
SWEEP_INTERVAL_SECONDS = 60.0

AccessHook = Callable[[Any, str, str], Awaitable[dict[str, Any] | None]]
PurgeHook = Callable[[Any, str], Awaitable[dict[str, int] | None]]
AuditHook = Callable[[Any, str, str, WorkspaceDeletion], Awaitable[None]]


@dataclass
class WorkspaceDeletionHooks:
    """What an edition adds to workspace deletion (``app.state.workspace_deletion_hooks``).

    * ``protected(state, workspace_id) -> bool``: True refuses the deletion (409).
    * ``operators(request) -> bool``: True when the caller may delete any workspace (besides
      ``caller_is_operator``).
    * ``end_access(state, workspace_id, actor)``: awaited in the request, in order, after the
      tombstone and before triggers and runs are stopped; may return a summary dict.
    * ``purge(state, workspace_id)``: awaited by every purge pass, before blobs and the
      ownership rows; may return counts. Must be idempotent.
    * ``audit(state, workspace_id, actor, deletion)``: awaited once after access ended.
    """

    protected: list[Callable[[Any, str], bool]] = field(default_factory=list)
    operators: list[Callable[[Any], bool]] = field(default_factory=list)
    end_access: list[AccessHook] = field(default_factory=list)
    purge: list[PurgeHook] = field(default_factory=list)
    audit: list[AuditHook] = field(default_factory=list)


def deletion_hooks(state: Any) -> WorkspaceDeletionHooks:
    """The app's hooks (created on first use)."""
    hooks: WorkspaceDeletionHooks | None = getattr(state, "workspace_deletion_hooks", None)
    if hooks is None:
        hooks = WorkspaceDeletionHooks()
        state.workspace_deletion_hooks = hooks
    return hooks


def protection_reason(state: Any, workspace_id: str) -> str | None:
    """Why ``workspace_id`` may not be deleted, or ``None`` when it may."""
    if workspace_id in PROTECTED_WORKSPACES:
        return "The default workspace cannot be deleted."
    if getattr(state, "server_model_policy", None) is not None:
        from nexura_runtime.service.api.model_access import server_models_allowed

        if server_models_allowed(state, workspace_id):
            return "An operator workspace (it uses the server's model keys) cannot be deleted."
    for check in deletion_hooks(state).protected:
        try:
            if check(state, workspace_id):
                return "This workspace is protected by the service configuration."
        except Exception:  # fail closed: a broken check protects
            logger.exception("workspace protection check failed for %s", workspace_id)
            return "This workspace is protected by the service configuration."
    return None


def final_purge_delay(state: Any) -> float:
    """Seconds after the request before a purge pass may mark the deletion done: the
    workflow lease TTL (a run still finishing on another instance stops writing by then)."""
    configured = getattr(state, "workspace_purge_final_delay", None)
    if configured is not None:
        return float(configured)
    from nexura_runtime.service.run_liveness import workflow_lease_ttl

    return workflow_lease_ttl(state)


async def _call(store: Any, method: str, *args: Any, **kwargs: Any) -> Any:
    """``store.method(*args)`` without blocking the event loop (``None`` when it lacks it)."""
    fn = getattr(store, method, None) if store is not None else None
    if fn is None:
        return None
    if asyncio.iscoroutinefunction(fn):
        return await fn(*args, **kwargs)
    return await asyncio.to_thread(fn, *args, **kwargs)


# --------------------------------------------------------------------------- end access


async def _end_triggers(state: Any, workspace_id: str) -> dict[str, int]:
    """Delete the workspace's webhooks (and their delivery tokens), schedules and jobs."""
    store = getattr(state, "trigger_store", None)
    if store is None:
        return {}
    webhooks = await _call(store, "list_webhook_configs", workspace_id) or []
    tokens = getattr(state, "delivery_tokens", None)
    if tokens is not None:
        for config in webhooks:
            await _call(tokens, "delete", config.endpoint_id)
    schedules = await _call(store, "list_schedules", workspace_id) or []
    if hasattr(store, "purge_workspace"):
        jobs = await _call(store, "purge_workspace", workspace_id)
    else:  # a store without the purge: remove what its API can
        for config in webhooks:
            await _call(store, "delete_webhook_config", config.endpoint_id)
        for schedule in schedules:
            await _call(store, "delete_schedule", schedule.schedule_id)
        jobs = 0
    return {"webhooks": len(webhooks), "schedules": len(schedules), "trigger_jobs": int(jobs or 0)}


async def workspace_run_ids(state: Any, workspace_id: str) -> list[str]:
    """Every run of the workspace: ownership ``run`` rows and runs journaled under it."""
    repo = state.workspace_repo
    ids = set(await asyncio.to_thread(repo.list_workspace_resource_ids, "run", workspace_id))
    store = getattr(state, "store", None)
    journaled = await _call(store, "journal_run_ids", workspace_id)
    ids.update(journaled or ())
    return sorted(ids)


async def cancel_workspace_runs(
    state: Any, workspace_id: str, actor: str | None = None
) -> list[str]:
    """Cancel every live run of the workspace through the shared cancel path."""
    from nexura_runtime.service.api.run_revocation import (
        REVOCABLE_STATUSES,
        _lock,
        cancel_message,
    )
    from nexura_runtime.service.api.runs import cancel_run_record

    store = state.store
    cancelled: list[str] = []
    async with _lock(state):
        for run_id in await workspace_run_ids(state, workspace_id):
            if await store.get_run_status(run_id) not in REVOCABLE_STATUSES:
                continue
            updated = await cancel_run_record(
                state,
                run_id,
                reason=cancel_message(WORKSPACE_DELETED),
                cancel_reason=WORKSPACE_DELETED,
                actor=actor,
            )
            if updated is not None:
                cancelled.append(run_id)
    return cancelled


async def end_workspace_access(state: Any, workspace_id: str, actor: str) -> dict[str, Any]:
    """Phase 2 (see the module docstring); returns a summary."""
    summary: dict[str, Any] = {}
    for hook in deletion_hooks(state).end_access:
        result = await hook(state, workspace_id, actor)
        if result:
            summary.update(result)
    summary.update(await _end_triggers(state, workspace_id))
    summary["cancelled_run_ids"] = await cancel_workspace_runs(state, workspace_id, actor)
    return summary


# --------------------------------------------------------------------------- purge


async def _delete_blobs(state: Any, keys: list[str]) -> int:
    blob_store = getattr(state, "blob_store", None)
    if blob_store is None or not keys:
        return 0

    def run() -> int:
        removed = 0
        for key in keys:
            with contextlib.suppress(Exception):
                removed += bool(blob_store.delete(key))
        return removed

    return await asyncio.to_thread(run)


async def _purge_blob_prefix(state: Any, workspace_id: str) -> int:
    blob_store = getattr(state, "blob_store", None)
    if blob_store is None or workspace_id in PROTECTED_WORKSPACES:
        return 0
    from nexura_runtime.blobs.base import workspace_segment

    prefix = f"{workspace_segment(workspace_id)}/"

    def run() -> int:
        removed = 0
        for info in list(blob_store.list(prefix)):
            if blob_store.delete(info.key):
                removed += 1
        return removed

    removed = await asyncio.to_thread(run)
    payloads = getattr(state, "blob_payloads", None)
    if payloads is not None and hasattr(payloads, "clear_cache"):
        payloads.clear_cache()
    return removed


async def _purge_knowledge(state: Any, workspace_id: str) -> int:
    index = getattr(state, "hybrid_knowledge_index", None)
    if index is None:
        stores = getattr(state, "service_stores", None)
        vectors = getattr(state, "vector_store", None)
        if stores is None or (vectors is None and not getattr(stores, "database_url", None)):
            return 0
        index = await asyncio.to_thread(
            stores.knowledge_index, vectors, getattr(state, "knowledge_embedder", None)
        )
    return int(await _call(index, "purge_workspace", workspace_id) or 0)


async def _purge_credentials(state: Any, workspace_id: str) -> int:
    removed = 0
    credentials = getattr(state, "credential_store", None)
    if credentials is not None:
        for meta in await asyncio.to_thread(credentials.list_credentials, workspace_id):
            removed += bool(
                await asyncio.to_thread(credentials.delete, workspace_id, meta.credential_id)
            )
    removed += int(
        await _call(getattr(state, "secret_store", None), "purge_workspace", workspace_id) or 0
    )
    cache = getattr(state, "credential_model_cache", None)
    if cache is not None and hasattr(cache, "forget_workspace"):
        cache.forget_workspace(workspace_id)
    return removed


async def _purge_integrations(state: Any, workspace_id: str) -> int:
    store = getattr(state, "integration_registrations", None)
    if store is None:
        return 0
    rows = await asyncio.to_thread(store.list)
    module_ids = [mod_id for ws_id, mod_id, *_ in rows if ws_id == workspace_id]
    if not module_ids:
        return 0
    deleted = await asyncio.to_thread(store.delete, workspace_id, module_ids)
    from nexura_runtime.service.api.integrations import unregister_integration_tools

    unregister_integration_tools(state, workspace_id, module_ids)
    return len(deleted)


async def _purge_journal(state: Any, workspace_id: str) -> int:
    checkpoints = getattr(getattr(state, "store", None), "checkpoint_store", None)
    journals: list[Any] = []
    for journal in (
        getattr(state, "execution_store", None),
        getattr(checkpoints, "execution_store", None),
    ):
        if journal is not None and all(journal is not j for j in journals):
            journals.append(journal)
    removed = 0
    for journal in journals:
        removed += int(await _call(journal, "purge_workspace", workspace_id) or 0)
    return removed


async def purge_workspace(state: Any, workspace_id: str) -> dict[str, int]:
    """Phase 3: delete the workspace's content from every store (see the module docstring).

    Every step runs even when an earlier one failed; the resource-ownership rows are deleted
    only when every step succeeded. Raises the first error after the other steps ran.
    """
    if workspace_id in PROTECTED_WORKSPACES:
        raise ValueError(f"Workspace '{workspace_id}' is protected")
    repo = state.workspace_repo
    counts: dict[str, int] = {}
    errors: list[BaseException] = []

    async def step(name: str, work: Callable[[], Awaitable[Any]]) -> None:
        try:
            counts[name] = int(await work() or 0)
        except Exception as exc:
            logger.warning("workspace %s purge step %s failed: %s", workspace_id, name, exc)
            errors.append(exc)

    store = getattr(state, "store", None)
    run_ids = await workspace_run_ids(state, workspace_id)
    workflow_ids = await asyncio.to_thread(
        repo.list_workspace_resource_ids, "workflow", workspace_id
    )
    session_ids = await asyncio.to_thread(repo.list_workspace_resource_ids, "session", workspace_id)

    async def runs() -> int:
        # Blobs referenced by the run events first (some are filed under another workspace,
        # "default" for runs without a journal workspace), then the rows.
        keys = await _call(store, "run_blob_keys", run_ids) or []
        await _delete_blobs(state, list(keys))
        return int(await _call(store, "purge_runs", run_ids) or 0)

    async def checkpoints() -> int:
        cps = getattr(state, "workflow_state_store", None) or getattr(
            store, "checkpoint_store", None
        )
        return int(await _call(cps, "purge_runs", run_ids, workspace_id=workspace_id) or 0)

    async def workflows() -> int:
        removed = 0
        for workflow_id in workflow_ids:
            removed += bool(await store.delete_workflow(workflow_id))
        return removed

    async def sessions() -> int:
        return int(await _call(getattr(state, "session_store", None), "purge", session_ids) or 0)

    def by_store(attr: str) -> Callable[[], Awaitable[Any]]:
        return lambda: _call(getattr(state, attr, None), "purge_workspace", workspace_id)

    await step("runs", runs)
    await step("checkpoints", checkpoints)
    await step("journal", lambda: _purge_journal(state, workspace_id))
    await step("workflows", workflows)
    await step("sessions", sessions)
    await step("agents", by_store("agent_store"))
    await step("skills", by_store("skills_store"))
    await step("evaluations", by_store("eval_store"))
    await step("studio_evaluations", by_store("studio_eval_store"))
    await step("knowledge", lambda: _purge_knowledge(state, workspace_id))
    await step("integrations", lambda: _purge_integrations(state, workspace_id))
    await step("credentials", lambda: _purge_credentials(state, workspace_id))
    await step("triggers", by_store("trigger_store"))
    await step("channel_outbox", by_store("channel_store"))
    await step("repro_bundles", by_store("repro_capture"))
    for hook in deletion_hooks(state).purge:
        try:
            counts.update(await hook(state, workspace_id) or {})
        except Exception as exc:
            logger.warning("workspace %s purge hook failed: %s", workspace_id, exc)
            errors.append(exc)
    await step("blobs", lambda: _purge_blob_prefix(state, workspace_id))
    if errors:
        raise errors[0]
    counts["ownership"] = int(
        await asyncio.to_thread(repo.delete_workspace_ownership, workspace_id)
    )
    return counts


def _purge_lock(state: Any, workspace_id: str) -> asyncio.Lock:
    locks: dict[str, asyncio.Lock] | None = getattr(state, "workspace_purge_locks", None)
    if locks is None:
        locks = {}
        state.workspace_purge_locks = locks
    return locks.setdefault(workspace_id, asyncio.Lock())


async def run_workspace_purge(state: Any, workspace_id: str) -> WorkspaceDeletion | None:
    """One purge pass of a deleted workspace, counted on its deletion record (see the module
    docstring). ``None`` when the workspace has no deletion record; a ``done`` record is
    returned unchanged."""
    repo = state.workspace_repo
    async with _purge_lock(state, workspace_id):
        deletion = await asyncio.to_thread(repo.get_workspace_deletion, workspace_id)
        if deletion is None or deletion.status == "done":
            return deletion
        started = time.time()
        error: str | None = None
        counts: dict[str, int] = {}
        try:
            counts = await purge_workspace(state, workspace_id)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:500]
        final = error is None and started >= deletion.requested_at + final_purge_delay(state)
        updated = await asyncio.to_thread(
            repo.record_purge_attempt, workspace_id, done=final, error=error
        )
    if error is not None:
        logger.warning("workspace %s purge pass failed (will retry): %s", workspace_id, error)
    elif final:
        logger.info("workspace %s purge done: %s", workspace_id, counts)
    return updated


async def sweep_workspace_deletions(state: Any) -> dict[str, int]:
    """Run a purge pass for every deletion still ``purging``; counts of ``done``/``pending``."""
    repo = getattr(state, "workspace_repo", None)
    if repo is None or not hasattr(repo, "list_workspace_deletions"):
        return {}
    pending = await asyncio.to_thread(repo.list_workspace_deletions, "purging")
    done = 0
    for deletion in pending:
        try:
            updated = await run_workspace_purge(state, deletion.workspace_id)
        except Exception:
            logger.exception("workspace %s purge pass failed", deletion.workspace_id)
            continue
        done += updated is not None and updated.status == "done"
    if not pending:
        return {}
    return {"workspace_deletions_done": done, "workspace_deletions_pending": len(pending) - done}


async def maybe_sweep_workspace_deletions(state: Any) -> dict[str, int]:
    """:func:`sweep_workspace_deletions` at most every ``SWEEP_INTERVAL_SECONDS`` per app (the
    in-process trigger poller calls it on every iteration)."""
    now = time.monotonic()
    interval = float(getattr(state, "workspace_sweep_interval", SWEEP_INTERVAL_SECONDS))
    last = getattr(state, "workspace_sweep_last", None)
    if last is not None and now - last < interval:
        return {}
    state.workspace_sweep_last = now
    return await sweep_workspace_deletions(state)


def start_background_purge(state: Any, workspace_id: str) -> asyncio.Task[Any]:
    """Run a purge pass now in the background (kept in ``state.workspace_purge_tasks``)."""
    tasks: set[asyncio.Task[Any]] | None = getattr(state, "workspace_purge_tasks", None)
    if tasks is None:
        tasks = set()
        state.workspace_purge_tasks = tasks
    task = asyncio.get_running_loop().create_task(run_workspace_purge(state, workspace_id))
    tasks.add(task)

    def done(finished: asyncio.Task[Any]) -> None:
        tasks.discard(finished)
        if not finished.cancelled() and finished.exception() is not None:
            logger.error("workspace purge task failed", exc_info=finished.exception())

    task.add_done_callback(done)
    return task


__all__ = [
    "PROTECTED_WORKSPACES",
    "SWEEP_INTERVAL_SECONDS",
    "WORKSPACE_DELETED",
    "WorkspaceDeletionHooks",
    "cancel_workspace_runs",
    "deletion_hooks",
    "end_workspace_access",
    "final_purge_delay",
    "maybe_sweep_workspace_deletions",
    "protection_reason",
    "purge_workspace",
    "run_workspace_purge",
    "start_background_purge",
    "sweep_workspace_deletions",
    "workspace_run_ids",
]
