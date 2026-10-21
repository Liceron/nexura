"""Gemini API (generateContent) and Vertex AI conformance."""

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
from nexura_agents.providers.gemini import GeminiNativeProvider
from nexura_agents.providers.gemini.types import GEMINI_SIGNATURES_KEY
from nexura_agents.providers.types import GenerationRequest
from nexura_agents.types import Message, ToolCall

P = "gemini"
MODEL = "gemini-3.8-flash"
KEY = "AIzaSyConformance0000000000000000000000"


def provider(replay: Replay, model: str = MODEL) -> GeminiNativeProvider:
    return GeminiNativeProvider(model, api_key=KEY, transport=replay.transport)


def ask(text: str = "What is the refund window?", **kw: object) -> GenerationRequest:
    return GenerationRequest(
        messages=[Message(role="system", content="Be brief."), Message(role="user", content=text)],
        **kw,  # type: ignore[arg-type]
    )


async def test_plain_reply_request_shape_and_thought_summaries_hidden() -> None:
    replay = Replay(P, "text")
    resp = await provider(replay).generate(ask(max_tokens=64))
    # The thought-summary part (thought: true) is never part of the answer.
    assert resp.message.content == "Refunds are accepted within 30 days."
    assert resp.finish_reason == "stop"
    req = replay.requests[0]
    assert req.url.path == f"/v1beta/models/{MODEL}:generateContent"
    assert req.headers["x-goog-api-key"] == KEY
    body = replay.body()
    assert body["systemInstruction"] == {"parts": [{"text": "Be brief."}]}
    assert body["contents"] == [{"role": "user", "parts": [{"text": "What is the refund window?"}]}]
    assert body["generationConfig"] == {"maxOutputTokens": 64}


async def test_usage_includes_tool_prompt_and_thought_tokens() -> None:
    ctx, budget = priced_context(MODEL)
    resp = await provider(Replay(P, "text")).generate(ask(), context=ctx)
    u = resp.usage
    assert u is not None
    assert u.prompt_tokens == 1200 + 30  # promptTokenCount (incl. cached) + toolUsePrompt
    assert u.cached_prompt_tokens == 1024
    assert u.completion_tokens == 12 + 40 and u.reasoning_tokens == 40
    assert u.total_tokens == 1282
    assert_close(spent(budget), expected_cost(uncached=206, cached=1024, output=52))


async def test_parallel_function_calls_keep_ids_and_thought_signature() -> None:
    replay = Replay(P, "function_calls_parallel", "function_final")
    p = provider(replay)
    first = await p.generate(ask("Temperature in Paris and London?", tools=[WEATHER_TOOL]))
    calls = first.message.tool_calls or []
    assert [(c.id, c.name, c.arguments) for c in calls] == [
        ("fc-7q3k", "get_current_temperature", {"location": "Paris"}),
        ("fc-9x1m", "get_current_temperature", {"location": "London"}),
    ]
    assert first.message.provider_data == {GEMINI_SIGNATURES_KEY: {"fc-7q3k": "<Signature_A>"}}
    assert replay.body()["tools"] == [
        {
            "functionDeclarations": [
                {
                    "name": "get_weather",
                    "description": "Current weather for a city.",
                    "parameters": WEATHER_TOOL["function"]["parameters"],
                }
            ]
        }
    ]
    history = [
        Message(role="user", content="Temperature in Paris and London?"),
        first.message,
        Message(role="tool", tool_call_id=calls[0].id, name=calls[0].name, content='{"c": 15}'),
        Message(role="tool", tool_call_id=calls[1].id, name=calls[1].name, content='{"c": 12}'),
    ]
    final = await p.generate(GenerationRequest(messages=history, tools=[WEATHER_TOOL]))
    assert final.message.content == "Paris is 15C and London is 12C."
    contents = replay.body(1)["contents"]
    # Thought-signatures guide: the signature goes back on the first call part; all calls
    # first, then one user turn with every functionResponse.
    assert contents[1] == {
        "role": "model",
        "parts": [
            {
                "functionCall": {
                    "name": "get_current_temperature",
                    "args": {"location": "Paris"},
                    "id": "fc-7q3k",
                },
                "thoughtSignature": "<Signature_A>",
            },
            {
                "functionCall": {
                    "name": "get_current_temperature",
                    "args": {"location": "London"},
                    "id": "fc-9x1m",
                }
            },
        ],
    }
    assert contents[2] == {
        "role": "user",
        "parts": [
            {
                "functionResponse": {
                    "name": "get_current_temperature",
                    "response": {"c": 15},
                    "id": "fc-7q3k",
                }
            },
            {
                "functionResponse": {
                    "name": "get_current_temperature",
                    "response": {"c": 12},
                    "id": "fc-9x1m",
                }
            },
        ],
    }
    assert len(contents) == 3


async def test_streamed_call_signature_is_reattached_next_turn() -> None:
    replay = Replay(P, "stream_function_call", "function_final")
    p = provider(replay)
    out = await drain(p, ask("Check AA100", tools=[WEATHER_TOOL]))
    assert [(c.id, c.arguments) for c in out.tool_calls] == [("fc-s1", {"flight": "AA100"})]
    history = [
        # The same conversation as the streamed request (the record is per conversation).
        *ask("Check AA100").messages,
        Message(role="assistant", tool_calls=out.tool_calls),  # streamed: no provider_data
        Message(role="tool", tool_call_id="fc-s1", name="check_flight", content='{"s": "ok"}'),
    ]
    await p.generate(GenerationRequest(messages=history, tools=[WEATHER_TOOL]))
    part = replay.body(1)["contents"][1]["parts"][0]
    assert part["thoughtSignature"] == "<Signature A>"


