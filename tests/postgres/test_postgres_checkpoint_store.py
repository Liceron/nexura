"""A6 S5: PostgresCheckpointStore against an explicitly configured disposable database.

Same store contract as the SQLite checkpoint store (fenced saves and deletes, exclusive
claims, single-use approvals), plus the execution journal used by ``Runner``. All model
calls and tools are local fakes.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from nexura_agents import Agent, FakeModel, ResolvedOutcome, Runner, ToolCall, tool
from nexura_agents.contracts.execution import EffectResolution, ExecutionId
from nexura_agents.errors import ApprovalDecisionError, CheckpointLeaseLostError
from nexura_postgres import PostgresCheckpointStore


class Crash(BaseException):
    """Simulated process death."""


@pytest.fixture
async def store() -> AsyncIterator[PostgresCheckpointStore]:
    uri = os.environ.get("NEXURA_TEST_POSTGRES_URI")
    if not uri:
        pytest.skip("NEXURA_TEST_POSTGRES_URI is not configured")
    instance = PostgresCheckpointStore(uri, workspace_id=f"s5-{uuid.uuid4().hex[:8]}")
    await instance.init_db()
    try:
        yield instance
    finally:
        await instance.close()


async def test_store_contract_matches_sqlite(store: PostgresCheckpointStore) -> None:
    assert await store.load("x") is None
    await store.save("x", "one")
    await store.save("x", "two")
    await store.save("y", "three")
    assert await store.load("x") == "two"
    assert await store.list_incomplete() == ["x", "y"]
    assert await store.claim("x", "o1", 60)
    assert not await store.claim("x", "o2", 60)
    for intruder in (None, "o2"):
        with pytest.raises(CheckpointLeaseLostError):
            await store.save("x", "stale", owner=intruder)
        with pytest.raises(CheckpointLeaseLostError):
            await store.delete("x", intruder, fenced=True)
    await store.save("x", "mine", owner="o1")
    with pytest.raises(CheckpointLeaseLostError):
        await store.save("nobody", "x", owner="o1")
    await store.delete("x", "o1", fenced=True)
    assert await store.load("x") is None
    assert await store.claim("z", "gone", 0.001)
    assert await store.claim("z", "next", 60)  # an expired claim is taken over
    await store.release("z", "next")
    assert await store.claim("z", "third", 60)
    await store.delete("y")  # operator purge
    assert await store.list_incomplete() == []


async def test_approvals_are_single_use(store: PostgresCheckpointStore) -> None:
    import time

    expires = time.time() + 60
    await store.save_approval("a1", "payload", expires)
    await store.save_approval("a1", "payload", expires)  # the same envelope again is fine
    with pytest.raises(ApprovalDecisionError):
        await store.save_approval("a1", "other", expires)
    assert await store.load_approval("a1") == "payload"
    assert not await store.consume_approval("a1", "wrong")
    assert await store.consume_approval("a1", "payload")
    assert not await store.consume_approval("a1", "payload")
    assert await store.load_approval("a1") == "payload"


async def test_runner_journal_on_postgres(store: PostgresCheckpointStore) -> None:
    n = {"charge": 0}

    @tool
    def charge(order: str) -> str:
        """Charge (not idempotent)."""
        n["charge"] += 1
        if n["charge"] == 1:
            raise Crash
        return f"charged {order}"

    def build(responses: list[Any]) -> Agent:
        return Agent(
            name="Billing",
            instructions="Bill.",
            model=FakeModel(responses=responses),
            tools=[charge],
        )

    with pytest.raises(Crash):
        await Runner(checkpoint_store=store).run_async(
            build([[ToolCall(id="c1", name="charge", arguments={"order": "A1"})], "x"]), "go"
        )
    (run_id,) = await store.list_incomplete()
    journal = await store.open_execution_store()
    eid = ExecutionId(store.workspace_id, run_id)
    snap = await journal.get(eid)
    assert set(snap.steps) == {"turn:1/call:c1"} and snap.run.lease_owner is None

    runner = Runner(checkpoint_store=store)
    paused = await runner.recover_async(build(["done"]), run_id)
    assert paused.interruption is not None
    assert paused.interruption.pause_reason == "effect_unknown"
    final = await runner.resume_async(
        build(["done"]), paused.interruption, outcomes={"c1": ResolvedOutcome("charged A1")}
    )
    assert final.status == "completed" and n["charge"] == 1
    snap = await journal.get(eid)
    assert snap.run.status == "completed"
    (effect,) = snap.effects.values()
    assert effect.resolution == EffectResolution.CONFIRMED
    assert await store.list_incomplete() == []
