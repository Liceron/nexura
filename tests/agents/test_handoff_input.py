"""Structured handoff input and enabled handoffs (F33)."""

from __future__ import annotations

from typing import Any

import pytest
from nexura_agents import Agent, FakeModel, Handoff, Runner, ToolCall
from nexura_agents.errors import SDKValidationError, ToolValidationError, UnknownToolError
from nexura_agents.events import RunEventType
from nexura_agents.run_context import RunContext
from pydantic import BaseModel


class Escalation(BaseModel):
    priority: int
    summary: str


def transfer(args: dict[str, Any]) -> list[ToolCall]:
    return [ToolCall(id="h1", name="transfer_to_billing", arguments=args)]


def team(handoff_kwargs: dict[str, Any], triage_responses: list[Any]) -> tuple[Agent, list[Any]]:
    seen: list[Any] = []

    def billing_instructions(ctx: RunContext[Any]) -> str:
        seen.append(ctx.metadata.get("handoff_input"))
        return "Handle billing."

    billing = Agent(
        name="Billing", instructions=billing_instructions, model=FakeModel(responses=["done"])
    )
    triage = Agent(
        name="Triage",
        instructions="Route.",
        model=FakeModel(responses=triage_responses),
        handoffs=[Handoff(billing, **handoff_kwargs)],
    )
    return triage, seen


async def test_input_type_is_the_tool_schema_and_reaches_target_and_callback() -> None:
    got: list[Any] = []

    async def on_handoff(ctx: RunContext[Any], data: Escalation) -> None:
        got.append(data)

    triage, seen = team(
        {"input_type": Escalation, "on_handoff": on_handoff},
        [transfer({"priority": 2, "summary": "double charge"})],
    )
    schema = triage.handoffs[0].tool.to_schema()["function"]["parameters"]
    assert set(schema["required"]) == {"priority", "summary"}
    result = await Runner().run_async(triage, "help")
    assert result.output == "done"
    assert got == [Escalation(priority=2, summary="double charge")]
    assert seen == [{"priority": 2, "summary": "double charge"}]


async def test_invalid_handoff_input_fails_clearly() -> None:
    triage, _ = team({"input_type": Escalation}, [transfer({"priority": "high"})])
    with pytest.raises(ToolValidationError):
        await Runner().run_async(triage, "help")


async def test_reason_callback_is_unchanged_without_input_type() -> None:
    got: list[Any] = []
    triage, seen = team(
        {"on_handoff": lambda ctx, reason: got.append(reason)}, [transfer({"reason": "billing"})]
    )
    await Runner().run_async(triage, "help")
    assert got == ["billing"] and seen == [None]


async def test_disabled_handoff_is_not_offered_and_cannot_be_called() -> None:
    triage, _ = team({"enabled": False}, [transfer({})])
    with pytest.raises(UnknownToolError):
        await Runner().run_async(triage, "help")
    model = triage.model
    assert model.calls[0]["tools"] is None  # type: ignore[attr-defined]


async def test_enabled_rule_sees_deps() -> None:
    triage, _ = team(
        {"enabled": lambda ctx: ctx.deps == "vip"},
        [transfer({}), transfer({})],
    )
    with pytest.raises(UnknownToolError):
        await Runner().run_async(triage, "help", deps="regular")
    result = await Runner().run_async(triage, "help", deps="vip")
    assert result.output == "done"
    events = [e for e in result.events if e.type == RunEventType.HANDOFF]
    assert events and events[0].data["to"] == "Billing"


async def test_handoff_input_survives_durable_recovery() -> None:
    from nexura_agents import MemoryCheckpointStore

    class Crash(BaseException):
        pass

    def crash(messages: Any, tools: Any) -> Any:
        raise Crash

    store = MemoryCheckpointStore()
    triage, _ = team({"input_type": Escalation}, [transfer({"priority": 1, "summary": "s"})])
    triage.handoffs[0].agent.model = FakeModel(handler=crash)
    with pytest.raises(Crash):
        await Runner(checkpoint_store=store).run_async(triage, "help")
    (run_id,) = await store.list_incomplete()
    again, seen = team({"input_type": Escalation}, [])
    result = await Runner(checkpoint_store=store).recover_async(again, run_id)
    assert result.output == "done"


@pytest.mark.parametrize(
    "kwargs, match",
    [({"input_type": dict}, "input_type"), ({"enabled": "yes"}, "enabled")],
)
def test_handoff_options_are_validated(kwargs: dict[str, Any], match: str) -> None:
    billing = Agent(name="Billing", instructions="i", model=FakeModel())
    with pytest.raises(SDKValidationError, match=match):
        Handoff(billing, **kwargs)


async def test_handoff_input_does_not_leak_to_the_next_agent_in_a_chain() -> None:
    seen: list[Any] = []

    def closer_rules(ctx: RunContext[Any]) -> str:
        seen.append(ctx.metadata.get("handoff_input"))
        return "Close."

    closer = Agent(name="Closer", instructions=closer_rules, model=FakeModel(responses=["closed"]))
    billing = Agent(
        name="Billing",
        instructions="i",
        model=FakeModel(responses=[[ToolCall(id="h2", name="transfer_to_closer", arguments={})]]),
        handoffs=[closer],
    )
    triage = Agent(
        name="Triage",
        instructions="i",
        model=FakeModel(responses=[transfer({"priority": 1, "summary": "s"})]),
        handoffs=[Handoff(billing, input_type=Escalation)],
    )
    result = await Runner().run_async(triage, "help")
    assert result.output == "closed"
    assert seen == [None]


# ---------------------------------------------------------------- audit 6


async def test_each_agent_gets_its_own_output_retries_after_a_handoff() -> None:
    class Out(BaseModel):
        n: int

    b = Agent(
        name="B",
        instructions="b",
        model=FakeModel(responses=["not json", '{"n": 1}']),
        output_type=Out,
        output_retries=1,
    )
    a = Agent(
        name="A",
        instructions="a",
        model=FakeModel(
            responses=["bad", [ToolCall(id="h", name="transfer_to_b", arguments={"reason": "x"})]]
        ),
        output_type=Out,
        output_retries=1,
        handoffs=[b],
    )
    result = await Runner().run_async(a, "go")
    assert result.status == "completed" and result.output == Out(n=1)


async def test_cancel_interrupts_a_slow_on_handoff() -> None:
    import asyncio
    import time

    from nexura_agents import CancelToken

    async def slow(ctx: Any, reason: str) -> None:
        await asyncio.sleep(3)

    b = Agent(name="B", instructions="b", model=FakeModel(responses=["done"]))
    a = Agent(
        name="A",
        instructions="a",
        model=FakeModel(
            responses=[[ToolCall(id="h", name="transfer_to_b", arguments={"reason": "x"})]]
        ),
        handoffs=[Handoff(b, on_handoff=slow)],
    )
    token = CancelToken()
    asyncio.get_running_loop().call_later(0.1, token.cancel)
    started = time.monotonic()
    result = await Runner().run_async(a, "x", cancel=token)
    assert result.status == "cancelled" and time.monotonic() - started < 1.5


def test_diagram_marks_disabled_and_conditional_handoffs() -> None:
    b = Agent(name="B", instructions="b", model=FakeModel())
    c = Agent(name="C", instructions="c", model=FakeModel())
    a = Agent(
        name="A",
        instructions="a",
        model=FakeModel(),
        handoffs=[Handoff(b, enabled=False), Handoff(c, enabled=lambda ctx: True)],
    )
    diagram = a.to_mermaid()
    assert '-.->|"handoff (disabled)"|' in diagram
    assert '-.->|"handoff (conditional)"|' in diagram
