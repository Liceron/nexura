"""Round 4, minor 2: a transient error in the terminal sweep's lease renewal now skips the
sweep "for recovery". If the terminal commit that follows succeeds (the usual case for a
one-off blip), the run is completed and its checkpoint deleted: there is no recovery, and
the skipped reservations stay active for good."""

from __future__ import annotations

from typing import Any

import pytest
from nexura_agents import Agent, FakeModel, MemoryCheckpointStore, Runner, ToolCall, tool
from nexura_agents.errors import RunRecoveryError
from nexura_agents.usage import BudgetConfig, BudgetManager, UsageLedger, admission


async def test_reservations_are_not_stranded_by_a_one_off_renewal_error() -> None:
    mgr = BudgetManager(ledger=UsageLedger())
    budget = mgr.create_run_budget(
        "ws", "s", config=BudgetConfig(max_tokens=10**7), default_model="fake-model"
    )

    @tool
    def helper(x: int) -> str:
        """Reserves and leaks (e.g. a thread that timed out)."""
        admission.active_budget.get().admit_provider_call("fake-model", estimated_tokens=300)
        return "ok"

    agent = Agent(
        name="A",
        instructions="i",
        tools=[helper],
        model=FakeModel(responses=[[ToolCall(id="c1", name="helper", arguments={"x": 1})], "done"]),
    )
    store = MemoryCheckpointStore()
    execution = await store.open_execution_store()
    cls = type(execution)
    original = cls.renew
    owned = "renew" in cls.__dict__  # inherited: restore by deleting the override
    state = {"failed": False}

    async def once(self: Any, lease: Any, ttl: float) -> Any:
        if not state["failed"]:
            state["failed"] = True
            raise OSError("database is locked (one-off)")
        return await original(self, lease, ttl)

    cls.renew = once  # the first renewal is the terminal sweep's (heartbeat is ttl/3 = 10 s)
    try:
        with pytest.warns(RuntimeWarning):
            result = await Runner(checkpoint_store=store, budget=budget).run_async(agent, "go")
    finally:
        if owned:
            cls.renew = original
        else:
            del cls.renew  # do not leave an override on the class (API reference)
    assert result.status == "completed" and state["failed"]
    with pytest.raises(RunRecoveryError):  # nothing left to recover: the run is finished
        await Runner(checkpoint_store=store, budget=budget).recover_async(agent, result.run_id)
    left = [r for r in mgr.ledger._reservations.values() if r.status in ("active", "expired")]
    assert left == [], f"{len(left)} reservation(s) left active after a completed run"
