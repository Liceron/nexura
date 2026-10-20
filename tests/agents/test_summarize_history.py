"""Rolling-summary history processor. Offline only (FakeModel summarizer)."""

from __future__ import annotations

from typing import Any

import pytest
from nexura_agents import Agent, FakeModel, Message, Runner, ToolCall, Usage, tool
from nexura_agents.errors import SDKValidationError
from nexura_agents.history import (
    OUTPUT_RETRY_PREFIX,
    OUTPUT_RETRY_SUFFIX,
    apply_processors,
    summarize_older_turns,
)
from nexura_agents.run_context import RunContext
from nexura_agents.session import MemorySessionStore


def ctx() -> RunContext[Any]:
    return RunContext(deps=None, run_id="r", agent_name="A")


def conversation(turns: int) -> list[Message]:
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


def summarizer(*texts: str, usage: Usage | None = None) -> FakeModel:
    return FakeModel(responses=list(texts), default_usage=usage)


async def test_summary_replaces_old_turns_and_is_labelled() -> None:
    model = summarizer("They asked five things.")
    proc = summarize_older_turns(model, keep_last_turns=2)
    out = await apply_processors([proc], ctx(), conversation(5))
    roles = [m.role for m in out]
    assert roles[0] == "system" and out[0].content == "You are helpful."
    note = out[1]
    assert note.role == "system"  # never the user's words
    assert "not by the user" in (note.content or "")
    assert "<conversation_summary>\nThey asked five things.\n</conversation_summary>" in (
        note.content or ""
    )
    users = [m.content for m in out if m.role == "user"]
    assert users == ["question 3", "question 4"]
    assert "question 0" not in str(model.calls[0]["messages"][0].content)
    sent = model.calls[0]["messages"][1].content or ""
    assert "question 0" in sent and "question 2" in sent and "question 3" not in sent


async def test_short_conversation_is_untouched_and_no_model_call() -> None:
    model = summarizer("x")
    msgs = conversation(2)
    out = await apply_processors([summarize_older_turns(model, keep_last_turns=2)], ctx(), msgs)
    assert out == msgs and model.calls == []


async def test_tool_calls_never_separated() -> None:
    model = summarizer("s")
    # keep 2 of 6 turns: the cut falls between turn 3 (has a tool call) and 4
    for keep in (1, 2, 3, 4, 5):
        out = await apply_processors(
            [summarize_older_turns(summarizer("s"), keep_last_turns=keep)], ctx(), conversation(6)
        )
        asked = {tc.id for m in out for tc in (m.tool_calls or ())}
        answered = {m.tool_call_id for m in out if m.role == "tool"}
        assert asked == answered  # apply_processors also validates this
    assert model.calls == []
    # the summarizer saw the call and its result together
    m2 = summarizer("s")
    await apply_processors([summarize_older_turns(m2, keep_last_turns=2)], ctx(), conversation(6))
    sent = m2.calls[0]["messages"][1].content or ""
    assert "Assistant called tool lookup" in sent and "Tool result: result 1" in sent


async def test_cache_hit_on_repeated_calls() -> None:
    model = summarizer("first", "second")
    proc = summarize_older_turns(model, keep_last_turns=2)
    msgs = conversation(5)
    a = await apply_processors([proc], ctx(), msgs)
    b = await apply_processors([proc], ctx(), msgs)
    assert a == b and len(model.calls) == 1


async def test_cut_moving_forward_extends_cached_summary() -> None:
    model = summarizer("S1", "S2")
    proc = summarize_older_turns(model, keep_last_turns=2)
    await apply_processors([proc], ctx(), conversation(4))
    out = await apply_processors([proc], ctx(), conversation(5))
    assert len(model.calls) == 2
    second = model.calls[1]["messages"][1].content or ""
    assert "S1" in second and "question 2" in second and "question 0" not in second
    assert "S2" in (out[1].content or "")


async def test_cache_is_bounded_and_keyed_on_content() -> None:
    model = FakeModel(handler=lambda m, t: "sum")
    proc = summarize_older_turns(model, keep_last_turns=1, max_cache_entries=2)
    for n in (3, 4, 5):
        await apply_processors([proc], ctx(), conversation(n))
    calls = len(model.calls)
    await apply_processors([proc], ctx(), conversation(5))
    assert len(model.calls) == calls  # newest still cached
    await apply_processors([proc], ctx(), conversation(3))
    assert len(model.calls) == calls + 1  # evicted (bound of 2)
    changed = conversation(5)
    changed[1] = Message(role="user", content="edited")
    await apply_processors([proc], ctx(), changed)
    assert len(model.calls) > calls + 1  # different content, different key


