"""Approve a tool call with edited arguments: ApproveWith (F39)."""

from __future__ import annotations

from typing import Any

import pytest
from nexura_agents import (
    Agent,
    ApproveWith,
    FakeModel,
    MemorySessionStore,
    Runner,
    SchemaTool,
    ToolCall,
    tool,
)
from nexura_agents.errors import ApprovalDecisionError
from nexura_agents.types import RunInterruption

RAN: list[dict[str, Any]] = []


@tool(requires_approval=True)
def refund(order_id: str, amount: int) -> str:
    """Refund an order."""
    RAN.append({"order_id": order_id, "amount": amount})
    return f"refunded {amount} on {order_id}"


def refund_call(amount: int = 500) -> list[ToolCall]:
    return [ToolCall(id="c1", name="refund", arguments={"order_id": "A7", "amount": amount})]


async def paused_run(
    responses: list[Any] | None = None, **runner_kw: Any
) -> tuple[Agent, Runner, Any]:
    RAN.clear()
    agent = Agent(
        name="Support",
        instructions="i",
        model=FakeModel(responses=responses or [refund_call(), "Refunded."]),
        tools=[refund],
    )
    runner = Runner(**runner_kw)
    paused = await runner.run_async(
        agent, "refund my order", **({"session_id": "s"} if runner_kw else {})
    )
    assert paused.status == "paused"
    return agent, runner, paused


async def test_the_call_runs_with_the_edited_arguments() -> None:
    agent, runner, paused = await paused_run()
    digest = paused.interruption.pending_digests()["c1"]
    done = await runner.resume_async(
        agent,
        paused.interruption,
        approve={"c1": ApproveWith(arguments={"order_id": "A7", "amount": 100}, digest=digest)},
    )
    assert done.output == "Refunded."
    assert RAN == [{"order_id": "A7", "amount": 100}]
    call = next(m for m in done.messages if m.role == "assistant" and m.tool_calls).tool_calls[0]
    assert call.arguments == {"order_id": "A7", "amount": 100}  # the transcript shows what ran
    assert [m.content for m in done.messages if m.role == "tool"] == ["refunded 100 on A7"]


async def test_edit_without_a_digest_is_approved_by_call_id() -> None:
    agent, runner, paused = await paused_run()
    await runner.resume_async(
        agent,
        paused.interruption,
        approve={"c1": ApproveWith(arguments={"order_id": "A7", "amount": 50})},
    )
    assert RAN == [{"order_id": "A7", "amount": 50}]


async def test_a_wrong_digest_is_refused() -> None:
    agent, runner, paused = await paused_run()
    with pytest.raises(ApprovalDecisionError, match="does not match the pending action"):
        await runner.resume_async(
            agent,
            paused.interruption,
            approve={"c1": ApproveWith(arguments={"order_id": "A7", "amount": 1}, digest="bad")},
        )
    assert RAN == []


@pytest.mark.parametrize(
    "arguments, match",
    [
        ({"order_id": "A7", "amount": "lots"}, "do not fit tool 'refund'"),
        ({"order_id": "A7"}, "do not fit tool 'refund'"),
        (["not", "a", "dict"], "must be a dict"),
    ],
)
async def test_invalid_edits_are_refused_before_anything_runs(arguments: Any, match: str) -> None:
    agent, runner, paused = await paused_run()
    with pytest.raises(ApprovalDecisionError, match=match):
        await runner.resume_async(
            agent, paused.interruption, approve={"c1": ApproveWith(arguments=arguments)}
        )
    assert RAN == []


async def test_edit_survives_json_and_is_saved_in_the_session() -> None:
    store = MemorySessionStore()
    agent, runner, paused = await paused_run(session_store=store)
    restored = RunInterruption.from_json(paused.interruption.to_json())
    await runner.resume_async(
        agent,
        restored,
        approve={"c1": ApproveWith(arguments={"order_id": "A7", "amount": 75})},
    )
    saved = await store.get("s")
    assert saved is not None
    calls = [c for m in saved.messages if m.tool_calls for c in m.tool_calls]
    assert calls[0].arguments == {"order_id": "A7", "amount": 75}


async def test_schema_tool_edits_are_validated_against_the_json_schema() -> None:
    ran: list[Any] = []

    async def handler(ctx: Any, arguments: dict[str, Any]) -> str:
        ran.append(arguments)
        return "sent"

    send = SchemaTool(
        "send",
        {"type": "object", "properties": {"to": {"type": "string"}}, "required": ["to"]},
        handler,
        requires_approval=True,
    )
    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel(
            responses=[[ToolCall(id="c1", name="send", arguments={"to": "all"})], "ok"]
        ),
        tools=[send],
    )
    runner = Runner()
    paused = await runner.run_async(agent, "go")
    with pytest.raises(ApprovalDecisionError, match="do not fit"):
        await runner.resume_async(
            agent, paused.interruption, approve={"c1": ApproveWith(arguments={"to": 5})}
        )
    await runner.resume_async(
        agent, paused.interruption, approve={"c1": ApproveWith(arguments={"to": "ops"})}
    )
    assert ran == [{"to": "ops"}]


async def test_mixing_plain_digests_and_edits() -> None:
    @tool(requires_approval=True)
    def notify(text: str) -> str:
        """Notify."""
        return f"notified: {text}"

    batch = [
        ToolCall(id="c1", name="refund", arguments={"order_id": "A7", "amount": 500}),
        ToolCall(id="c2", name="notify", arguments={"text": "refund 500"}),
    ]
    RAN.clear()
    agent = Agent(
        name="A",
        instructions="i",
        model=FakeModel(responses=[batch, "done"]),
        tools=[refund, notify],
    )
    runner = Runner()
    paused = await runner.run_async(agent, "go")
    digests = paused.interruption.pending_digests()
    done = await runner.resume_async(
        agent,
        paused.interruption,
        approve={
            "c1": ApproveWith(arguments={"order_id": "A7", "amount": 200}, digest=digests["c1"]),
            "c2": digests["c2"],
        },
    )
    assert sorted(m.content for m in done.messages if m.role == "tool") == [
        "notified: refund 500",
        "refunded 200 on A7",
    ]


async def test_edits_with_unknown_argument_names_are_refused() -> None:
    agent, runner, paused = await paused_run()
    with pytest.raises(ApprovalDecisionError, match="unknown arguments: note"):
        await runner.resume_async(
            agent,
            paused.interruption,
            approve={"c1": ApproveWith(arguments={"order_id": "A7", "amount": 1, "note": "x"})},
        )
    assert RAN == []
