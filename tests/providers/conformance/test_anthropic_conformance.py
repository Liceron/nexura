"""Anthropic Messages API conformance (``AnthropicNativeProvider``)."""

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
from nexura_agents.errors import ProviderAuthError
from nexura_agents.providers.anthropic import AnthropicNativeProvider
from nexura_agents.providers.anthropic.provider import supports_output_format
from nexura_agents.providers.errors import (
    ProviderClientError,
    ProviderContextLengthError,
    ProviderRateLimitedError,
    ProviderServerError,
)
from nexura_agents.providers.types import GenerationRequest
from nexura_agents.types import Message

P = "anthropic"
MODEL = "claude-haiku-5-5"
KEY = "sk-ant-api03-conformance000000000000000000"


def provider(replay: Replay, model: str = MODEL) -> AnthropicNativeProvider:
    return AnthropicNativeProvider(model, api_key=KEY, transport=replay.transport)


def ask(text: str = "What is the refund window?", **kw: object) -> GenerationRequest:
    return GenerationRequest(
        messages=[Message(role="system", content="Be brief."), Message(role="user", content=text)],
        **kw,  # type: ignore[arg-type]
    )


async def test_plain_reply_and_request_shape() -> None:
    replay = Replay(P, "text")
    resp = await provider(replay).generate(ask(max_tokens=256, reasoning_effort="low"))
    assert resp.message.content == "Refunds are accepted within 30 days."
    assert resp.finish_reason == "stop"
    req = replay.requests[0]
    assert req.url.path == "/v1/messages"
    assert req.headers["x-api-key"] == KEY
    assert req.headers["anthropic-version"] == "2023-06-01"
    body = replay.body()
    assert body["model"] == MODEL and body["max_tokens"] == 256
    assert body["system"] == "Be brief."
    assert body["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "What is the refund window?"}]}
    ]
    # Claude 4.6+ steer thinking with output_config.effort (effort docs).
    assert body["output_config"] == {"effort": "low"}


def test_default_model_is_a_current_model() -> None:
    # claude-3-5-sonnet-20241022 (the old default) is retired (models overview, 2026-10-21).
    assert AnthropicNativeProvider(api_key="k").model == "claude-haiku-5-5"


async def test_usage_counts_cache_reads_and_writes_as_input() -> None:
    ctx, budget = priced_context(MODEL)
    resp = await provider(Replay(P, "text")).generate(ask(), context=ctx)
    u = resp.usage
    assert u is not None
    # input_tokens (50) is only the part after the last cache breakpoint.
    assert u.prompt_tokens == 50 + 248 + 1800
    assert u.cache_read_input_tokens == u.cached_prompt_tokens == 1800
    assert u.cache_creation_input_tokens == 248
    assert u.completion_tokens == 503 and u.total_tokens == 50 + 248 + 1800 + 503
    assert_close(spent(budget), expected_cost(uncached=50, cached=1800, written=248, output=503))


async def test_parallel_tool_use_round_trip_keeps_thinking_blocks() -> None:
    replay = Replay(P, "tool_use_parallel", "tool_final")
    p = provider(replay)
    first = await p.generate(ask("Weather in Paris and London?", tools=[WEATHER_TOOL]))
    calls = first.message.tool_calls or []
    assert first.finish_reason == "tool_calls"
    assert [(c.id, c.arguments) for c in calls] == [
        ("toolu_01A09q90qw90lq917835lq9", {"location": "Paris"}),
        ("toolu_01PjgRJLbXrXEMZwDNYLnBqk", {"location": "London"}),
    ]
    assert replay.body()["tools"] == [
        {
            "name": "get_weather",
            "description": "Current weather for a city.",
            "input_schema": WEATHER_TOOL["function"]["parameters"],
        }
    ]
    history = [
        Message(role="user", content="Weather in Paris and London?"),
        first.message,
        Message(role="tool", tool_call_id=calls[0].id, name="get_weather", content="18C"),
        Message(role="tool", tool_call_id=calls[1].id, name="get_weather", content="14C"),
    ]
    final = await p.generate(GenerationRequest(messages=history, tools=[WEATHER_TOOL]))
    assert final.message.content == "Paris is 18C and London is 14C."
    sent = replay.body(1)["messages"]
    assistant = sent[1]["content"]
    assert [b["type"] for b in assistant] == ["thinking", "text", "tool_use", "tool_use"]
    assert assistant[0]["signature"].startswith("EqQB")
    # Both results go back in one user turn, in call order.
    assert sent[2]["role"] == "user"
    assert [(b["type"], b["tool_use_id"]) for b in sent[2]["content"]] == [
        ("tool_result", calls[0].id),
        ("tool_result", calls[1].id),
    ]


