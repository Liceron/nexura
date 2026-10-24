# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""P39 G4: streamed reply text never reaches the consumer before output guardrails pass.

Offline only: FakeModel (labelled local fake models), no provider calls.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any

import pytest
from nexura_agents import (
    Agent,
    AgentHooks,
    CancelToken,
    ComposedHooks,
    FakeModel,
    GuardrailResult,
    GuardrailTripwireTriggered,
    RunEvent,
    RunEventType,
    Runner,
    StreamGuard,
    ToolCall,
    redact_output_pii,
    set_default_hooks,
    tool,
)
from nexura_agents.errors import SDKValidationError
from nexura_agents.session import MemorySessionStore
from pydantic import BaseModel

CARD = "4111 1111 1111 1111"
SECRET_REPLY = f"Your card number is {CARD}, keep it safe."
CHECKED_REPLY = "Your card number is [REDACTED:card], keep it safe."
T = RunEventType


@pytest.fixture(autouse=True)
def _no_default_hooks() -> Iterator[None]:
    from nexura_agents import policy

    policy._unlock_for_tests()
    set_default_hooks(None)
    yield
    policy._unlock_for_tests()
    set_default_hooks(None)


def fake(*responses: Any, chunk_size: int = 3) -> FakeModel:
    """A local fake model that streams its replies in small pieces."""
    return FakeModel(list(responses), stream=True, chunk_size=chunk_size)


async def collect(stream: AsyncIterator[RunEvent]) -> tuple[list[RunEvent], BaseException | None]:
    events: list[RunEvent] = []
    try:
        async for event in stream:
            events.append(event)
    except Exception as exc:  # the run's error, raised after RUN_FAILED
        return events, exc
    return events, None


def deltas(events: Sequence[RunEvent]) -> list[str]:
    return [e.data["text"] for e in events if e.type == T.TEXT_DELTA]


def kinds(events: Sequence[RunEvent]) -> list[RunEventType]:
    return [e.type for e in events]


def card_free(text: str) -> bool:
    digits = "".join(ch for ch in text if ch.isdigit())
    return "4111" not in text and "1111" not in digits


def redact() -> Any:
    return redact_output_pii(["card"])


# ------------------------------------------------------------------- hold (the default)


async def test_hold_never_streams_unchecked_text_and_finishes_with_the_checked_reply() -> None:
    agent = Agent(
        name="A", instructions="i", model=fake(SECRET_REPLY), output_guardrails=[redact()]
    )
    events, error = await collect(Runner().stream_async(agent, "card?", stream_text=True))
    assert error is None
    assert deltas(events) == []
    started = next(e for e in events if e.type == T.MODEL_STARTED)
    assert started.data["stream_guard"] == "hold"
    assert next(e for e in events if e.type == T.FINAL_OUTPUT).data["output"] == CHECKED_REPLY
    assert CARD not in json.dumps([e.data.get("output") for e in events], default=str)
    assert not any(e.type == T.PARTIAL_OUTPUT for e in events)


async def test_hold_shows_nothing_while_the_guardrail_is_still_checking() -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_review(ctx: Any, output: Any) -> GuardrailResult:
        entered.set()
        await release.wait()
        return GuardrailResult.rewritten("reviewed reply")

    agent = Agent(
        name="A", instructions="i", model=fake(SECRET_REPLY), output_guardrails=[slow_review]
    )
    seen: list[RunEvent] = []

    async def consume() -> None:
        async for event in Runner().stream_async(agent, "go", stream_text=True):
            seen.append(event)

    task = asyncio.create_task(consume())
    await asyncio.wait_for(entered.wait(), 5)
    assert not any(e.type == T.TEXT_DELTA for e in seen)
    assert T.MODEL_COMPLETED not in kinds(seen) or deltas(seen) == []
    release.set()
    await asyncio.wait_for(task, 5)
    assert deltas(seen) == []
    assert [e.data["output"] for e in seen if e.type == T.FINAL_OUTPUT] == ["reviewed reply"]