async def test_unknown_signature_uses_documented_dummy_on_gemini_3() -> None:
    replay = Replay(P, "function_final")
    history = [
        Message(role="user", content="Check AA100"),
        Message(
            role="assistant",
            tool_calls=[ToolCall(id="call_check_flight_0", name="check_flight", arguments={})],
        ),
        Message(role="tool", tool_call_id="call_check_flight_0", name="check_flight", content="{}"),
    ]
    await provider(replay).generate(GenerationRequest(messages=history))
    part = replay.body()["contents"][1]["parts"][0]
    assert part["thoughtSignature"] == "skip_thought_signature_validator"
    assert "id" not in part["functionCall"]  # made-up ids are never sent to Gemini
    # Gemini 2.5 does not enforce signatures: nothing is invented there.
    replay25 = Replay(P, "function_final")
    await provider(replay25, "gemini-2.5-flash").generate(GenerationRequest(messages=history))
    assert "thoughtSignature" not in replay25.body()["contents"][1]["parts"][0]


async def test_structured_output_request_shape() -> None:
    replay = Replay(P, "structured")
    resp = await provider(replay).generate(ask(schema=ORDER_SCHEMA))
    assert json.loads(resp.message.content or "") == {"order_id": "ORD-7", "status": "delivered"}
    config = replay.body()["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert config["responseSchema"]["required"] == ["order_id", "status"]


async def test_stream_text_hides_thoughts_and_reports_usage() -> None:
    replay = Replay(P, "stream_text")
    out = await drain(provider(replay), ask())
    assert out.text == "1, 2, 3"
    assert out.finish_reasons == ["stop"]
    assert out.usage is not None and out.usage.total_tokens == 31
    assert out.usage.completion_tokens == 7 + 18
    assert replay.requests[0].url.path.endswith(":streamGenerateContent")
    assert replay.requests[0].url.params["alt"] == "sse"


async def test_finish_reasons_and_content_filters() -> None:
    cut = await provider(Replay(P, "max_tokens")).generate(ask())
    assert cut.finish_reason == "length" and cut.message.content == "Refunds are"
    with pytest.raises(ProviderContentFilterError) as blocked:
        await provider(Replay(P, "prompt_blocked")).generate(ask())
    assert blocked.value.reason == "SAFETY"
    with pytest.raises(ProviderContentFilterError):
        await provider(Replay(P, "safety_finish")).generate(ask())
    with pytest.raises(ProviderContentFilterError):
        await drain(provider(Replay(P, "stream_safety_finish")), ask())


async def test_errors() -> None:
    # An invalid key is HTTP 400 INVALID_ARGUMENT with reason API_KEY_INVALID.
    with pytest.raises(ProviderAuthError):
        await provider(Replay(P, "error_400_api_key_invalid")).generate(ask())
    with pytest.raises(ProviderRateLimitedError) as limited:
        await provider(Replay(P, "error_429_per_minute")).generate(ask())
    assert limited.value.retry_after == 37  # from google.rpc.RetryInfo, no header
    assert not isinstance(limited.value, ProviderQuotaExceededError)
    with pytest.raises(ProviderServerError):
        await provider(Replay(P, "error_503")).generate(ask())
    with pytest.raises(ProviderContextLengthError):
        await provider(Replay(P, "error_400_context")).generate(ask())


async def test_per_day_quota_is_not_retried() -> None:
    replay = Replay(P, "error_429_per_day")
    with pytest.raises(ProviderQuotaExceededError) as info:
        await RetryModel(provider(replay), max_attempts=3, max_delay=0).generate(
            [Message(role="user", content="hi")]
        )
    assert isinstance(info.value, ProviderRateLimitError)
    assert "PerDay" in (info.value.code or "")
    assert len(replay.requests) == 1


async def test_vertex_uses_the_same_wire_format() -> None:
    from nexura_vertex import VertexAIProvider

    replay = Replay(P, "function_calls_parallel")
    vertex = VertexAIProvider(
        MODEL, project="demo", token_provider=lambda: "tok", transport=replay.transport
    )
    resp = await vertex.generate(ask(tools=[WEATHER_TOOL]))
    assert [c.id for c in resp.message.tool_calls or []] == ["fc-7q3k", "fc-9x1m"]
    req = replay.requests[0]
    assert req.url.path == (
        f"/v1/projects/demo/locations/global/publishers/google/models/{MODEL}:generateContent"
    )
    assert req.headers["Authorization"] == "Bearer tok"
    assert replay.body()["systemInstruction"] == {"parts": [{"text": "Be brief."}]}
    # Vertex documents the same placeholder for unsigned calls (thought-signatures page,
    # docs.cloud.google.com/vertex-ai/generative-ai/docs/thought-signatures, 2026-10-20).
    assert vertex.unsigned_call_signature == "skip_thought_signature_validator"
    with pytest.raises(ProviderRateLimitedError):
        await VertexAIProvider(
            MODEL,
            project="demo",
            token_provider=lambda: "tok",
            transport=Replay(P, "error_429_per_minute").transport,
        ).generate(ask())
