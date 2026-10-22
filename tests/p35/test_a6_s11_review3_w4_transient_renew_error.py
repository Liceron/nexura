"""Round 3, R2-M1 fence: the terminal sweep's lease renewal is outside the "a failure to
settle at the terminal status is a RuntimeWarning" handling. A transient store error (not
LeaseLostError) in that renewal now fails a run whose work is complete: the caller gets the
storage error instead of the result."""

from __future__ import annotations

from typing import Any

from nexura_agents import Agent, FakeModel, MemoryCheckpointStore, Runner
from nexura_agents.usage import BudgetConfig, BudgetManager, UsageLedger


async def test_transient_renew_error_at_the_terminal_sweep_is_a_warning() -> None:
    mgr = BudgetManager(ledger=UsageLedger())
    budget = mgr.create_run_budget(
        "ws", "s", config=BudgetConfig(max_tokens=10**7), default_model="fake-model"
    )
    store = MemoryCheckpointStore()
    execution = await store.open_execution_store()
    original = type(execution).renew
    owned = "renew" in type(execution).__dict__  # inherited: restore by deleting it
    calls = {"n": 0}

    async def flaky(self: Any, lease: Any, ttl: float) -> Any:
        calls["n"] += 1
        raise OSError("database is locked (transient)")

    agent = Agent(name="A", instructions="i", model=FakeModel(responses=["done"]))
    runner = Runner(checkpoint_store=store, budget=budget)
    type(execution).renew = flaky  # only renewals fail; heartbeats are far apart (ttl/3)
    try:
        try:
            result = await runner.run_async(agent, "go")
        except OSError as exc:
            raise AssertionError(
                f"completed run turned into an error by the sweep's renewal: {exc!r}"
            ) from exc
    finally:
        if owned:
            type(execution).renew = original
        else:
            del type(execution).renew  # do not leave an override on the class
    assert result.status == "completed"