async def test_hold_rewrite_is_what_the_consumer_session_and_result_get() -> None:
    store = MemorySessionStore()
    agent = Agent(
        name="A", instructions="i", model=fake(SECRET_REPLY), output_guardrails=[redact()]
    )
    events, error = await collect(
        Runner(session_store=store).stream_async(agent, "card?", session_id="s", stream_text=True)
    )
    assert error is None
    done = events[-1]
    assert done.type == T.RUN_COMPLETED and done.data["output"] == CHECKED_REPLY
    snapshot = await store.get("s")
    assert snapshot is not None
    assert all(CARD not in (m.content or "") for m in snapshot.messages)
    assert snapshot.messages[-1].content == CHECKED_REPLY


async def test_hold_tripwire_emits_guardrail_tripped_without_content() -> None:
    def judge(ctx: Any, output: Any) -> GuardrailResult:
        return GuardrailResult.tripped({"reason": f"leak {output}", "categories": ["privacy"]})

    agent = Agent(name="A", instructions="i", model=fake(SECRET_REPLY), output_guardrails=[judge])
    events, error = await collect(Runner().stream_async(agent, "card?", stream_text=True))
    assert isinstance(error, GuardrailTripwireTriggered) and error.stage == "output"
    assert deltas(events) == []
    assert kinds(events)[-3:] == [T.GUARDRAIL_DECISION, T.GUARDRAIL_TRIPPED, T.RUN_FAILED]
    tripped = events[-2].data
    assert tripped == {
        "stage": "output",
        "guardrail": "judge",
        "agent": "A",
        "turn": 1,
        "phase": "final",
        "stream_guard": "hold",
        "shown": False,
    }
    assert "4111" not in json.dumps([e.data for e in events[-3:]], default=str)
    assert not any(e.type == T.FINAL_OUTPUT for e in events)


async def test_a_tripped_final_check_without_streaming_also_reports_guardrail_tripped() -> None:
    def judge(ctx: Any, output: Any) -> GuardrailResult:
        return GuardrailResult.tripped()

    agent = Agent(name="A", instructions="i", model=FakeModel(["x"]), output_guardrails=[judge])
    events, error = await collect(Runner().stream_async(agent, "go"))
    assert isinstance(error, GuardrailTripwireTriggered)
    tripped = next(e for e in events if e.type == T.GUARDRAIL_TRIPPED).data
    assert tripped["stream_guard"] is None and tripped["shown"] is False


async def test_hold_does_not_show_text_written_before_tool_calls() -> None:
    @tool
    def lookup(order: str) -> str:
        """Look an order up."""
        return "shipped"

    model = FakeModel(
        [
            ModelTurnWithText("Let me check order 4111 1111 1111 1111.", "lookup"),
            "It shipped.",
        ],
        stream=True,
        chunk_size=4,
    )
    agent = Agent(
        name="A", instructions="i", model=model, tools=[lookup], output_guardrails=[redact()]
    )
    events, error = await collect(Runner().stream_async(agent, "status", stream_text=True))
    assert error is None
    assert deltas(events) == []
    assert [e.data["output"] for e in events if e.type == T.FINAL_OUTPUT] == ["It shipped."]


def ModelTurnWithText(text: str, tool_name: str) -> Any:  # noqa: N802 - reads like a value
    """A scripted FakeModel turn with text before a tool call."""
    from nexura_agents.types import Message, ModelResponse

    return ModelResponse(
        message=Message(
            role="assistant",
            content=text,
            tool_calls=[ToolCall(id="c1", name=tool_name, arguments={"order": "A1"})],
        )
    )


# --------------------------------------------------------------- no output guardrails


