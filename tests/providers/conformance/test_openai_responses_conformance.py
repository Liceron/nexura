"""OpenAI Responses API conformance (``OpenAIResponsesProvider``)."""

from __future__ import annotations

import json

import pytest
from conformance_harness import (
    ORDER_SCHEMA,
    WEATHER_TOOL,
    Replay,
    assert_close,
    drain,
    expected_cost,
    priced_context,
    spent,
)
from nexura_agents import Agent, Runner, tool
from nexura_agents.errors import ProviderAuthError, ProviderRateLimitError
from nexura_agents.providers.errors import (
    ProviderContentFilterError,
    ProviderQuotaExceededError,
    ProviderServerError,
)
from nexura_agents.providers.openai import OpenAIResponsesProvider
from nexura_agents.providers.openai.responses import OPENAI_OUTPUT_ITEMS_KEY
from nexura_agents.providers.types import GenerationRequest
from nexura_agents.types import Message

P = "openai_responses"
MODEL = "gpt-5.4-nano"


def provider(replay: Replay) -> OpenAIResponsesProvider:
    return OpenAIResponsesProvider(
        MODEL, api_key="sk-test-responses-000000000000", transport=replay.transport
    )


def ask(text: str = "What is the refund window?", **kw: object) -> GenerationRequest:
    return GenerationRequest(
        messages=[Message(role="system", content="Be brief."), Message(role="user", content=text)],
        **kw,  # type: ignore[arg-type]
    )


async def test_plain_reply_and_request_shape() -> None:
    replay = Replay(P, "text")
    resp = await provider(replay).generate(ask(max_tokens=64, reasoning_effort="low"))
    assert resp.message.content == "Refunds are accepted within 30 days."
    assert resp.finish_reason == "stop"
    assert replay.requests[0].url.path == "/v1/responses"
    body = replay.body()
    assert body["model"] == MODEL
    assert body["instructions"] == "Be brief."
    assert body["input"] == [
        {"type": "message", "role": "user", "content": "What is the refund window?"}
    ]
    assert body["store"] is False
    assert body["max_output_tokens"] == 64
    assert body["reasoning"] == {"effort": "low"}


async def test_usage_and_budget_settlement() -> None:
    ctx, budget = priced_context(MODEL)
    resp = await provider(Replay(P, "text")).generate(ask(), context=ctx)
    u = resp.usage
    assert u is not None
    assert (u.prompt_tokens, u.completion_tokens, u.total_tokens) == (2048, 120, 2168)
    assert u.cached_prompt_tokens == 1536 and u.reasoning_tokens == 64
    assert_close(spent(budget), expected_cost(uncached=512, cached=1536, output=120))


async def test_parallel_tool_round_trip_sends_reasoning_items_back() -> None:
    replay = Replay(P, "tool_calls_parallel", "tool_final")
    p = provider(replay)
    first = await p.generate(ask("Weather in Paris and London?", tools=[WEATHER_TOOL]))
    calls = first.message.tool_calls or []
    assert first.finish_reason == "tool_calls"
    assert [(c.id, c.arguments) for c in calls] == [
        ("call_12345xyz", {"location": "Paris, France"}),
        ("call_67890abc", {"location": "London, UK"}),
    ]
    assert first.message.provider_data is not None
    assert [i["type"] for i in first.message.provider_data[OPENAI_OUTPUT_ITEMS_KEY]] == [
        "reasoning",
        "function_call",
        "function_call",
    ]
    tool_def = replay.body()["tools"][0]
    assert tool_def == {
        "type": "function",
        "name": "get_weather",
        "description": "Current weather for a city.",
        "parameters": WEATHER_TOOL["function"]["parameters"],
        "strict": False,
    }
    history = [
        Message(role="user", content="Weather in Paris and London?"),
        first.message,
        Message(role="tool", tool_call_id=calls[0].id, name="get_weather", content="18C"),
        Message(role="tool", tool_call_id=calls[1].id, name="get_weather", content="14C"),
    ]
    final = await p.generate(GenerationRequest(messages=history, tools=[WEATHER_TOOL]))
    assert final.message.content == "Paris is 18C and London is 14C."
    items = replay.body(1)["input"]
    assert [i["type"] for i in items] == [
        "message",
        "reasoning",
        "function_call",
        "function_call",
        "function_call_output",
        "function_call_output",
    ]
    assert items[1]["encrypted_content"] == "gAAAAABodmbX1wEXAMPLEencrypted=="
    assert "status" not in items[1]
    assert items[2]["call_id"] == "call_12345xyz" and items[2]["id"] == "fc_12345xyz"
    assert items[4] == {"type": "function_call_output", "call_id": "call_12345xyz", "output": "18C"}


