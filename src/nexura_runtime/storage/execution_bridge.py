# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Read-only projections of legacy durable-graph records in the ExecutionStore.

The journal is authoritative. ``durable_graph`` runs were written by the removed
``DurableExecutionEngine`` (P35 A6 S12); repository reads of those ids are projections of
the journal, and repository updates are refused.
"""

from __future__ import annotations

import asyncio
import copy
import importlib
from collections.abc import Awaitable, Callable
from typing import Any

from nexura_agents.contracts.execution import (
    ExecutionId,
)
from nexura_agents.errors import StorageError
from nexura_agents.execution_store import (
    ExecutionSnapshot,
    ExecutionStore,
    SQLiteExecutionStore,
)

from nexura_runtime.storage.models import RunEventRecord, RunRecord


async def execution_store_for(repository: Any) -> ExecutionStore:
    """Return one initialized journal per repository, sharing its database backend."""
    lock = getattr(repository, "_execution_store_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        repository._execution_store_lock = lock
    async with lock:
        store: ExecutionStore | None = getattr(repository, "_execution_store", None)
        if store is None:
            if hasattr(repository, "db_path"):
                store = SQLiteExecutionStore(repository.db_path)
            elif hasattr(repository, "_get_pool"):
                module = importlib.import_module("nexura_postgres.execution_store")
                store = module.PostgresExecutionStore(pool=await repository._get_pool())
            else:
                raise TypeError("Pass an ExecutionStore for a custom repository")
            await store.init_db()
            repository._execution_store = store
        return store


def project(snapshot: ExecutionSnapshot) -> RunRecord:
    """Return a detached legacy view; legacy token zero precedes its first worker claim."""
    run = snapshot.run
    state = copy.deepcopy(run.state)
    state.update(
        revision=run.revision,
        fencing_token=run.fencing_token - 1,
        lease_expires_at=run.lease_expires_at or 0.0,
    )
    if run.lease_owner is not None:
        state["worker_id"] = run.lease_owner
    status = state.get("status", run.status)
    if status not in ("pending", "cancelled"):
        status = run.status
    state["status"] = status
    return RunRecord(
        workspace_id=run.execution_id.workspace_id,
        run_id=run.execution_id.run_id,
        status=status,
        revision=run.revision,
        state=state,
        input_data=run.origin.get("legacy_input", {}),
        metadata=run.origin.get("legacy_metadata", {}),
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


class ExecutionRepositoryBridge:
    """Read-only ``RunRecord`` view of ``durable_graph`` runs in the execution journal.

    Those runs were written by the legacy ``DurableExecutionEngine``, removed by P35 A6 S12.
    A database that still holds them keeps them readable through ``get_run`` and
    ``get_run_events`` (their effects stay listable and resolvable through the journal and
    the effects API); ``update_run`` refuses, because nothing can continue them any more.
    """

    def __init__(self, store: ExecutionStore) -> None:
        self.store = store

    async def get_run(self, workspace_id: str, run_id: str) -> RunRecord | None:
        """Read a durable graph, returning None for absent or unrelated journal entries."""
        try:
            snapshot = await self.store.get(ExecutionId(workspace_id, run_id))
        except KeyError:
            return None
        return project(snapshot) if snapshot.run.kind == "durable_graph" else None

    async def update_run(
        self,
        workspace_id: str,
        run_id: str,
        expected_revision: int,
        status: str,
        state_update: dict[str, Any] | None = None,
        events: list[dict[str, Any]] | None = None,
    ) -> RunRecord:
        """Refuse: a ``durable_graph`` run belongs to the removed engine and is read-only."""
        raise StorageError(
            f"Run '{run_id}' was started by the removed DurableExecutionEngine (P35 A6 S12) "
            "and is read-only. Read it with get_run/get_run_events or the execution journal "
            "(ExecutionStore.get); settle an unknown effect with ExecutionStore.resolve_effect "
            "or POST /api/v1/runs/{run_id}/effects/{effect_id}/resolve. New durable workflows "
            "run on nexura_runtime.workflow.WorkflowEngine (recover, resolve_effect)."
        )

    async def get_run_events(self, workspace_id: str, run_id: str) -> list[RunEventRecord]:
        """Project journal events with the legacy sequence field name."""
        snapshot = await self.store.get(ExecutionId(workspace_id, run_id))
        return [
            RunEventRecord(
                workspace_id=workspace_id,
                run_id=run_id,
                seq=event.sequence,
                event_type=event.event_type,
                payload=event.payload,
                timestamp=event.timestamp,
            )
            for event in snapshot.events
        ]


async def bridge_for(repository: Any) -> ExecutionRepositoryBridge:
    """Build a light adapter over the repository's cached shared store."""
    return ExecutionRepositoryBridge(await execution_store_for(repository))


async def journal_bridge(
    repository: Any,
    workspace_id: str,
    run_id: str,
    probe: Callable[[], Awaitable[bool]],
) -> ExecutionRepositoryBridge | None:
    """Return a bridge when this run is a journaled durable graph, else None.

    ``probe`` cheaply checks the repository's own database for a journal row, so plain
    repository use neither opens nor migrates the journal. Once this process opened the
    journal for the repository (an engine used it), the journal itself is consulted.
    """
    if getattr(repository, "_execution_store", None) is None and not await probe():
        return None
    bridge = await bridge_for(repository)
    return bridge if await bridge.get_run(workspace_id, run_id) is not None else None