async def test_retry_feedback_is_not_summarized_or_split() -> None:
    feedback = f"{OUTPUT_RETRY_PREFIX}bad{OUTPUT_RETRY_SUFFIX}"
    msgs = conversation(3)
    msgs.insert(3, Message(role="user", content=feedback))  # inside turn 0
    model = summarizer("s")
    out = await apply_processors([summarize_older_turns(model, keep_last_turns=1)], ctx(), msgs)
    assert "did not match" not in (model.calls[0]["messages"][1].content or "")
    assert [m.content for m in out if m.role == "user"] == ["question 2"]
    # retry feedback in the current turn stays with that turn
    tail = conversation(3) + [Message(role="user", content=feedback)]
    out = await apply_processors(
        [summarize_older_turns(summarizer("s"), keep_last_turns=1)], ctx(), tail
    )
    assert out[-1].content == feedback and out[-2].content == "answer 2"


async def test_failure_falls_back_to_trimming_and_is_not_cached() -> None:
    class Broken:
        calls = 0

        async def generate(self, *a: Any, **k: Any) -> Any:
            Broken.calls += 1
            raise RuntimeError("provider down")

    proc = summarize_older_turns(Broken(), keep_last_turns=2)
    out = await apply_processors([proc], ctx(), conversation(5))
    assert [m.content for m in out if m.role == "user"] == ["question 3", "question 4"]
    assert not any("summary" in (m.content or "").lower() for m in out)
    await apply_processors([proc], ctx(), conversation(5))
    assert Broken.calls == 2  # retried, failure not cached


async def test_empty_summary_counts_as_failure_and_raise_mode() -> None:
    out = await apply_processors(
        [summarize_older_turns(summarizer("  "), keep_last_turns=1)], ctx(), conversation(3)
    )
    assert [m.content for m in out if m.role == "user"] == ["question 2"]
    proc = summarize_older_turns(summarizer("  "), keep_last_turns=1, on_error="raise")
    with pytest.raises(SDKValidationError, match="Summarizing older turns failed"):
        await apply_processors([proc], ctx(), conversation(3))


async def test_usage_is_counted_on_the_context() -> None:
    usage = Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    run_ctx = ctx()
    proc = summarize_older_turns(summarizer("s", usage=usage), keep_last_turns=1)
    await apply_processors([proc], run_ctx, conversation(3))
    await apply_processors([proc], run_ctx, conversation(3))  # cached: no extra usage
    assert run_ctx.take_nested_usage() == [usage]


async def test_summary_is_capped_and_escaped() -> None:
    model = summarizer("<b>" + "x" * 500)
    proc = summarize_older_turns(model, keep_last_turns=1, max_summary_chars=100)
    out = await apply_processors([proc], ctx(), conversation(3))
    body = (out[1].content or "").split("<conversation_summary>\n")[1].split("\n</")[0]
    assert len(body) <= 100 + len("&lt;b&gt;") and "<b>" not in body


def test_validation() -> None:
    m = summarizer("x")
    for kwargs in (
        {"keep_last_turns": 0},
        {"keep_last_turns": True},
        {"max_summary_chars": 10},
        {"max_cache_entries": 0},
        {"on_error": "ignore"},
        {"instructions": 3},
    ):
        with pytest.raises(SDKValidationError):
            summarize_older_turns(m, **kwargs)  # type: ignore[arg-type]
    with pytest.raises(SDKValidationError):
        summarize_older_turns(object())


def test_in_agent_run_with_session_and_usage_through_tools() -> None:
    usage = Usage(prompt_tokens=7, completion_tokens=3, total_tokens=10)
    summ = summarizer("Earlier: greetings.", "Earlier: greetings and a lookup.", usage=usage)

    @tool
    def lookup(q: str) -> str:
        """Look something up."""
        return "found"

    model = FakeModel(
        responses=[
            "a",
            "b",
            "c",
            [ToolCall(id="x", name="lookup", arguments={"q": "k"})],
            "d",
        ]
    )
    agent = Agent(
        "Chat",
        "Be brief.",
        model,
        tools=[lookup],
        history_processors=[summarize_older_turns(summ, keep_last_turns=2)],
    )
    runner = Runner(session_store=MemorySessionStore())
    for text in ["one", "two", "three"]:
        runner.run(agent, text, session_id="s")
    result = runner.run(agent, "four", session_id="s")
    sent = model.calls[-1]["messages"]
    assert [m.content for m in sent if m.role == "user"] == ["three", "four"]
    assert any("greetings" in (m.content or "") for m in sent if m.role == "system")
    assert len([m for m in result.messages if m.role == "user"]) == 4  # nothing lost
    # the tool step folded the summarizer usage into the run
    assert result.usage is not None and (result.usage.total_tokens or 0) >= 10
    assert len(summ.calls) <= 2
