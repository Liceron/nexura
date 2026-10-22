"""Round 3, minor 3 (check): recover() without a budget is refused with no journal write."""

from __future__ import annotations

from typing import Any

import pytest
from nexura_agents import Agent, FakeModel, MemoryCheckpointStore, Runner, ToolCall, tool
from nexura_agents.contracts.execution import ExecutionId
from nexura_agents.usage import BudgetConfig, BudgetManager, UsageError, UsageLedger


def agent(first: bool) -> Agent:
    @tool(requires_approval=True)
    def pay(amount: int) -> str:
        """Pay."""
        return "paid"

    responses: list[Any] = ["done"]
    if first:
        responses.insert(0, [ToolCall(id="c1", name="pay", arguments={"amount": 1})])
    return Agent(name="A", instructions="i", model=FakeModel(responses=responses), tools=[pay])


async def test_recover_refusal_without_budget_writes_nothing() -> None:
    mgr = BudgetManager(ledger=UsageLedger())
    budget = mgr.create_run_budget("ws", "s", config=BudgetConfig(max_tokens=10**7))
    store = MemoryCheckpointStore()
    paused = await Runner(checkpoint_store=store, budget=budget).run_async(agent(True), "go")
    journal = await store.open_execution_store()
    before = (await journal.get(ExecutionId("default", paused.run_id))).run
    with pytest.raises(UsageError):
        await Runner(checkpoint_store=store).recover_async(agent(False), paused.run_id)
    after = (await journal.get(ExecutionId("default", paused.run_id))).run
    assert (after.revision, after.fencing_token, after.lease_owner) == (
        before.revision,
        before.fencing_token,
        before.lease_owner,
    )
