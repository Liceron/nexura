"""Async instruction functions (B10): awaited by the runner, clear errors elsewhere."""

from __future__ import annotations

import warnings
from typing import Any

import pytest
from nexura_agents import Agent, FakeModel, Runner, ToolCall
from nexura_agents.errors import SDKValidationError
from nexura_agents.run_context import RunContext


async def greeting(ctx: RunContext[Any]) -> str:
    return f"You help {ctx.deps['user']}."


def system_sent(model: FakeModel, call: int = 0) -> str | None:
    return model.calls[call]["messages"][0].content


async def test_async_instructions_are_awaited() -> None:
    model = FakeModel(responses=["ok"])
    agent = Agent(name="A", instructions=greeting, model=model)
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # no "coroutine was never awaited"
        result = await Runner().run_async(agent, "hi", deps={"user": "Asha"})
    assert result.output == "ok"
    assert system_sent(model) == "You help Asha."


def test_async_instructions_with_the_sync_runner() -> None:
    model = FakeModel(responses=["ok"])
    agent = Agent(name="A", instructions=greeting, model=model)
    Runner().run(agent, "hi", deps={"user": "Ravi"})
    assert system_sent(model) == "You help Ravi."


async def test_async_instructions_of_a_handoff_target() -> None:
    async def billing_rules(ctx: RunContext[Any]) -> str:
        return "Billing rules."

    billing_model = FakeModel(responses=["paid"])
    billing = Agent(name="Billing", instructions=billing_rules, model=billing_model)
    triage = Agent(
        name="Triage",
        instructions="Route.",
        model=FakeModel(responses=[[ToolCall(id="h", name="transfer_to_billing", arguments={})]]),
        handoffs=[billing],
    )
    result = await Runner().run_async(triage, "pay")
    assert result.output == "paid"
    assert system_sent(billing_model) == "Billing rules."


async def test_async_instructions_with_output_schema() -> None:
    from pydantic import BaseModel

    class Out(BaseModel):
        x: int

    async def rules(ctx: RunContext[Any]) -> str:
        return "Rules."

    model = FakeModel(responses=['{"x": 1}'])
    agent = Agent(name="A", instructions=rules, model=model, output_type=Out)
    result = await Runner().run_async(agent, "go")
    assert result.output == Out(x=1)
    sent = system_sent(model) or ""
    assert sent.startswith("Rules.") and "JSON" in sent


def test_sync_render_gives_a_clear_error_and_no_warning() -> None:
    agent = Agent(name="A", instructions=greeting, model=FakeModel())
    ctx: RunContext[Any] = RunContext(deps={"user": "x"}, run_id="r", agent_name="A")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with pytest.raises(SDKValidationError, match="render_instructions_async"):
            agent.render_instructions(ctx)


@pytest.mark.parametrize("bad", [None, 42, ["a"]])
async def test_instructions_must_produce_text(bad: Any) -> None:
    agent = Agent(name="A", instructions=lambda ctx: bad, model=FakeModel(responses=["x"]))
    with pytest.raises(SDKValidationError, match="must produce a str"):
        await Runner().run_async(agent, "hi")
