# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Compatibility projections of durable-graph records onto ExecutionStore.

The new journal is authoritative. Legacy repository reads/updates for these ids
are projections, not a second independently committed runs-table copy.
"""

from __future__ import annotations

import asyncio
import copy
import importlib
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

from nexura_agents.contracts.canonical import canonical_digest
from nexura_agents.contracts.execution import (
    EffectClass,
    EffectRecord,
    EffectStatus,
    ExecutionId,
    StepIntent,
    StepKey,
    StepOutcome,
    default_idempotency_key,
    effect_class_from_policy,
)
from nexura_agents.errors import SessionConflictError, StorageError
from nexura_agents.execution_store import (
    ExecutionConflictError,
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


def visit_key(state: dict[str, Any]) -> StepKey:
    """Identify a graph visit, including an explicitly authorized new dispatch attempt."""
    return StepKey.workflow(
        str(state["current_node_id"]),
        len(state.get("completed_nodes", [])),
        attempt=int(state.get("dispatch_attempt", 1)),
    )


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
    """Legacy RunRecord/CAS adapter over the shared execution journal."""

    def __init__(self, store: ExecutionStore) -> None:
        self.store = store

    async def get_run(self, workspace_id: str, run_id: str) -> RunRecord | None:
        """Read a durable graph, returning None for absent or unrelated journal entries."""
        try:
            snapshot = await self.store.get(ExecutionId(workspace_id, run_id))
        except KeyError:
            return None
        return project(snapshot) if snapshot.run.kind == "durable_graph" else None

    async def create_run(
        self,
        workspace_id: str,
        run_id: str,
        initial_state: dict[str, Any],
        metadata: dict[str, Any],
    ) -> RunRecord:
        """Initialize with a bootstrap lease, then release for the first worker to claim."""
        run = await self.store.create(
            ExecutionId(workspace_id, run_id),
            owner="initialize-" + uuid.uuid4().hex,
            definition_digest=metadata["workflow_digest"],
            state=initial_state,
            kind="durable_graph",
            origin={"source": "sdk", "legacy_metadata": metadata, "legacy_input": initial_state},
        )
        await self.store.release(run.lease)
        record = await self.get_run(workspace_id, run_id)
        assert record is not None
        return record

    async def update_run(
        self,
        workspace_id: str,
        run_id: str,
        expected_revision: int,
        status: str,
        state_update: dict[str, Any] | None = None,
        events: list[dict[str, Any]] | None = None,
        *,
        outcome: Any = None,
        has_outcome: bool = False,
    ) -> RunRecord:
        """Apply legacy CAS while journaling dispatch intent and outcome in the same commit."""
        identity = ExecutionId(workspace_id, run_id)
        snapshot = await self.store.get(identity)
        run = snapshot.run
        if run.revision != expected_revision:
            raise SessionConflictError(run_id, expected_revision, run.revision)
        if run.status in ("completed", "failed"):
            # The journal never reopens a finished run (cancelled is stored as failed).
            raise StorageError(f"Run '{run_id}' has finished and cannot be updated.")
        state = copy.deepcopy(run.state)
        state.update(state_update or {})
        state["status"] = status
        owner = state.get("worker_id") or run.lease_owner or "operator-" + uuid.uuid4().hex
        if run.lease_owner != owner or run.lease_expires_at is None:
            lease = await self.store.claim(
                identity, owner, 30, definition_digest=run.definition_digest
            )
            if lease is None:
                raise SessionConflictError(run_id, expected_revision, run.revision)
            expected_revision += 1
        else:
            lease = run.lease
        steps: list[StepIntent | StepOutcome] = []
        effects: list[EffectRecord] = []
        intent = state.get("pre_dispatch_intent")
        if intent is not None and intent.get("dispatched"):
            key = visit_key(state)
            policy = intent["effect_policy"]
            effect_class = (
                EffectClass(policy)
                if policy in {item.value for item in EffectClass}
                else effect_class_from_policy(policy)
            )
            if effect_class != EffectClass.PURE:
                old_step = snapshot.steps.get(key)
                step_intent = old_step.intent if old_step is not None else None
                if step_intent is None:
                    step_intent = StepIntent(
                        key,
                        canonical_digest(state["state"]),
                        default_idempotency_key(identity, key),
                        effect_class,
                    )
                steps.append(step_intent)
                effect = EffectRecord(identity, step_intent)
                old_effect = snapshot.effects.get(effect.effect_id)
                if old_effect is None:
                    effects.append(effect)
                elif old_effect.status == EffectStatus.PENDING and status == "paused":
                    effects.append(replace(old_effect, status=EffectStatus.INDETERMINATE))
        completed = state.get("completed_nodes", [])
        previous = run.state.get("completed_nodes", [])
        failed = status == "failed" and run.state.get("pre_dispatch_intent") is not None
        if len(completed) > len(previous) or failed:
            key = visit_key(run.state)
            result = (
                outcome
                if has_outcome
                else ({"error": state.get("error")} if failed else state["state"])
            )
            steps.append(StepOutcome(key, "failed" if failed else "completed", result))
            # A returned handler confirms its effect. A raised or timed-out handler leaves
            # it unknown: the remote system may have applied it before the failure.
            effect_status = EffectStatus.INDETERMINATE if failed else EffectStatus.CONFIRMED
            for effect in snapshot.effects.values():
                if effect.intent.step_key == key and effect.status == EffectStatus.PENDING:
                    effects.append(
                        replace(effect, status=effect_status, result=None if failed else result)
                    )
        try:
            await self.store.commit(
                lease,
                state=state,
                cursor=state.get("current_node_id"),
                # The journal has no pending/cancelled; the legacy status stays in state.
                status={"cancelled": "failed", "pending": "running"}.get(status, status),
                steps=steps,
                effects=effects,
                events=events or [],
                expected_revision=expected_revision,
            )
        except ExecutionConflictError as exc:
            current = await self.store.get(identity)
            if current.run.revision != expected_revision:
                raise SessionConflictError(run_id, expected_revision, current.run.revision) from exc
            raise
        result_record = await self.get_run(workspace_id, run_id)
        assert result_record is not None
        return result_record

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
