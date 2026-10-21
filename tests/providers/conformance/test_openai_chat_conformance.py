"""OpenAI Chat Completions conformance (``OpenAIProvider``) against documented wire shapes."""

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
from nexura_agents.errors import ProviderAuthError, ProviderRateLimitError
from nexura_agents.models.resilience import RetryModel
from nexura_agents.providers.errors import (
    ProviderContentFilterError,
    ProviderContextLengthError,
    ProviderQuotaExceededError,
    ProviderRateLimitedError,
    ProviderServerError,
)
from nexura_agents.providers.openai import OpenAIProvider
from nexura_agents.providers.types import GenerationRequest
from nexura_agents.types import Message

P = "openai_chat"
MODEL = "gpt-5.4-nano"


def provider(replay: Replay, **kw: object) -> OpenAIProvider:
    return OpenAIProvider(
        MODEL, api_key="sk-test-conformance-0000000000", transport=replay.transport, **kw
    )  # type: ignore[arg-type]


def ask(text: str = "What is the refund window?", **kw: object) -> GenerationRequest:
    return GenerationRequest(
        messages=[Message(role="system", content="Be brief."), Message(role="user", content=text)],
        **kw,  # type: ignore[arg-type]
    )


async def test_plain_reply_and_request_shape() -> None:
    replay = Replay(P, "text")
    resp = await provider(replay).generate(ask(max_tokens=64, temperature=0.2))
    assert resp.message.content == "Refunds are accepted within 30 days."
    assert resp.finish_reason == "stop"
    req = replay.requests[0]
    assert req.url.path == "/v1/chat/completions"
    assert req.headers["Authorization"] == "Bearer sk-test-conformance-0000000000"
    body = replay.body()
    assert body["model"] == MODEL
    assert body["messages"] == [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "What is the refund window?"},
    ]
    # max_tokens is deprecated and rejected by reasoning models on api.openai.com.
    assert body["max_completion_tokens"] == 64 and "max_tokens" not in body
    assert body["temperature"] == 0.2
    assert "stream" not in body


async def test_usage_maps_cached_reasoning_and_settles_budget() -> None:
    replay = Replay(P, "text")
    ctx, budget = priced_context(MODEL)
    resp = await provider(replay).generate(ask(), context=ctx)
    u = resp.usage
    assert u is not None
    assert (u.prompt_tokens, u.completion_tokens, u.total_tokens) == (1117, 46, 1163)
    assert u.cached_prompt_tokens == u.cache_read_input_tokens == 1024
    assert u.cache_creation_input_tokens == 0
    assert u.reasoning_tokens == 32  # inside completion_tokens, not added
    # prompt_tokens includes the cached tokens: 93 uncached at the input rate.
    assert_close(spent(budget), expected_cost(uncached=93, cached=1024, output=46))


async def test_parallel_tool_call_round_trip() -> None:
    replay = Replay(P, "tool_calls_parallel", "tool_final")
    p = provider(replay)
    first = await p.generate(ask("Weather in Paris and London?", tools=[WEATHER_TOOL]))
    calls = first.message.tool_calls or []
    assert first.finish_reason == "tool_calls"
    assert [(c.id, c.arguments) for c in calls] == [
        ("call_FthC9qRpsL5kBpwwyw6c7j4k", {"location": "Paris, France"}),
        ("call_RpEDoB8O0FTL9JoKTuCVFOyR", {"location": "London, UK"}),
    ]
    history = [
        Message(role="user", content="Weather in Paris and London?"),
        first.message,
        Message(role="tool", tool_call_id=calls[0].id, name="get_weather", content="18C"),
        Message(role="tool", tool_call_id=calls[1].id, name="get_weather", content="14C"),
    ]
    final = await p.generate(GenerationRequest(messages=history, tools=[WEATHER_TOOL]))
    assert final.message.content == "Paris is 18C and London is 14C."
    sent = replay.body(1)
    assert sent["tools"] == [WEATHER_TOOL]
    assistant = sent["messages"][1]
    assert [c["id"] for c in assistant["tool_calls"]] == [calls[0].id, calls[1].id]
    assert assistant["tool_calls"][0]["function"]["arguments"] == '{"location": "Paris, France"}'
    assert sent["messages"][2] == {
        "role": "tool",
        "content": "18C",
        "tool_call_id": calls[0].id,
        "name": "get_weather",
    }