async def test_structured_output_uses_output_config_format_on_current_models() -> None:
    replay = Replay(P, "structured_native")
    p = provider(replay)
    assert p.structured_output_mode == "native" and p.response_schema_with_tools
    resp = await p.generate(ask(schema=ORDER_SCHEMA))
    assert json.loads(resp.message.content or "") == {"order_id": "ORD-7", "status": "delivered"}
    body = replay.body()
    fmt = body["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"]["additionalProperties"] is False
    # Forced tool use is rejected by Opus/Sonnet 5.5; JSON outputs need no tool at all.
    assert "tool_choice" not in body and "tools" not in body


def test_output_format_model_detection() -> None:
    for model in (
        "claude-haiku-5-5",
        "claude-opus-5-5",
        "claude-sonnet-4-5-20250929",
        "claude-fable-5-1",
        "anthropic.claude-sonnet-5",
    ):
        assert supports_output_format(model), model
    for model in ("claude-3-5-sonnet-20241022", "claude-opus-4-20250514", "claude-x", "m"):
        assert not supports_output_format(model), model


async def test_structured_output_falls_back_to_forced_tool_on_old_models() -> None:
    reply = Replay(P, "structured_native")
    p = provider(reply, model="claude-3-5-haiku-20241022")
    assert p.structured_output_mode == "tool" and not p.response_schema_with_tools
    await p.generate(ask(schema=ORDER_SCHEMA))
    body = reply.body()
    assert body["tool_choice"] == {"type": "tool", "name": "structured_output"}
    assert "output_config" not in body


async def test_stream_text_usage_and_ping() -> None:
    replay = Replay(P, "stream_text")
    out = await drain(provider(replay), ask())
    assert out.text == "Hello!"
    assert out.finish_reasons[-1] == "stop"
    assert out.usage is not None
    assert (out.usage.prompt_tokens, out.usage.completion_tokens) == (25, 15)
    assert replay.body()["stream"] is True


async def test_stream_parallel_tool_input_json_deltas() -> None:
    out = await drain(provider(Replay(P, "stream_tool_use")), ask(tools=[WEATHER_TOOL]))
    assert out.text == "Okay, let's check the weather for San Francisco, CA:"
    assert [(c.id, c.arguments) for c in out.tool_calls] == [
        ("toolu_01T1x1fJ34qAmk2tNTrN7Up6", {"location": "San Francisco, CA"}),
        ("toolu_02U2y2gK45rBnl3uOuDO8Vq7", {"location": "London"}),
    ]
    assert out.finish_reasons[-1] == "tool_calls"


async def test_stream_usage_is_cumulative_in_message_delta() -> None:
    out = await drain(provider(Replay(P, "stream_cache_usage")), ask())
    assert out.usage is not None
    assert out.usage.prompt_tokens == 10682 and out.usage.completion_tokens == 510


async def test_stream_structured_output_on_current_models() -> None:
    replay = Replay(P, "stream_text")
    p = provider(replay)
    assert p.streams_response_schema
    await drain(p, ask(schema=ORDER_SCHEMA))
    assert replay.body()["output_config"]["format"]["type"] == "json_schema"


async def test_stream_overloaded_event_is_a_server_error() -> None:
    with pytest.raises(ProviderServerError):
        await drain(provider(Replay(P, "stream_overloaded")), ask())


async def test_stop_reasons() -> None:
    refusal = await provider(Replay(P, "refusal")).generate(ask())
    assert refusal.finish_reason == "refusal"
    full = await provider(Replay(P, "context_window_stop")).generate(ask())
    assert full.finish_reason == "length"  # model_context_window_exceeded is a truncation


async def test_errors() -> None:
    with pytest.raises(ProviderAuthError) as auth:
        await provider(Replay(P, "error_401")).generate(ask())
    assert KEY not in str(auth.value)
    with pytest.raises(ProviderRateLimitedError) as limited:
        await provider(Replay(P, "error_429")).generate(ask())
    assert limited.value.retry_after == 12
    with pytest.raises(ProviderServerError):
        await provider(Replay(P, "error_529")).generate(ask())
    with pytest.raises(ProviderContextLengthError):
        await provider(Replay(P, "error_400_prompt_too_long")).generate(ask())
    with pytest.raises(ProviderClientError):
        await provider(Replay(P, "error_413")).generate(ask())
    with pytest.raises(ProviderServerError):
        await drain(provider(Replay(P, "error_529")), ask())
