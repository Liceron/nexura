"""F49: FakeModel streaming, model-request guard and assertion helpers."""

from __future__ import annotations

import httpx
import pytest
from nexura_agents import Agent, FakeModel, RunEventType, Runner, ToolCall, tool
from nexura_agents.models.openai import OpenAIChatModel
from nexura_agents.providers.fake import StreamingFakeProvider
from nexura_agents.providers.openai.provider import OpenAIProvider
from nexura_agents.providers.types import StreamDelta
from nexura_agents.testing import (
    ModelRequestsBlockedError,
    assert_tool_called,
    block_model_requests,
    block_model_requests_from_env,
    collect_events,
    event_types,
    tool_calls,
)


@tool
def lookup(q: str, limit: int = 1) -> str:
    """Lookup."""
    return q


def _script() -> list[object]:
    return [[ToolCall(id="c1", name="lookup", arguments={"q": "abc", "limit": 2})], "Done now."]


def _agent(model: FakeModel) -> Agent:
    return Agent(name="T", instructions="i", model=model, tools=[lookup])


def test_stream_chunks_assemble_text_and_tool_call() -> None:
    model = FakeModel(responses=_script(), stream=True, chunk_size=3)  # type: ignore[arg-type]
    result_events = collect_events(_agent(model), "go", stream_text=True)
    args = "".join(
        e.data["arguments_delta"] for e in result_events if e.type == RunEventType.TOOL_CALL_DELTA
    )
    assert args == '{"q": "abc", "limit": 2}'
    first = next(e for e in result_events if e.type == RunEventType.TOOL_CALL_DELTA)
    assert first.data["id"] == "c1" and first.data["name"] == "lookup"
    chunks = [e.data["text"] for e in result_events if e.type == RunEventType.TEXT_DELTA]
    assert chunks == ["Don", "e n", "ow."]
    done = next(e for e in result_events if e.type == RunEventType.RUN_COMPLETED)
    assert done.data["output"] == "Done now."


def test_stream_and_non_stream_record_same_calls() -> None:
    plain = FakeModel(responses=_script())  # type: ignore[arg-type]
    streamed = FakeModel(responses=_script(), stream=True)  # type: ignore[arg-type]
    r1 = Runner().run(_agent(plain), "go")
    collect_events(_agent(streamed), "go", stream_text=True)
    assert len(plain.calls) == len(streamed.calls) == 2
    for a, b in zip(plain.calls, streamed.calls, strict=True):
        assert a.keys() == b.keys()
        assert [m.content for m in a["messages"]] == [m.content for m in b["messages"]]
        assert a["tools"] == b["tools"]
    assert r1.output == "Done now."


def test_default_does_not_stream() -> None:
    model = FakeModel(responses=["hello world"])
    assert not hasattr(model, "capabilities")
    events = collect_events(_agent(model), "go", stream_text=True)
    assert not [e for e in events if e.type == RunEventType.TEXT_DELTA]


def test_invalid_chunk_size() -> None:
    with pytest.raises(ValueError):
        FakeModel(chunk_size=0)


def _failing_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"network request was made: {request.url}")

    return httpx.MockTransport(handler)


def test_guard_blocks_openai_chat_model() -> None:
    model = OpenAIChatModel(model="m", api_key="sk-fake", transport=_failing_transport())
    with block_model_requests(), pytest.raises(ModelRequestsBlockedError, match="blocked"):
        Agent(name="A", instructions="", model=model).run("hi")


def test_guard_blocks_provider() -> None:
    provider = OpenAIProvider("m", api_key="sk-fake", transport=_failing_transport())
    agent = Agent(name="A", instructions="", model=provider)
    with block_model_requests(), pytest.raises(ModelRequestsBlockedError):
        agent.run("hi")


def test_guard_allows_fakes_and_releases() -> None:
    original = OpenAIChatModel.generate
    guard = block_model_requests()
    try:
        assert OpenAIChatModel.generate is not original
        fake = FakeModel(responses=["ok"])
        assert Agent(name="A", instructions="", model=fake).run("x").output == "ok"
        prov = StreamingFakeProvider("f", canned_responses=["hi"])
        assert Agent(name="S", instructions="", model=prov).run("x").output == "hi"
        deltas = [
            StreamDelta(kind="text_delta", text="yo"),
            StreamDelta(kind="finish", finish_reason="stop"),
        ]
        sprov = StreamingFakeProvider("f", queued_deltas=[deltas])
        stream_agent = Agent(name="S", instructions="", model=sprov)
        events = collect_events(stream_agent, "x", stream_text=True)
        assert [e.data["text"] for e in events if e.type == RunEventType.TEXT_DELTA] == ["yo"]
    finally:
        guard.release()
    guard.release()
    assert OpenAIChatModel.generate is original


def test_guard_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEXURA_BLOCK_MODEL_REQUESTS", raising=False)
    assert block_model_requests_from_env() is None
    monkeypatch.setenv("NEXURA_BLOCK_MODEL_REQUESTS", "1")
    guard = block_model_requests_from_env()
    assert guard is not None
    guard.release()


def test_tool_call_helpers() -> None:
    result = Runner().run(_agent(FakeModel(responses=_script())), "go")  # type: ignore[arg-type]
    assert tool_calls(result) == [("lookup", {"q": "abc", "limit": 2})]
    assert_tool_called(result, "lookup")
    assert_tool_called(result, "lookup", q="abc")
    assert "tool_started" in event_types(result)


def test_assert_tool_called_negative_cases() -> None:
    result = Runner().run(_agent(FakeModel(responses=_script())), "go")  # type: ignore[arg-type]
    with pytest.raises(AssertionError, match="other"):
        assert_tool_called(result, "other")
    with pytest.raises(AssertionError, match="lookup"):
        assert_tool_called(result, "lookup", q="zzz")
    with pytest.raises(AssertionError):
        assert_tool_called(result, "lookup", missing=1)
    plain = Runner().run(Agent(name="N", instructions="", model=FakeModel(responses=["x"])), "go")
    assert tool_calls(plain) == []
    with pytest.raises(AssertionError):
        assert_tool_called(plain, "lookup")
