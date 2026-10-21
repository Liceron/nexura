# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Whether a Studio run is interrupted, decided from its execution-journal lease (A6 S9).

A workflow run the service starts is journaled (``WorkflowEngine`` on a store with an
execution journal: SQLite next to the Studio database, or Postgres with
``workflow_state_url``). The process executing it holds the run lease and renews it every
``lease_ttl / 3``. The run row in ``nexura_studio_runs`` records the journal's workspace
(``journal_workspace``) so the service can find it.

A journaled ``running`` run is shown ``interrupted`` only when its journal says nobody runs
it any more:

- the journal lists it as recoverable (``ExecutionStore.list_recoverable``: running with a
  free or expired lease, judged by the store's clock), or
- its lease owner is a process on this host that has exited (the same shortcut
  ``WorkflowEngine.recover`` takes).

A run whose lease is live (another ``serve`` or ``worker`` executes it, on this host or
another) is never shown interrupted; an ``interrupted`` row whose lease is live again (a
recover in progress elsewhere) is shown ``running``. When the journal cannot tell (no
journal row yet, or the journal already moved on while the run row still says
``running``) the B46 owner-process rule applies, as it does for legacy rows without a
journal, which ``StudioStore`` still sweeps when it opens.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from nexura_agents.contracts.execution import ExecutionId

from nexura_runtime._workflow_journal import journal_capable
from nexura_runtime.studio.store import owner_process_is_gone, this_process_owner

logger = logging.getLogger(__name__)

#: Error stored on a run marked interrupted because its lease expired or its owner exited.
LEASE_EXPIRED_ERROR = (
    "Run was interrupted: the process executing it stopped (its run lease expired). "
    "Continue it with POST /api/v1/runs/{run_id}/recover."
)
LEASE_EXPIRED_REASON = "The run lease expired or its owner process exited; the run is recoverable."

#: Error and event reason for a run this process stopped executing when it shut down.
SHUTDOWN_ERROR = "Run was interrupted by server shutdown."
SHUTDOWN_REASON = "Run interrupted by server shutdown; background task cancelled."

LIVE = "live"
RECOVERABLE = "recoverable"

#: Default seconds of a workflow run lease in the service (renewed every third of it).
DEFAULT_WORKFLOW_LEASE_TTL = 30.0


def workflow_lease_ttl(app_state: Any) -> float:
    """Seconds of the run lease the service's engines take (``create_api_app`` option)."""
    ttl = getattr(app_state, "workflow_lease_ttl", None)
    return float(ttl) if ttl is not None else DEFAULT_WORKFLOW_LEASE_TTL


async def run_journal(app_state: Any) -> Any:
    """The execution journal next to the service's workflow checkpoints, or None."""
    checkpoints: Any = getattr(app_state.store, "checkpoint_store", None)
    if not journal_capable(checkpoints):
        return None
    return await checkpoints.open_execution_store()


def journal_workspace(app_state: Any, workspace_id: str | None) -> str | None:
    """The journal workspace to record for a new run (None when runs are not journaled)."""
    checkpoints: Any = getattr(app_state.store, "checkpoint_store", None)
    return (workspace_id or "default") if journal_capable(checkpoints) else None


async def journal_decides(app_state: Any, run_id: str) -> bool:
    """True when ``run_id`` is a journaled run (its lease decides whether it is interrupted)."""
    rows = await app_state.store.list_journaled_runs(("running",), run_ids=[run_id])
    return bool(rows)


async def run_liveness(
    journal: Any, rows: Sequence[dict[str, Any]], *, stopped_here: bool = False
) -> dict[str, str]:
    """``{run_id: "live" | "recoverable"}`` for journaled run rows (see the module doc).

    Each row needs ``run_id``, ``journal_workspace`` and ``owner_process``. ``stopped_here``:
    this process has just stopped executing these runs (shutdown), so a row it owns counts
    as stopped when the journal cannot tell (cancelled before its first journal write).
    """
    from nexura_agents.durable import lease_owner_is_dead

    recoverable: set[tuple[str, str]] = set()
    for workspace in sorted({str(row["journal_workspace"]) for row in rows}):
        for run in await journal.list_recoverable(workspace):
            recoverable.add((run.execution_id.workspace_id, run.execution_id.run_id))
    verdicts: dict[str, str] = {}
    for row in rows:
        run_id, workspace = str(row["run_id"]), str(row["journal_workspace"])
        if (workspace, run_id) in recoverable:
            verdicts[run_id] = RECOVERABLE
            continue
        try:
            snapshot = await journal.get(ExecutionId(workspace, run_id))
        except KeyError:
            snapshot = None
        if snapshot is not None and snapshot.run.status in ("running", "interrupted"):
            owner = snapshot.run.lease_owner
            dead = owner is not None and lease_owner_is_dead(owner)
            verdicts[run_id] = RECOVERABLE if dead else LIVE
            continue
        # No journal row (killed before its first journal write) or the journal moved on
        # (paused/finished) while the run row says running: the process that would write
        # the row decides, as for legacy rows (B46).
        owner = row.get("owner_process")
        gone = owner_process_is_gone(owner) or (stopped_here and owner == this_process_owner())
        verdicts[run_id] = RECOVERABLE if gone else LIVE
    return verdicts


async def reconcile_run_statuses(
    app_state: Any,
    *,
    workspace_id: str | None = None,
    run_ids: Sequence[str] | None = None,
    error: str = LEASE_EXPIRED_ERROR,
    reason: str = LEASE_EXPIRED_REASON,
    stopped_here: bool = False,
) -> set[str]:
    """Mark journaled ``running`` runs nobody executes any more ``interrupted``.

    Writes are compare-and-set (``running`` -> ``interrupted`` plus a ``RUN_INTERRUPTED``
    event), so a run that finished meanwhile keeps its result. Returns the ids of
    ``interrupted`` rows whose lease is live again: callers show those as ``running``.
    Runs without a journal (legacy rows) are left to the B46 open-time sweep.
    ``stopped_here``: see :func:`run_liveness` (the shutdown paths pass it with the run ids
    whose tasks this process cancelled).
    """
    store = app_state.store
    rows = await store.list_journaled_runs(workspace_id=workspace_id, run_ids=run_ids)
    if not rows:
        return set()
    journal = await run_journal(app_state)
    if journal is None:
        return set()
    verdicts = await run_liveness(journal, rows, stopped_here=stopped_here)
    live_again: set[str] = set()
    for row in rows:
        run_id = str(row["run_id"])
        verdict = verdicts.get(run_id)
        if row["status"] == "running" and verdict == RECOVERABLE:
            await store.mark_interrupted(
                run_id, error=error.replace("{run_id}", run_id), reason=reason
            )
        elif row["status"] == "interrupted" and verdict == LIVE:
            live_again.add(run_id)
    return live_again


async def reconcile_quietly(app_state: Any, **kwargs: Any) -> set[str]:
    """:func:`reconcile_run_statuses`, logging instead of raising (startup, shutdown, reads)."""
    try:
        return await reconcile_run_statuses(app_state, **kwargs)
    except Exception:
        logger.warning("Could not reconcile run statuses with the run journal.", exc_info=True)
        return set()


def show_live(run: dict[str, Any], live_again: set[str]) -> dict[str, Any]:
    """The run view: an ``interrupted`` run whose lease is live again is ``running``."""
    if run.get("status") == "interrupted" and run.get("run_id") in live_again:
        return {**run, "status": "running", "error": None}
    return run
