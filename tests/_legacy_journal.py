"""Explicit historical journal fixtures; never use the retired create/commit APIs."""

from __future__ import annotations

import copy
import json
from dataclasses import asdict

from nexura_agents.contracts.execution import (
    EffectRecord,
    EffectStatus,
    ExecutionId,
    StepIntent,
    StepKey,
    default_idempotency_key,
)
from nexura_agents.execution_store import (
    ExecutionEvent,
    ExecutionRun,
    ExecutionSnapshot,
    ExecutionStep,
    ExecutionStore,
    MemoryExecutionStore,
)
from nexura_agents.execution_store.sql import _SQLStore


async def seed_legacy(store: ExecutionStore, status: str = "paused") -> ExecutionSnapshot:
    """Seed the envelope/rows a pre-S12 engine left, including its final event."""
    await store.init_db()
    identity = ExecutionId("ws", "legacy-run")
    run = ExecutionRun(
        identity,
        "legacy-digest",
        kind="durable_graph",
        status=status,
        state={"status": status, "current_node_id": "pay", "state": {"amount": 5}},
        origin={"source": "sdk", "legacy_metadata": {}, "legacy_input": {"amount": 5}},
        revision=3,
        fencing_token=1,
    )
    key = StepKey.workflow("pay", 0)
    intent = StepIntent(key, "digest", default_idempotency_key(identity, key))
    effect = EffectRecord(
        identity,
        intent,
        status=EffectStatus.INDETERMINATE if status == "paused" else EffectStatus.CONFIRMED,
    )
    event = ExecutionEvent(1, f"run_{status}", {"type": f"run_{status}"}, 1.0)
    snapshot = ExecutionSnapshot(
        run, {str(key): ExecutionStep(intent=intent)}, {effect.effect_id: effect}, [event]
    )
    if isinstance(store, MemoryExecutionStore):
        store._runs[identity] = copy.deepcopy(snapshot)
    else:
        assert isinstance(store, _SQLStore)
        async with store._connection() as db:
            params = store._run_params(run)
            await db.execute(
                """INSERT INTO nexura_exec_runs
                (workspace_id, run_id, definition_digest, journal_schema_version, state_version,
                 status, lease_owner, lease_expires_at, fencing_token, revision, run_json)
                VALUES (:workspace, :run, :definition_digest, :journal_schema_version,
                 :state_version, :status, :lease_owner, :lease_expires_at, :fencing_token,
                 :revision, :run_json)""",
                params,
            )
            await db.execute(
                """INSERT INTO nexura_exec_steps
                (workspace_id, run_id, step_key, intent_json, outcome_json)
                VALUES (:workspace, :run, :key, :intent, NULL)""",
                {**params, "key": str(key), "intent": json.dumps(asdict(intent))},
            )
            await db.execute(
                """INSERT INTO nexura_exec_effects
                (workspace_id, run_id, effect_id, idempotency_key, status,
                 resolution, resolved_by, record_json)
                VALUES (:workspace, :run, :eid, :ikey, :estatus, NULL, NULL, :record)""",
                {
                    **params,
                    "eid": effect.effect_id,
                    "ikey": intent.idempotency_key,
                    "estatus": effect.status.value,
                    "record": json.dumps(asdict(effect)),
                },
            )
            await db.execute(
                """INSERT INTO nexura_exec_events
                (workspace_id, run_id, sequence, event_type, payload_json, timestamp)
                VALUES (:workspace, :run, 1, :event, :payload, 1.0)""",
                {**params, "event": event.event_type, "payload": json.dumps(event.payload)},
            )
    return snapshot
