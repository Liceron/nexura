"""History processors: per-call trimming that never changes the stored conversation."""

from __future__ import annotations

from typing import Any

import pytest
from nexura_agents import (
    Agent,
    FakeModel,
    Message,
    Runner,
    ToolCall,
    fit_token_budget,
    keep_last_turns,
    tool,
)
from nexura_agents.errors import SDKValidationError
from nexura_agents.history import apply_processors, message_tokens
from nexura_agents.run_context import RunContext
from nexura_agents.session import MemorySessionStore
from nexura_agents.tokenizers import CharacterTokenEstimator


def conversation(turns: int) -> list[Message]:
    """System message, then ``turns`` user turns; odd turns include a tool call."""
    out = [Message(role="system", content="You are helpful.")]
    for i in range(turns):
        out.append(Message(role="user", content=f"question {i}"))
        if i % 2:
            out.append(
                Message(
                    role="assistant",
                    tool_calls=[ToolCall(id=f"t{i}", name="lookup", arguments={"q": i})],
                )
            )
            out.append(Message(role="tool", content=f"result {i}", tool_call_id=f"t{i}"))
        out.append(Message(role="assistant", content=f"answer {i}"))
    return out


def ctx() -> RunContext[Any]:
    return RunContext(deps=None, run_id="r", agent_name="A")


def contents(messages: list[Message]) -> list[str | None]:
    return [m.content for m in messages]


# ---------------------------------------------------------------- built-ins


def test_keep_last_turns_keeps_system_and_whole_turns() -> None:
    out = keep_last_turns(2)(conversation(5))
    assert out[0].role == "system"
    assert contents(out)[1:] == [
        "question 3",
        None,
        "result 3",
        "answer 3",
        "question 4",
        "answer 4",
    ]


def test_keep_last_turns_with_fewer_turns_keeps_everything() -> None:
    msgs = conversation(2)
    assert keep_last_turns(10)(msgs) == msgs


def test_fit_token_budget_drops_oldest_turns_until_it_fits() -> None:
    est = CharacterTokenEstimator()
    msgs = conversation(6)
    full = sum(message_tokens(m, est) for m in msgs)
    out = fit_token_budget(full // 2, estimator=est)(msgs)
    assert out[0].role == "system"
    assert sum(message_tokens(m, est) for m in out) <= full // 2
    assert out[-1].content == "answer 5"
    assert out[1].role == "user"  # cut at a user message
    assert len(out) < len(msgs)


def test_fit_token_budget_keeps_current_turn_even_when_over_budget() -> None:
    msgs = [Message(role="system", content="s"), Message(role="user", content="x" * 4000)]
    assert fit_token_budget(5)(msgs) == msgs


def test_fit_token_budget_counts_tool_call_arguments() -> None:
    est = CharacterTokenEstimator()
    small = Message(role="assistant", tool_calls=[ToolCall(id="a", name="f", arguments={})])
    big = Message(
        role="assistant", tool_calls=[ToolCall(id="a", name="f", arguments={"q": "y" * 400})]
    )
    assert message_tokens(big, est) > message_tokens(small, est) + 90


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "3"])
def test_builtins_validate_arguments(bad: Any) -> None:
    with pytest.raises(SDKValidationError):
        keep_last_turns(bad)
    with pytest.raises(SDKValidationError):
        fit_token_budget(bad)


# ---------------------------------------------------------------- apply_processors


async def test_processors_run_in_order_sync_async_and_with_context() -> None:
    seen: list[str] = []

    def plain(messages: list[Message]) -> list[Message]:
        seen.append("plain")
        return messages[:-1]

    async def with_ctx(run: RunContext[Any], messages: list[Message]) -> list[Message]:
        seen.append(f"ctx:{run.run_id}")
        return messages

    out = await apply_processors([plain, with_ctx], ctx(), conversation(2))
    assert seen == ["plain", "ctx:r"]
    assert out[-1].role == "tool"  # plain() dropped the last answer


@pytest.mark.parametrize(
    "processor, match",
    [
        (lambda m: None, "must return a list of Message"),
        (lambda m: [*m, "text"], "must return a list of Message"),
        (lambda m: [x for x in m if x.role == "system"], "removed every message"),
        (lambda m: [m[0], m[-2], m[-1]], "dropped the call"),
    ],
)
async def test_invalid_processor_output_is_rejected(processor: Any, match: str) -> None:
    with pytest.raises(SDKValidationError, match=match):
        await apply_processors([processor], ctx(), conversation(2))


def test_agent_validates_history_processors() -> None:
    model = FakeModel(responses=["x"])
    with pytest.raises(SDKValidationError):
        Agent(name="A", instructions="i", model=model, history_processors=keep_last_turns(1))  # type: ignore[arg-type]
    with pytest.raises(SDKValidationError):
        Agent(name="A", instructions="i", model=model, history_processors=["nope"])  # type: ignore[list-item]
    agent = Agent(name="A", instructions="i", model=model)
    assert agent.history_processors == ()