async def test_without_output_guardrails_text_streams_exactly_as_before() -> None:
    agent = Agent(name="A", instructions="i", model=fake(SECRET_REPLY))
    events, error = await collect(Runner().stream_async(agent, "go", stream_text=True))
    assert error is None
    pieces = deltas(events)
    assert len(pieces) == -(-len(SECRET_REPLY) // 3) and "".join(pieces) == SECRET_REPLY
    started = next(e for e in events if e.type == T.MODEL_STARTED)
    assert set(started.data) == {"turn", "messages_count"}
    assert all(set(e.data) == {"turn", "text"} for e in events if e.type == T.TEXT_DELTA)
    assert not any(e.type == T.GUARDRAIL_TRIPPED for e in events)


async def test_passthrough_is_an_explicit_opt_out_for_the_agents_own_guardrails() -> None:
    agent = Agent(
        name="A",
        instructions="i",
        model=fake(SECRET_REPLY),
        output_guardrails=[redact()],
        stream_guard="passthrough",
    )
    events, _ = await collect(Runner().stream_async(agent, "go", stream_text=True))
    assert "".join(deltas(events)) == SECRET_REPLY  # today's behaviour, chosen explicitly
    assert next(e for e in events if e.type == T.FINAL_OUTPUT).data["output"] == CHECKED_REPLY


# ----------------------------------------------------------------------------- chunked


def chunked(chunk: int = 12, overlap: int = 32) -> StreamGuard:
    return StreamGuard(mode="chunked", chunk_chars=chunk, overlap_chars=overlap)


async def test_chunked_never_shows_a_card_split_across_deltas_and_windows() -> None:
    reply = ("Filler text here. " * 3) + f"Card {CARD} done. " + ("More text. " * 4)
    for size in (1, 2, 3, 5, 7):
        agent = Agent(
            name="A",
            instructions="i",
            model=fake(reply, chunk_size=size),
            output_guardrails=[redact()],
            stream_guard=chunked(),
        )
        events, error = await collect(Runner().stream_async(agent, "go", stream_text=True))
        assert error is None
        shown = deltas(events)
        assert len(shown) > 2  # really streamed in windows
        assert all(e.data.get("checked") is True for e in events if e.type == T.TEXT_DELTA)
        assert all(card_free(piece) for piece in shown), shown
        final = next(e for e in events if e.type == T.FINAL_OUTPUT).data["output"]
        assert "".join(shown) == final
        assert "[REDACTED:card]" in final and card_free(final)
        assert next(e for e in events if e.type == T.MODEL_STARTED).data["stream_guard"] == (
            "chunked"
        )


async def test_chunked_trip_never_shows_the_failing_window() -> None:
    reply = "Safe opening words. " * 3 + "FORBIDDEN plan details. " + "tail " * 10

    def no_forbidden(ctx: Any, output: Any) -> GuardrailResult:
        return GuardrailResult(tripwire="FORB" in str(output))

    agent = Agent(
        name="A",
        instructions="i",
        model=fake(reply),
        output_guardrails=[no_forbidden],
        stream_guard=chunked(chunk=10, overlap=32),
    )
    events, error = await collect(Runner().stream_async(agent, "go", stream_text=True))
    assert isinstance(error, GuardrailTripwireTriggered)
    shown = "".join(deltas(events))
    assert shown and "FORB" not in shown and "F" not in shown.replace("Safe", "")
    assert reply.startswith(shown)
    assert kinds(events)[-3:] == [T.GUARDRAIL_DECISION, T.GUARDRAIL_TRIPPED, T.RUN_FAILED]
    tripped = events[-2].data
    assert tripped["phase"] == "chunk" and tripped["shown"] is True
    assert tripped["stream_guard"] == "chunked"
    assert not any(e.type == T.FINAL_OUTPUT for e in events)


async def test_chunked_rewrite_shows_only_the_rewritten_window() -> None:
    def mask_names(ctx: Any, output: Any) -> GuardrailResult:
        text = str(output)
        return (
            GuardrailResult.rewritten(text.replace("Ravi", "[name]"))
            if "Ravi" in text
            else GuardrailResult.passed()
        )

    reply = "Hello there, the customer Ravi called twice today about the order. " + (
        "More words follow here. " * 4
    )
    agent = Agent(
        name="A",
        instructions="i",
        model=fake(reply),
        output_guardrails=[mask_names],
        stream_guard=chunked(chunk=8, overlap=32),
    )
    events, error = await collect(Runner().stream_async(agent, "go", stream_text=True))
    assert error is None
    shown = "".join(deltas(events))
    assert "Ravi" not in shown and "Rav" not in shown
    assert shown == reply.replace("Ravi", "[name]")
    result_text = events[-1].data["output"]
    assert result_text == shown


async def test_chunked_fails_closed_when_the_final_check_rewrites_shown_text() -> None:
    reply = "A first sentence that will be shown. And a second one."

    def whole_answer_rewriter(ctx: Any, output: Any) -> GuardrailResult:
        if output == reply:  # only the whole answer, never a window
            return GuardrailResult.rewritten("Something else entirely.")
        return GuardrailResult.passed()

    agent = Agent(
        name="A",
        instructions="i",
        model=fake(reply),
        output_guardrails=[whole_answer_rewriter],
        stream_guard=chunked(chunk=10, overlap=32),
    )
    events, error = await collect(Runner().stream_async(agent, "go", stream_text=True))
    assert isinstance(error, GuardrailTripwireTriggered) and error.guardrail == "stream_guard"
    shown = "".join(deltas(events))
    assert "Something else" not in shown and reply.startswith(shown)
    tripped = next(e for e in events if e.type == T.GUARDRAIL_TRIPPED).data
    assert tripped["phase"] == "final" and tripped["guardrail"] == "stream_guard"
    assert kinds(events)[-1] == T.RUN_FAILED


async def test_chunked_checks_text_before_tool_calls_and_shows_it_once_it_passed() -> None:
    @tool
    def lookup(order: str) -> str:
        """Look an order up."""
        return "shipped"

    model = FakeModel(
        [ModelTurnWithText(f"Checking card {CARD} now.", "lookup"), "It shipped."],
        stream=True,
        chunk_size=4,
    )
    agent = Agent(
        name="A",
        instructions="i",
        model=model,
        tools=[lookup],
        output_guardrails=[redact()],
        stream_guard=chunked(chunk=6, overlap=32),
    )
    events, error = await collect(Runner().stream_async(agent, "status", stream_text=True))
    assert error is None
    turn1 = "".join(
        e.data["text"] for e in events if e.type == T.TEXT_DELTA and e.data["turn"] == 1
    )
    assert turn1 == "Checking card [REDACTED:card] now."
    turn2 = "".join(
        e.data["text"] for e in events if e.type == T.TEXT_DELTA and e.data["turn"] == 2
    )
    assert turn2 == "It shipped."


class Refund(BaseModel):
    amount: int


async def test_chunked_typed_agent_holds_and_streams_no_partial_output() -> None:
    def ok(ctx: Any, output: Any) -> GuardrailResult:
        return GuardrailResult.passed()

    agent = Agent(
        name="A",
        instructions="i",
        model=fake('{"amount": 4111}'),
        output_type=Refund,
        output_guardrails=[ok],
        stream_guard="chunked",
    )
    events, error = await collect(Runner().stream_async(agent, "go", stream_text=True))
    assert error is None
    assert deltas(events) == [] and not any(e.type == T.PARTIAL_OUTPUT for e in events)
    assert next(e for e in events if e.type == T.MODEL_STARTED).data["stream_guard"] == "hold"


def test_stream_guard_validation() -> None:
    bad: Any
    for bad in ("stream", 3, None):
        with pytest.raises(SDKValidationError):
            Agent(name="A", instructions="i", model=FakeModel(["x"]), stream_guard=bad)
    kwargs: dict[str, Any]
    for kwargs in ({"chunk_chars": 0}, {"overlap_chars": -1}, {"chunk_chars": True}):
        with pytest.raises(SDKValidationError):
            StreamGuard(mode="chunked", **kwargs)
    agent = Agent(name="A", instructions="i", model=FakeModel(["x"]), stream_guard="chunked")
    assert agent.stream_guard == StreamGuard(mode="chunked")
    assert agent.clone(stream_guard="hold").stream_guard.mode == "hold"


# ---------------------------------------------------- inherited and default policy hooks


class ScrubPolicy(AgentHooks):
    """A policy that checks every final answer through the hooks contribution point."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def output_guardrails(self, ctx: Any, agent: Any) -> Sequence[Any]:
        self.asked.append(agent.name)
        return [redact()]


async def test_default_policy_output_guardrails_hold_the_stream() -> None:
    policy = ScrubPolicy()
    set_default_hooks(policy)
    agent = Agent(name="Plain", instructions="i", model=fake(SECRET_REPLY))
    events, error = await collect(Runner().stream_async(agent, "go", stream_text=True))
    assert error is None
    assert deltas(events) == []
    assert events[-1].data["output"] == CHECKED_REPLY
    assert "Plain" in policy.asked


async def test_policy_guardrails_cannot_be_opted_out_with_passthrough() -> None:
    set_default_hooks(ScrubPolicy())
    agent = Agent(
        name="Plain", instructions="i", model=fake(SECRET_REPLY), stream_guard="passthrough"
    )
    events, _ = await collect(Runner().stream_async(agent, "go", stream_text=True))
    assert deltas(events) == []
    assert next(e for e in events if e.type == T.MODEL_STARTED).data["stream_guard"] == "hold"


async def test_policy_guardrails_stream_in_checked_windows_when_the_agent_opts_in() -> None:
    set_default_hooks(ScrubPolicy())
    reply = "Some words first. " * 2 + f"Card {CARD}. " + "Closing words. " * 2
    agent = Agent(name="Plain", instructions="i", model=fake(reply), stream_guard=chunked())
    events, error = await collect(Runner().stream_async(agent, "go", stream_text=True))
    assert error is None
    assert all(card_free(p) for p in deltas(events)) and len(deltas(events)) > 1
    assert "".join(deltas(events)) == events[-1].data["output"]


async def test_runner_hooks_and_inherited_hooks_count_for_nested_runs() -> None:
    inner = Agent(name="Inner", instructions="i", model=fake(SECRET_REPLY))
    seen: list[list[str]] = []

    @tool
    async def ask_inner(question: str) -> str:
        """Ask the inner agent."""
        events = [e async for e in Runner().stream_async(inner, question, stream_text=True)]
        seen.append(deltas(events))
        return str(events[-1].data["output"])

    outer = Agent(
        name="Outer",
        instructions="i",
        model=FakeModel([[ToolCall(id="c1", name="ask_inner", arguments={"question": "q"})], "ok"]),
        tools=[ask_inner],
    )
    result = await Runner(hooks=[ScrubPolicy()]).run_async(outer, "go")
    assert result.output == "ok"
    assert seen == [[]]  # the nested run inherited the policy and held its reply
    tool_result = next(m.content for m in result.messages if m.role == "tool")
    assert tool_result == CHECKED_REPLY


async def test_handoff_target_follows_its_own_guardrails() -> None:
    target = Agent(
        name="Target", instructions="i", model=fake(SECRET_REPLY), output_guardrails=[redact()]
    )
    root = Agent(
        name="Root",
        instructions="i",
        model=FakeModel([[ToolCall(id="h", name="transfer_to_target", arguments={})]], stream=True),
        handoffs=[target],
    )
    events, error = await collect(Runner().stream_async(root, "go", stream_text=True))
    assert error is None
    assert deltas(events) == []
    assert events[-1].data["output"] == CHECKED_REPLY


async def test_composed_hooks_concatenate_output_guardrails_in_order() -> None:
    order: list[str] = []

    def named(label: str) -> Any:
        def check(ctx: Any, output: Any) -> GuardrailResult:
            order.append(label)
            return GuardrailResult.rewritten(f"{output}+{label}")

        check.__name__ = label
        return check

    class Contributes(AgentHooks):
        def __init__(self, label: str) -> None:
            self.label = label

        def output_guardrails(self, ctx: Any, agent: Any) -> Sequence[Any]:
            return [named(self.label)]

    agent = Agent(
        name="A", instructions="i", model=FakeModel(["x"]), output_guardrails=[named("own")]
    )
    result = await Runner(hooks=ComposedHooks(Contributes("a"), Contributes("b"))).run_async(
        agent, "go"
    )
    assert order == ["own", "a", "b"]
    assert result.output == "x+own+a+b"


@pytest.mark.parametrize("bad", ["not a list", [42], {"a": 1}])
async def test_invalid_hook_output_guardrails_fail_the_run(bad: Any) -> None:
    class Bad(AgentHooks):
        def output_guardrails(self, ctx: Any, agent: Any) -> Any:
            return bad

    agent = Agent(name="A", instructions="i", model=fake("hello"))
    events, error = await collect(Runner(hooks=[Bad()]).stream_async(agent, "go", stream_text=True))
    assert isinstance(error, SDKValidationError)
    assert events[-1].type == T.RUN_FAILED
    assert deltas(events) == []


async def test_async_hook_output_guardrails_are_refused() -> None:
    class Bad(AgentHooks):
        async def output_guardrails(self, ctx: Any, agent: Any) -> Any:  # type: ignore[override]
            return []

    agent = Agent(name="A", instructions="i", model=FakeModel(["x"]))
    with pytest.raises(SDKValidationError, match="sync"):
        await Runner(hooks=[Bad()]).run_async(agent, "go")


# --------------------------------------------------------------- cancellation and limits


async def test_cancel_while_the_reply_is_held_shows_nothing() -> None:
    token = CancelToken()
    entered = asyncio.Event()

    async def slow(ctx: Any, output: Any) -> GuardrailResult:
        entered.set()
        await asyncio.sleep(30)
        return GuardrailResult.passed()

    agent = Agent(name="A", instructions="i", model=fake(SECRET_REPLY), output_guardrails=[slow])
    seen: list[RunEvent] = []

    async def consume() -> None:
        async for event in Runner().stream_async(agent, "go", stream_text=True, cancel=token):
            seen.append(event)

    task = asyncio.create_task(consume())
    await asyncio.wait_for(entered.wait(), 5)
    token.cancel("user")
    await asyncio.wait_for(task, 5)
    assert seen[-1].type == T.RUN_CANCELLED
    assert deltas(seen) == []
    assert all(CARD not in (m.content or "") for m in seen[-1].data["messages"])


async def test_cancel_during_a_chunk_check_shows_only_windows_that_passed() -> None:
    token = CancelToken()
    calls = 0

    async def second_window_waits(ctx: Any, output: Any) -> GuardrailResult:
        nonlocal calls
        calls += 1
        if calls == 2:
            token.cancel("user")
            await asyncio.sleep(30)
        return GuardrailResult.passed()

    reply = "abcdefghij" * 10
    agent = Agent(
        name="A",
        instructions="i",
        model=fake(reply, chunk_size=5),
        output_guardrails=[second_window_waits],
        stream_guard=chunked(chunk=40, overlap=32),
    )
    events = [e async for e in Runner().stream_async(agent, "go", stream_text=True, cancel=token)]
    assert events[-1].type == T.RUN_CANCELLED
    assert deltas(events) == ["abcdefgh"]  # the first window minus its held-back overlap


async def test_a_window_guardrail_error_fails_like_a_final_one() -> None:
    def broken(ctx: Any, output: Any) -> GuardrailResult:
        raise ValueError("guardrail bug")

    agent = Agent(
        name="A",
        instructions="i",
        model=fake("x" * 40),
        output_guardrails=[broken],
        stream_guard=chunked(chunk=5, overlap=32),
    )
    events, error = await collect(Runner().stream_async(agent, "go", stream_text=True))
    assert isinstance(error, ValueError)
    assert deltas(events) == []
    decision = next(e for e in events if e.type == T.GUARDRAIL_DECISION).data
    assert decision["action"] == "fail" and decision["error_type"] == "ValueError"


async def test_judge_usage_of_window_checks_counts_for_the_run() -> None:
    from nexura_agents import Usage

    calls = 0

    def costly(ctx: Any, output: Any) -> GuardrailResult:
        nonlocal calls
        calls += 1
        ctx.add_usage(Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2))
        return GuardrailResult.passed()

    def run_with(guardrails: list[Any]) -> Any:
        return Agent(
            name="A",
            instructions="i",
            model=fake("y" * 120),
            output_guardrails=guardrails,
            stream_guard=chunked(chunk=10, overlap=32),
        )

    baseline = [e async for e in Runner().stream_async(run_with([]), "go", stream_text=True)]
    events = [e async for e in Runner().stream_async(run_with([costly]), "go", stream_text=True)]
    assert len(deltas(events)) >= 3
    # Every window check plus the final whole-answer check is counted.
    assert calls >= len(deltas(events))
    base = baseline[-1].data["usage"].total_tokens or 0  # the fake model reports none
    extra = (events[-1].data["usage"].total_tokens or 0) - base
    assert extra == 2 * calls