async def test_tool_choice_mapping() -> None:
    replay = Replay(P, "tool_calls_parallel")
    await provider(replay).generate(ask(tools=[WEATHER_TOOL], tool_choice="get_weather"))
    assert replay.body()["tool_choice"] == {
        "type": "function",
        "function": {"name": "get_weather"},
    }


async def test_structured_output_sends_strict_json_schema() -> None:
    replay = Replay(P, "structured")
    resp = await provider(replay).generate(ask(schema=ORDER_SCHEMA))
    assert json.loads(resp.message.content or "") == {"order_id": "ORD-7", "status": "delivered"}
    fmt = replay.body()["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["schema"]["additionalProperties"] is False


async def test_stream_text_and_usage_chunk() -> None:
    replay = Replay(P, "stream_text")
    ctx, budget = priced_context(MODEL)
    out = await drain(provider(replay), ask(max_tokens=10), context=ctx)
    assert out.text == "Hello there"
    assert "stop" in out.finish_reasons
    assert out.usage is not None and out.usage.total_tokens == 29
    body = replay.body()
    assert body["stream"] is True and body["stream_options"] == {"include_usage": True}
    assert replay.requests[0].headers["Accept"] == "text/event-stream"
    assert_close(spent(budget), expected_cost(uncached=19, output=10))


async def test_stream_parallel_tool_call_arguments() -> None:
    out = await drain(provider(Replay(P, "stream_tool_calls")), ask(tools=[WEATHER_TOOL]))
    assert [(c.id, c.name, c.arguments) for c in out.tool_calls] == [
        ("call_a1", "get_weather", {"location": "Paris"}),
        ("call_b2", "get_weather", {"location": "London"}),
    ]
    assert "tool_calls" in out.finish_reasons


async def test_refusal_is_reported() -> None:
    resp = await provider(Replay(P, "refusal")).generate(ask())
    assert resp.finish_reason == "refusal"
    assert resp.message.content == "Model Refusal: I'm sorry, I can't help with that."


async def test_content_filter_finish_without_content_is_typed() -> None:
    with pytest.raises(ProviderContentFilterError) as info:
        await provider(Replay(P, "content_filter")).generate(ask())
    assert info.value.reason == "content_filter"


async def test_error_auth() -> None:
    replay = Replay(P, "error_401")
    with pytest.raises(ProviderAuthError) as info:
        await provider(replay).generate(ask())
    assert "sk-test-conformance" not in str(info.value)


async def test_error_rate_limit_carries_retry_after() -> None:
    with pytest.raises(ProviderRateLimitedError) as info:
        await provider(Replay(P, "error_429_rate")).generate(ask())
    assert info.value.retry_after == 20
    assert not isinstance(info.value, ProviderQuotaExceededError)


async def test_error_quota_is_not_retried() -> None:
    replay = Replay(P, "error_429_quota")
    model = RetryModel(provider(replay), max_attempts=3, base_delay=0, max_delay=0)
    with pytest.raises(ProviderQuotaExceededError) as info:
        await model.generate([Message(role="user", content="hi")])
    assert isinstance(info.value, ProviderRateLimitError)  # still a 429 for callers
    assert info.value.code == "insufficient_quota"
    assert len(replay.requests) == 1


async def test_error_overloaded_is_server_error_with_retry_after() -> None:
    with pytest.raises(ProviderServerError) as info:
        await provider(Replay(P, "error_503_overloaded")).generate(ask())
    assert info.value.retry_after == 3


async def test_error_context_too_long() -> None:
    with pytest.raises(ProviderContextLengthError):
        await provider(Replay(P, "error_400_context")).generate(ask())


async def test_error_content_filter_rejection() -> None:
    with pytest.raises(ProviderContentFilterError):
        await provider(Replay(P, "error_400_content_filter")).generate(ask())


async def test_stream_errors_use_the_same_mapping() -> None:
    with pytest.raises(ProviderQuotaExceededError):
        await drain(provider(Replay(P, "error_429_quota")), ask())
    with pytest.raises(ProviderServerError):
        await drain(provider(Replay(P, "error_503_overloaded")), ask())