# ---------------------------------------------------------------- in the runner


async def test_runner_sends_trimmed_history_but_session_keeps_everything() -> None:
    model = FakeModel(responses=[f"answer {i}" for i in range(5)])
    agent = Agent(
        name="A", instructions="Be brief.", model=model, history_processors=[keep_last_turns(2)]
    )
    store = MemorySessionStore()
    runner = Runner(session_store=store)
    for i in range(5):
        result = await runner.run_async(agent, f"question {i}", session_id="s")
    sent = model.calls[-1]["messages"]
    assert [m.content for m in sent if m.role == "user"] == ["question 3", "question 4"]
    assert sent[0].role == "system"
    stored = [m.content for m in result.messages if m.role == "user"]
    assert stored == [f"question {i}" for i in range(5)]


async def test_trimming_keeps_tool_calls_of_the_current_turn() -> None:
    @tool
    def lookup(q: str) -> str:
        """Lookup."""
        return f"found {q}"

    script: list[Any] = [
        [ToolCall(id="c1", name="lookup", arguments={"q": "a"})],
        "done",
    ]
    model = FakeModel(responses=script)
    agent = Agent(
        name="A",
        instructions="i",
        model=model,
        tools=[lookup],
        history_processors=[keep_last_turns(1)],
    )
    history = conversation(3)[1:]  # earlier turns, without a system message
    result = await Runner().run_async(agent, "now", history=history)
    assert result.output == "done"
    second = model.calls[1]["messages"]
    assert [m.role for m in second] == ["system", "user", "assistant", "tool"]
    assert second[-1].content == "found a"


async def test_processor_receives_the_run_context() -> None:
    seen: dict[str, Any] = {}

    def spy(run: RunContext[Any], messages: list[Message]) -> list[Message]:
        seen["deps"] = run.deps
        seen["agent"] = run.agent_name
        return messages

    model = FakeModel(responses=["ok"])
    agent = Agent(name="A", instructions="i", model=model, history_processors=[spy])
    await Runner().run_async(agent, "hi", deps={"user": "u1"})
    assert seen == {"deps": {"user": "u1"}, "agent": "A"}


async def test_failing_processor_fails_the_run_clearly() -> None:
    def broken(messages: list[Message]) -> list[Message]:
        raise RuntimeError("processor bug")

    agent = Agent(
        name="A", instructions="i", model=FakeModel(responses=["x"]), history_processors=[broken]
    )
    with pytest.raises(SDKValidationError, match="History processor 'broken' failed") as info:
        await Runner().run_async(agent, "hi")
    assert isinstance(info.value.original_error, RuntimeError)


# ---------------------------------------------------------------- review fixes


async def test_dropping_a_result_but_keeping_its_call_is_rejected() -> None:
    msgs = conversation(2)  # turn 1 has assistant(tool_calls t1), tool(t1), assistant
    keep_call_drop_result = [m for m in msgs if m.role != "tool"]
    with pytest.raises(SDKValidationError, match="dropped its result"):
        await apply_processors([lambda m: keep_call_drop_result], ctx(), msgs)


async def test_parallel_calls_must_keep_every_result() -> None:
    msgs = [
        Message(role="user", content="q"),
        Message(
            role="assistant",
            tool_calls=[
                ToolCall(id="a", name="f", arguments={}),
                ToolCall(id="b", name="f", arguments={}),
            ],
        ),
        Message(role="tool", content="ra", tool_call_id="a"),
        Message(role="tool", content="rb", tool_call_id="b"),
    ]
    with pytest.raises(SDKValidationError, match="'b' but dropped its result"):
        await apply_processors([lambda m: m[:3]], ctx(), msgs)


async def test_calls_already_unanswered_in_the_input_are_not_blamed_on_the_processor() -> None:
    msgs = [
        Message(role="user", content="q"),
        Message(role="assistant", tool_calls=[ToolCall(id="a", name="f", arguments={})]),
    ]
    assert await apply_processors([lambda m: m], ctx(), msgs) == msgs


async def test_output_retry_feedback_stays_in_the_current_turn() -> None:
    from pydantic import BaseModel

    class Out(BaseModel):
        x: int

    model = FakeModel(responses=["not json", '{"x": 1}'])
    agent = Agent(
        name="A",
        instructions="i",
        model=model,
        output_type=Out,
        output_retries=1,
        history_processors=[keep_last_turns(1)],
    )
    result = await Runner().run_async(agent, "ORIGINAL QUESTION", history=conversation(2)[1:])
    assert result.status == "completed"
    retry_call = [m.content for m in model.calls[1]["messages"]]
    assert "ORIGINAL QUESTION" in retry_call and "not json" in retry_call
    assert "question 0" not in retry_call  # older turns are still trimmed
