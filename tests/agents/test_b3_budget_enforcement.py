"""Regression: RunBudget max_turns / max_tool_calls are enforced by the Runner."""

from __future__ import annotations

from typing import Any

import pytest
from nexura_agents.usage import BudgetConfig, BudgetManager
from nexura_agents.usage.errors import BudgetExceededError

from nexura_runtime import Agent, FakeModel, Runner, ToolCall, tool


def _call(call_id: str = "c1") -> list[ToolCall]:
    return [ToolCall(id=call_id, name="ping", arguments={})]


def _budget(**limits: Any) -> Any:
    return BudgetManager().create_run_budget(
        workspace_id="w", run_id="r", config=BudgetConfig(**limits)
    )


def _agent(model: FakeModel, executed: list[str]) -> Agent:
    @tool
    def ping() -> str:
        """Ping."""
        executed.append("x")
        return "pong"

    return Agent(name="A", instructions="x", model=model, tools=[ping])


async def test_budget_max_turns_stops_before_over_limit_model_call() -> None:
    model = FakeModel(responses=[_call("c1"), _call("c2"), "done"])
    budget = _budget(max_turns=2)
    with pytest.raises(BudgetExceededError) as info:
        await Runner(budget=budget).run_async(_agent(model, []), "go")
    assert info.value.limit_type == "turns"
    assert len(model.calls) == 2


async def test_budget_max_tool_calls_stops_before_tool_executes() -> None:
    executed: list[str] = []
    model = FakeModel(responses=[_call("c1"), _call("c2"), "done"])
    budget = _budget(max_tool_calls=1)
    with pytest.raises(BudgetExceededError) as info:
        await Runner(budget=budget).run_async(_agent(model, executed), "go")
    assert info.value.limit_type == "tool_calls"
    assert executed == ["x"]


async def test_budget_shared_across_runs_and_unlimited_is_unaffected() -> None:
    budget = _budget(max_turns=2)
    runner = Runner(budget=budget)
    await runner.run_async(_agent(FakeModel(responses=["a"]), []), "go")
    await runner.run_async(_agent(FakeModel(responses=["b"]), []), "go")
    with pytest.raises(BudgetExceededError):
        await runner.run_async(_agent(FakeModel(responses=["c"]), []), "go")
    await Runner().run_async(_agent(FakeModel(responses=["ok"]), []), "go")


async def test_budget_resume_after_approval_does_not_double_count() -> None:
    executed: list[str] = []

    @tool(requires_approval=True)
    def ping() -> str:
        """Ping."""
        executed.append("x")
        return "pong"

    model = FakeModel(responses=[_call(), "done"])
    agent = Agent(name="A", instructions="x", model=model, tools=[ping])
    budget = _budget(max_turns=2, max_tool_calls=1)
    runner = Runner(budget=budget)
    paused = await runner.run_async(agent, "go")
    interruption = paused.interruption
    assert interruption is not None
    result = await runner.resume_async(agent, interruption, approve=True)
    assert result.output == "done"
    assert executed == ["x"]
