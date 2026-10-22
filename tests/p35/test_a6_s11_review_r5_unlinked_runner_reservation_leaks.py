"""A6 S11 review repro R5: a reservation a Runner run makes outside its model/tool dispatch scopes
(history
processor / summarizer model, dynamic instructions) is never ended after a crash.

The Runner settles only *linked* reservations (include_unlinked is never passed, and an
SDK RunBudget scope is not the run id). A process that dies inside a history-processor
model call leaves its reservation ``active`` forever: neither recover() nor the run's
terminal status ends it, although the docs say active reservations do not outlive a
completed run.
"""

from __future__ import annotations

from typing import Any

import pytest
from nexura_agents import Agent, FakeModel, MemoryCheckpointStore, Runner
from nexura_agents.usage import BudgetConfig, BudgetManager, UsageLedger, admission


class Crash(BaseException):
    pass


async def test_history_processor_reservation_does_not_outlive_the_run() -> None:
    mgr = BudgetManager(ledger=UsageLedger())
    budget = mgr.create_run_budget(
        "ws", "scope-1", config=BudgetConfig(max_tokens=1_000_000), default_model="fake-model"
    )
    crash = {"now": True}

    def summarizer(ctx: Any, messages: list[Any]) -> list[Any]:
        # e.g. nexura_agents.history summarize(): a model call admitted on the run's budget
        admission.active_budget.get().admit_provider_call("fake-model", estimated_tokens=700)
        if crash.pop("now", False):
            raise Crash("process died inside the summarizer call")
        return messages

    def agent() -> Agent:
        return Agent(
            name="A",
            instructions="i",
            model=FakeModel(responses=["done"]),
            history_processors=[summarizer],
        )

    store = MemoryCheckpointStore()
    with pytest.raises(Crash):
        await Runner(checkpoint_store=store, budget=budget).run_async(agent(), "go")
    run_id = next(iter(store._data))
    result = await Runner(checkpoint_store=store, budget=budget).recover_async(agent(), run_id)
    assert result.status == "completed"
    left = [r for r in mgr.ledger._reservations.values() if r.status in ("active", "expired")]
    # The second summarizer call leaked too (never settled by this toy processor); the
    # point is the FIRST one, from the crashed process, which nothing ever ends.
    assert left == [], [(r.reservation_id, r.estimated_tokens, r.metadata) for r in left]
