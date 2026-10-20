"""S3/S4 Postgres effects round trip against an explicitly configured disposable database."""

import os
import uuid

import pytest
from nexura_agents.contracts.errors import IndeterminateEffectError
from nexura_agents.contracts.execution import EffectResolution, EffectStatus, ExecutionId
from nexura_agents.errors import SessionConflictError
from nexura_agents.execution_store import EffectAlreadyResolvedError
from nexura_postgres import PostgresRepository

from nexura_runtime.runtime.execution import (
    DurableExecutionEngine,
    DurableNodeSpec,
    DurableWorkflowSpec,
)
from nexura_runtime.runtime.execution.models import NodeIntent


async def test_postgres_legacy_engine_effect_resolution_is_authoritative() -> None:
    uri = os.environ.get("NEXURA_TEST_POSTGRES_URI")
    if not uri:
        pytest.skip("NEXURA_TEST_POSTGRES_URI is not configured")
    repo = PostgresRepository(uri)
    try:
        await repo.init_db()
        engine = DurableExecutionEngine(repo)
        workflow = DurableWorkflowSpec(
            workflow_id="fake-effects",
            entry_node="pay",
            terminal_nodes=["pay"],
            nodes={"pay": DurableNodeSpec(node_id="pay", handler="fake-pay")},
        )
        run_id = uuid.uuid4().hex
        await engine.init_run("s34", run_id, workflow)
        state = await engine.claim_run("s34", run_id, "original")
        state.pre_dispatch_intent = NodeIntent(
            node_id="pay", effect_policy="non_idempotent_write", dispatched=True
        )
        await repo.update_run("s34", run_id, state.revision, "running", state.model_dump())
        with pytest.raises(IndeterminateEffectError):
            await engine.execute_step("s34", run_id, "original", workflow)
        store = await engine.get_execution_store()
        snapshot = await store.get(ExecutionId("s34", run_id))
        effect = next(iter(snapshot.effects.values()))
        assert effect.status == EffectStatus.INDETERMINATE
        await engine.resolve_effect(
            "s34",
            run_id,
            effect.effect_id,
            EffectResolution.CONFIRMED,
            actor="postgres-test",
            result={"receipt": "local-fake"},
        )
        with pytest.raises(EffectAlreadyResolvedError):
            await engine.resolve_effect(
                "s34", run_id, effect.effect_id, EffectResolution.CONFIRMED, actor="postgres-test"
            )
        calls: list[str] = []
        engine.register_handler("fake-pay", lambda _: calls.append("dispatched again"))
        done = await engine.run_until_idle("s34", run_id, "resumer", workflow)
        assert done.status == "completed" and done.state["receipt"] == "local-fake"
        assert calls == []  # a confirmed effect is never dispatched again
        final = await store.get(ExecutionId("s34", run_id))
        resolved = final.effects[effect.effect_id]
        assert resolved.status == EffectStatus.RESOLVED
        assert resolved.resolution == EffectResolution.CONFIRMED
        assert resolved.resolved_by == "postgres-test"
        assert [e.payload["actor"] for e in final.events if e.event_type == "effect_resolved"] == [
            "postgres-test"
        ]
        # The repository serves the run from the journal, and a stale CAS write is refused.
        record = await repo.get_run("s34", run_id)
        assert record is not None and record.status == "completed"
        with pytest.raises(SessionConflictError):
            await repo.update_run("s34", run_id, state.revision, "running")
    finally:
        await repo.close()


async def test_postgres_plain_repository_runs_do_not_touch_the_journal() -> None:
    uri = os.environ.get("NEXURA_TEST_POSTGRES_URI")
    if not uri:
        pytest.skip("NEXURA_TEST_POSTGRES_URI is not configured")
    repo = PostgresRepository(uri)
    try:
        await repo.init_db()
        run_id = uuid.uuid4().hex
        created = await repo.create_run("s34", run_id, initial_state={"step": 0})
        updated = await repo.update_run("s34", run_id, created.revision, "step1", {"step": 1})
        assert updated.revision == created.revision + 1 and updated.status == "step1"
        assert getattr(repo, "_execution_store", None) is None
    finally:
        await repo.close()
