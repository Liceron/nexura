"""P37 B3 review repros (commit 5870f6f), kept as regression tests with their assertions.

Every test here is offline: httpx.MockTransport (LOCAL FAKE provider replies) or pure
request building. Each test FAILS on 5870f6f and documents the doc URL it is based on.
Copied from .autopilot/p37/review/test_b3_review.py; every test failed before the fixes.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from nexura_agents.providers.anthropic import AnthropicNativeProvider
from nexura_agents.providers.gemini import GeminiNativeProvider
from nexura_agents.providers.types import GenerationRequest
from nexura_agents.types import Message, ToolCall

ANTHROPIC_KEY = "sk-ant-api03-review0000000000000000000000000"


def _anthropic_text_reply(text: str = "ok") -> dict[str, Any]:
    return {
        "id": "msg_review",
        "type": "message",
        "role": "assistant",
        "model": "x",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 5, "output_tokens": 1},
    }


class _Capture:
    """LOCAL FAKE transport: records request bodies, answers with a fixed body."""

    def __init__(self, responder: Any) -> None:
        self.bodies: list[dict[str, Any]] = []
        self.responder = responder

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content or b"{}"))
        return self.responder(request)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


# --------------------------------------------------------------------------------------
# R1. Anthropic: reasoning_effort is copied verbatim into output_config.effort.
# Nexura allows "minimal" (REASONING_EFFORTS); Anthropic effort levels are only
# low|medium|high|xhigh|max -> 400. https://platform.claude.com/docs/en/build-with-claude/effort
# --------------------------------------------------------------------------------------
async def test_r1_anthropic_minimal_effort_is_not_sent_verbatim() -> None:
    cap = _Capture(lambda r: httpx.Response(200, json=_anthropic_text_reply()))
    provider = AnthropicNativeProvider(
        "claude-opus-5-5", api_key=ANTHROPIC_KEY, transport=cap.transport
    )
    await provider.generate(
        GenerationRequest(messages=[Message(role="user", content="hi")], reasoning_effort="minimal")
    )
    effort = (cap.bodies[0].get("output_config") or {}).get("effort")
    assert effort in (None, "low", "medium", "high", "xhigh", "max"), effort


# --------------------------------------------------------------------------------------
# R2. Anthropic: output_config.effort is sent to models that do not support effort
# (Sonnet 4.5, Haiku 4.5, Opus 4.1, 3.x). Before B3 it was warned about and dropped; now
# those models get a 400. Effort supportedModels list: Opus 4.5+, Sonnet 4.6+, Haiku 5.5,
# Fable, Mythos. https://platform.claude.com/docs/en/build-with-claude/effort
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "model", ["claude-sonnet-4-5-20250929", "claude-haiku-4-5-20251001", "claude-opus-4-1"]
)
async def test_r2_anthropic_effort_not_sent_to_models_without_effort(model: str) -> None:
    cap = _Capture(lambda r: httpx.Response(200, json=_anthropic_text_reply()))
    provider = AnthropicNativeProvider(model, api_key=ANTHROPIC_KEY, transport=cap.transport)
    await provider.generate(
        GenerationRequest(messages=[Message(role="user", content="hi")], reasoning_effort="low")
    )
    assert "effort" not in (cap.bodies[0].get("output_config") or {})


# --------------------------------------------------------------------------------------
# R3. Anthropic native structured output, STREAMED, non-object schema: the wire schema is
# wrapped as {"value": X} but stream() never unwraps, while streams_response_schema=True
# makes the runner stream these turns. The streamed final text is '{"value": [...]}',
# which does not validate against the caller's array schema. generate() does unwrap.
# --------------------------------------------------------------------------------------
def _anthropic_sse(text: str) -> httpx.Response:
    events = [
        ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 9}}}),
        (
            "content_block_start",
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 4},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    body = "".join(f"event: {n}\ndata: {json.dumps(d)}\n\n" for n, d in events)
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)


async def test_r3_anthropic_streamed_native_array_schema_is_unwrapped() -> None:
    schema = {"type": "array", "items": {"type": "integer"}}
    cap = _Capture(lambda r: _anthropic_sse('{"value": [1, 2, 3]}'))
    provider = AnthropicNativeProvider(
        "claude-haiku-5-5", api_key=ANTHROPIC_KEY, transport=cap.transport
    )
    assert provider.streams_response_schema is True
    text = ""
    async for d in provider.stream(
        GenerationRequest(messages=[Message(role="user", content="1..3")], schema=schema)
    ):
        if d.kind == "text_delta":
            text += d.text or ""
    assert cap.bodies[0]["output_config"]["format"]["schema"]["properties"]["value"]
    assert json.loads(text) == [1, 2, 3], text


# --------------------------------------------------------------------------------------
# R4. Anthropic native schema conversion turns a map schema (additionalProperties: {schema})
# into {"type": "object", "additionalProperties": false} with no properties: the model can
# only answer {} and the runner accepts {} against the original schema (silent data loss).
# Docs: additionalProperties other than false is unsupported -> such schemas should use the
# tool/prompt path or fail loudly, not be rewritten to an empty object.
# https://platform.claude.com/docs/en/build-with-claude/structured-outputs
# --------------------------------------------------------------------------------------
async def test_r4_anthropic_map_schema_is_not_silently_emptied() -> None:
    schema = {"type": "object", "additionalProperties": {"type": "integer"}}
    cap = _Capture(lambda r: httpx.Response(200, json=_anthropic_text_reply('{"a": 1}')))
    provider = AnthropicNativeProvider(
        "claude-haiku-5-5", api_key=ANTHROPIC_KEY, transport=cap.transport
    )
    try:
        await provider.generate(
            GenerationRequest(messages=[Message(role="user", content="x")], schema=schema)
        )
    except Exception:
        return  # failing loudly is acceptable
    sent = (cap.bodies[0].get("output_config") or {}).get("format", {}).get("schema")
    assert sent != {"type": "object", "additionalProperties": False}, sent


# --------------------------------------------------------------------------------------
# R5. Vertex AI sends NO thoughtSignature for an unsigned Gemini 3 function call, which
# Vertex documents as a 400. Vertex's own thought-signatures page documents
# "skip_thought_signature_validator" for exactly this case (the B3 comment says it is
# documented only for the Gemini API).
# https://docs.cloud.google.com/vertex-ai/generative-ai/docs/thought-signatures
# --------------------------------------------------------------------------------------
def test_r5_vertex_unsigned_gemini3_call_gets_documented_skip_signature() -> None:
    from nexura_vertex import VertexAIProvider

    vertex = VertexAIProvider(
        "gemini-3.8-flash", project="review-proj", token_provider=lambda: "ya29.fake"
    )
    history = [
        Message(role="user", content="weather?"),
        Message(
            role="assistant",
            content=None,
            tool_calls=[ToolCall(id="fc-1", name="get_weather", arguments={"city": "Pune"})],
        ),
        Message(role="tool", content='{"t": 30}', tool_call_id="fc-1", name="get_weather"),
    ]
    body = vertex._request_body(GenerationRequest(messages=history))
    call_part = body["contents"][1]["parts"][0]
    assert call_part.get("thoughtSignature") == "skip_thought_signature_validator", call_part


# --------------------------------------------------------------------------------------
# R6. Gemini signature record keyed by made-up ids. When Gemini omits functionCall.id (the
# REST examples in the thought-signatures guide have none), the adapter invents
# "call_<name>_<n>", which repeats across turns and across conversations sharing one
# provider. Streamed turns get signatures from that record, so conversation A's current
# call is sent with conversation B's signature (a foreign signature on the first call of the
# current turn fails validation -> 400).
# https://ai.google.dev/gemini-api/docs/generate-content/thought-signatures
# --------------------------------------------------------------------------------------
def _gemini_stream_call(signature: str) -> httpx.Response:
    chunk = {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {
                            "functionCall": {"name": "get_weather", "args": {"city": "Pune"}},
                            "thoughtSignature": signature,
                        }
                    ],
                },
                "finishReason": "STOP",
                "index": 0,
            }
        ],
        "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 2, "totalTokenCount": 7},
    }
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        text=f"data: {json.dumps(chunk)}\n\n",
    )


async def test_r6_gemini_streamed_signatures_do_not_leak_between_conversations() -> None:
    replies = iter([_gemini_stream_call("SIG_A"), _gemini_stream_call("SIG_B")])
    cap = _Capture(lambda r: next(replies))
    provider = GeminiNativeProvider(
        "gemini-3.8-flash", api_key="AIza-fake", transport=cap.transport
    )

    async def stream_call(prompt: str) -> str:
        call_id = ""
        async for d in provider.stream(
            GenerationRequest(messages=[Message(role="user", content=prompt)])
        ):
            if d.kind == "tool_call_delta" and d.tool_call_id:
                call_id = d.tool_call_id
        return call_id

    id_a = await stream_call("conversation A")
    id_b = await stream_call("conversation B")
    assert id_a == id_b  # the made-up id repeats

    history_a = [
        Message(role="user", content="conversation A"),
        Message(
            role="assistant",
            tool_calls=[ToolCall(id=id_a, name="get_weather", arguments={"city": "Pune"})],
        ),
        Message(role="tool", content='{"t": 30}', tool_call_id=id_a, name="get_weather"),
    ]
    body = provider._request_body(GenerationRequest(messages=history_a))
    sent = body["contents"][1]["parts"][0].get("thoughtSignature")
    assert sent == "SIG_A", f"conversation A's call was sent with {sent!r}"


# --------------------------------------------------------------------------------------
# R7. OpenAIChatModel (public nexura_agents.models.OpenAIChatModel, default base_url
# api.openai.com) still sends max_tokens; B3 fixed only OpenAIProvider. Reasoning models
# (gpt-5.x, o-series) reject max_tokens; use max_completion_tokens.
# https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create
# --------------------------------------------------------------------------------------
def _openai_chat_reply() -> dict[str, Any]:
    return {
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
    }


async def test_r7_openai_chat_model_uses_max_completion_tokens_on_api_openai_com() -> None:
    from nexura_agents import ModelSettings
    from nexura_agents.models.openai import OpenAIChatModel

    cap = _Capture(lambda r: httpx.Response(200, json=_openai_chat_reply()))
    model = OpenAIChatModel("gpt-5.4-nano", api_key="sk-fake", transport=cap.transport)
    await model.generate(
        [Message(role="user", content="hi")], model_settings=ModelSettings(max_tokens=64)
    )
    body = cap.bodies[0]
    assert "max_tokens" not in body and body.get("max_completion_tokens") == 64, body


# --------------------------------------------------------------------------------------
# R8. OpenAIChatModel does not pass the error body's codes to http_status_error, so a 429
# insufficient_quota is still a retryable ProviderRateLimitedError (RetryModel repeats it).
# B3 bug #2 was fixed only for OpenAIProvider. "Retrying billing, spend, or quota errors
# won't restore API access." https://developers.openai.com/api/docs/guides/error-codes
# --------------------------------------------------------------------------------------
async def test_r8_openai_chat_model_quota_429_is_not_retryable() -> None:
    from nexura_agents.models.openai import OpenAIChatModel
    from nexura_agents.providers.errors import ProviderQuotaExceededError

    body = {
        "error": {
            "message": "You exceeded your current quota.",
            "type": "insufficient_quota",
            "code": "insufficient_quota",
        }
    }
    cap = _Capture(lambda r: httpx.Response(429, json=body))
    model = OpenAIChatModel("gpt-5.4-nano", api_key="sk-fake", transport=cap.transport)
    with pytest.raises(ProviderQuotaExceededError):
        await model.generate([Message(role="user", content="hi")])


# ------------------------------------------------------------------ follow-up checks


async def test_r2_effort_is_warned_and_dropped_for_unsupported_models() -> None:
    cap = _Capture(lambda r: httpx.Response(200, json=_anthropic_text_reply()))
    provider = AnthropicNativeProvider(
        "claude-sonnet-4-5-20250929", api_key=ANTHROPIC_KEY, transport=cap.transport
    )
    with pytest.warns(RuntimeWarning, match="reasoning_effort"):
        await provider.generate(
            GenerationRequest(
                messages=[Message(role="user", content="hi")], reasoning_effort="high"
            )
        )
    assert "output_config" not in cap.bodies[0]


@pytest.mark.parametrize(
    "model", ["claude-opus-4-5-20251101", "claude-sonnet-4-6", "claude-fable-5"]
)
async def test_r2_effort_is_sent_to_models_on_the_effort_page(model: str) -> None:
    cap = _Capture(lambda r: httpx.Response(200, json=_anthropic_text_reply()))
    provider = AnthropicNativeProvider(model, api_key=ANTHROPIC_KEY, transport=cap.transport)
    await provider.generate(
        GenerationRequest(messages=[Message(role="user", content="hi")], reasoning_effort="minimal")
    )
    assert cap.bodies[0]["output_config"]["effort"] == "low"


async def test_r4_map_schema_uses_forced_tool_and_fails_loudly_without_one() -> None:
    from nexura_agents.providers.errors import CapabilityError

    schema = {"type": "object", "additionalProperties": {"type": "integer"}}
    tool_reply = {
        **_anthropic_text_reply(),
        "content": [
            {"type": "tool_use", "id": "tu_1", "name": "structured_output", "input": {"a": 1}}
        ],
        "stop_reason": "tool_use",
    }
    cap = _Capture(lambda r: httpx.Response(200, json=tool_reply))
    haiku = AnthropicNativeProvider(
        "claude-haiku-5-5", api_key=ANTHROPIC_KEY, transport=cap.transport
    )
    resp = await haiku.generate(
        GenerationRequest(messages=[Message(role="user", content="x")], schema=schema)
    )
    assert json.loads(resp.message.content or "") == {"a": 1}
    assert cap.bodies[0]["tools"][0]["input_schema"] == schema
    assert "output_config" not in cap.bodies[0]
    opus = AnthropicNativeProvider(
        "claude-opus-5-5", api_key=ANTHROPIC_KEY, transport=cap.transport
    )
    with pytest.raises(CapabilityError):
        await opus.generate(
            GenerationRequest(messages=[Message(role="user", content="x")], schema=schema)
        )


async def test_r7_openai_chat_model_keeps_max_tokens_for_compatible_servers() -> None:
    from nexura_agents import ModelSettings
    from nexura_agents.models.openai import OpenAIChatModel

    cap = _Capture(lambda r: httpx.Response(200, json=_openai_chat_reply()))
    model = OpenAIChatModel(
        "qwen3", base_url="http://localhost:11434/v1", api_key="ollama", transport=cap.transport
    )
    await model.generate(
        [Message(role="user", content="hi")], model_settings=ModelSettings(max_tokens=64)
    )
    assert cap.bodies[0]["max_tokens"] == 64 and "max_completion_tokens" not in cap.bodies[0]
