"""Regressions from the F49 review: guard release order, stream parity, strict argument match."""

import asyncio

import pytest
from nexura_agents import Agent, FakeModel, Runner, ToolCall, tool
from nexura_agents.models.openai import OpenAIChatModel
from nexura_agents.testing import assert_tool_called, block_model_requests
from nexura_agents.types import Message


def _first_deltas(model):
    async def go():
        return [d async for d in model.stream([Message(role="user", content="x")])]

    return asyncio.run(go())


def test_non_lifo_release_leaks_block():
    original = OpenAIChatModel.generate
    outer = block_model_requests()
    inner = block_model_requests()
    outer.release()
    inner.release()
    try:
        assert OpenAIChatModel.generate is original  # still blocked: state leaks
    finally:
        OpenAIChatModel.generate = original


def test_provider_data_dropped_when_streaming():
    msg = Message(role="assistant", content="hi", provider_data={"k": "v"})
    plain = asyncio.run(FakeModel(responses=[msg]).generate([Message(role="user", content="x")]))
    assert plain.message.provider_data == {"k": "v"}
    ds = _first_deltas(FakeModel(responses=[msg], stream=True))
    assert any(d.kind == "continuation_delta" for d in ds)


def test_finish_reason_differs_from_generate():
    plain = asyncio.run(FakeModel(responses=["hi"]).generate([Message(role="user", content="x")]))
    last = _first_deltas(FakeModel(responses=["hi"], stream=True))[-1]
    assert plain.finish_reason == last.finish_reason  # None vs "stop"


def test_assert_tool_called_bool_matches_int():
    @tool
    def f(a: int = 1) -> str:
        """f"""
        return "x"

    m = FakeModel(responses=[[ToolCall(id="1", name="f", arguments={"a": 1})], "d"])
    r = Runner().run(Agent(name="a", instructions="", model=m, tools=[f]), "go")
    with pytest.raises(AssertionError):
        assert_tool_called(r, "f", a=True)


async def test_streamed_thinking_blocks_reach_the_next_request() -> None:
    from nexura_agents import ToolCall as Call

    block = {"type": "thinking", "thinking": "plan", "signature": "sig"}
    first = Message(
        role="assistant",
        tool_calls=[Call(id="c1", name="f", arguments={})],
        provider_data={"anthropic_thinking": [block]},
    )

    @tool
    def f() -> str:
        """f"""
        return "ok"

    model = FakeModel(responses=[first, "done"], stream=True)
    agent = Agent(name="a", instructions="", model=model, tools=[f])
    [e async for e in Runner().stream_async(agent, "go", stream_text=True)]
    sent = [m for m in model.calls[1]["messages"] if m.role == "assistant"]
    assert sent[0].provider_data == {"anthropic_thinking": [block]}
