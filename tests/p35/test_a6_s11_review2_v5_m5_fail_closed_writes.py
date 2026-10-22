"""Round 2, M5: "fails closed ... before any journal write" (docs/durable-execution.md and
budgets-and-usage.md). The SDK Runner checks the budget scope only after journal.begin has
claimed the run lease (a fenced write that bumps the fencing token and revision)."""

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


async def test_runner_refusal_without_budget_writes_nothing() -> None:
    mgr = BudgetManager(ledger=UsageLedger())
    budget = mgr.create_run_budget("ws", "s", config=BudgetConfig(max_tokens=10**7))
    store = MemoryCheckpointStore()
    paused = await Runner(checkpoint_store=store, budget=budget).run_async(agent(True), "go")
    journal = await store.open_execution_store()
    before = (await journal.get(ExecutionId("default", paused.run_id))).run
    with pytest.raises(UsageError):
        await Runner(checkpoint_store=store).resume_async(
            agent(False), paused.interruption, approve=True
        )
    after = (await journal.get(ExecutionId("default", paused.run_id))).run
    assert (after.revision, after.fencing_token) == (before.revision, before.fencing_token), (
        f"refusal wrote the journal: revision {before.revision}->{after.revision}, "
        f"fencing token {before.fencing_token}->{after.fencing_token}"
    )