async def test_structured_output_uses_text_format() -> None:
    replay = Replay(P, "structured")
    resp = await provider(replay).generate(ask(schema=ORDER_SCHEMA))
    assert json.loads(resp.message.content or "") == {"order_id": "ORD-7", "status": "delivered"}
    fmt = replay.body()["text"]["format"]
    assert fmt["type"] == "json_schema" and fmt["name"] == "output_schema"
    assert fmt["strict"] is True and fmt["schema"]["additionalProperties"] is False


async def test_stream_text_and_usage() -> None:
    replay = Replay(P, "stream_text")
    out = await drain(provider(replay), ask())
    assert out.text == "Hello there"
    assert out.finish_reasons[-1] == "stop"
    assert out.usage is not None and out.usage.total_tokens == 29
    assert replay.body()["stream"] is True


async def test_stream_tool_call_arguments_and_reasoning_kept_for_next_turn() -> None:
    replay = Replay(P, "stream_tool_calls", "tool_final")
    p = provider(replay)
    out = await drain(p, ask("Weather?", tools=[WEATHER_TOOL]))
    assert [(c.id, c.arguments) for c in out.tool_calls] == [
        ("call_1", {"location": "Paris"}),
        ("call_2", {"location": "London"}),
    ]
    assert out.finish_reasons[-1] == "tool_calls"
    # The streamed message has no provider_data; the provider re-attaches the reasoning item.
    history = [
        Message(role="user", content="Weather?"),
        Message(role="assistant", tool_calls=out.tool_calls),
        Message(role="tool", tool_call_id="call_1", content="18C"),
        Message(role="tool", tool_call_id="call_2", content="14C"),
    ]
    await p.generate(GenerationRequest(messages=history, tools=[WEATHER_TOOL]))
    types = [i["type"] for i in replay.body(1)["input"]]
    assert types[:4] == ["message", "reasoning", "function_call", "function_call"]


async def test_agent_run_end_to_end() -> None:
    @tool
    def get_weather(location: str) -> str:
        """Current weather for a city."""
        return "18C" if "Paris" in location else "14C"

    replay = Replay(P, "tool_calls_parallel", "tool_final")
    agent = Agent(name="W", instructions="Be brief.", model=provider(replay), tools=[get_weather])
    result = await Runner().run_async(agent, "Weather in Paris and London?")
    assert result.final_output == "Paris is 18C and London is 14C."
    assert len(replay.requests) == 2


async def test_refusal_and_incomplete_reasons() -> None:
    refusal = await provider(Replay(P, "refusal")).generate(ask())
    assert refusal.finish_reason == "refusal"
    assert refusal.message.content == "Model Refusal: I'm sorry, I can't help with that."
    cut = await provider(Replay(P, "incomplete_max_tokens")).generate(ask())
    assert cut.finish_reason == "length" and cut.message.content == "Refunds are accepted within"
    with pytest.raises(ProviderContentFilterError):
        await provider(Replay(P, "incomplete_content_filter")).generate(ask())


async def test_failed_response_and_stream_errors() -> None:
    with pytest.raises(ProviderServerError):
        await provider(Replay(P, "failed")).generate(ask())
    with pytest.raises(ProviderServerError):
        await drain(provider(Replay(P, "stream_failed")), ask())
    with pytest.raises(ProviderRateLimitError):
        await drain(provider(Replay("openai_responses", "stream_error_rate_limit")), ask())


async def test_http_errors_share_the_chat_mapping() -> None:
    with pytest.raises(ProviderAuthError):
        await provider(Replay("openai_chat", "error_401")).generate(ask())
    with pytest.raises(ProviderQuotaExceededError):
        await provider(Replay("openai_chat", "error_429_quota")).generate(ask())
