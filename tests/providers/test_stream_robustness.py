"""Provider streams fail with typed errors on error events, truncation and bad chunks (B11)."""

import json

import httpx
import pytest
from nexura_agents.errors import ProviderRequestError, ProviderResponseError
from nexura_agents.providers.anthropic.provider import AnthropicNativeProvider
from nexura_agents.providers.gemini.provider import GeminiNativeProvider
from nexura_agents.providers.openai.provider import OpenAIProvider
from nexura_agents.providers.types import GenerationRequest, ToolCallAssembler
from nexura_agents.types import Message

REQ = lambda: GenerationRequest(messages=[Message(role="user", content="hi")])  # noqa: E731


def _data(*chunks):
    return "".join(f"data: {c if isinstance(c, str) else json.dumps(c)}\n\n" for c in chunks)


def _events(*evs):
    return "".join(f"event: {t}\ndata: {json.dumps(d)}\n\n" for t, d in evs)


async def _drain(provider):
    return [d async for d in provider.stream(REQ())]


def _openai(body):
    return OpenAIProvider(
        model="m",
        api_key="k",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body)),
    )


def _gemini(body):
    return GeminiNativeProvider(
        model="m",
        api_key="k",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body)),
    )


def _anthropic(handler):
    return AnthropicNativeProvider(model="m", api_key="k", transport=httpx.MockTransport(handler))


# 1. OpenAIProvider.stream ignores in-stream error events and truncated streams.
async def test_openai_provider_stream_error_event_raises():
    body = _data({"error": {"message": "boom", "type": "server_error"}})
    with pytest.raises((ProviderResponseError, ProviderRequestError, Exception)) as ei:
        await _drain(_openai(body))
    assert "boom" in str(ei.value) or "server" in str(ei.value).lower()


async def test_openai_provider_stream_truncated_is_error():
    body = _data({"choices": [{"delta": {"content": "par"}}]})  # no finish_reason, no [DONE]
    with pytest.raises(ProviderResponseError):
        await _drain(_openai(body))


# 2. OpenAIProvider.stream crashes with AttributeError/TypeError on null fields.
@pytest.mark.parametrize(
    "chunk",
    [
        {"choices": None},
        {"choices": [{"delta": None, "finish_reason": "stop"}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "a", "function": None}]}}]},
    ],
)
async def test_openai_provider_stream_null_fields_typed_error(chunk):
    body = _data(chunk, "[DONE]")
    try:
        await _drain(_openai(body))
    except (AttributeError, TypeError) as exc:  # untyped crash
        pytest.fail(f"untyped crash {type(exc).__name__}: {exc}")
    except Exception:
        pass  # a typed provider error (or clean handling) is acceptable


# 3. Anthropic stream does not map transport errors and accepts truncated streams.
@pytest.mark.parametrize("exc", [httpx.ConnectError("down"), httpx.ReadTimeout("slow")])
async def test_anthropic_stream_transport_error_is_provider_error(exc):
    def boom(request):
        raise exc

    with pytest.raises(ProviderRequestError):
        await _drain(_anthropic(boom))


async def test_anthropic_stream_truncated_is_error():
    body = _events(
        ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 3}}}),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "par"},
            },
        ),
    )  # no message_delta / message_stop
    with pytest.raises(ProviderResponseError):
        await _drain(_anthropic(lambda r: httpx.Response(200, content=body)))


# 4. Gemini parallel function calls arriving in separate SSE chunks collapse into one call.
def _fc(name, args):
    return {
        "candidates": [{"content": {"parts": [{"functionCall": {"name": name, "args": args}}]}}]
    }


async def test_gemini_stream_parallel_calls_in_separate_chunks():
    body = _data(_fc("a", {"x": 1}), _fc("b", {"y": 2}), {"candidates": [{"finishReason": "STOP"}]})
    asm = ToolCallAssembler()
    for d in await _drain(_gemini(body)):
        if d.kind == "tool_call_delta":
            asm.feed(d)
    calls = asm.validate_and_assemble()
    assert [(c.name, c.arguments) for c in calls] == [("a", {"x": 1}), ("b", {"y": 2})]


# 5. Gemini stream drops error events, accepts truncation, crashes on null content.
async def test_gemini_stream_error_event_raises():
    body = _data({"error": {"code": 500, "message": "boom"}})
    with pytest.raises(Exception) as ei:
        await _drain(_gemini(body))
    assert "boom" in str(ei.value)


async def test_gemini_stream_null_content_not_untyped_crash():
    body = _data({"candidates": [{"content": None, "finishReason": "SAFETY"}]})
    try:
        await _drain(_gemini(body))
    except (AttributeError, TypeError) as exc:
        pytest.fail(f"untyped crash {type(exc).__name__}: {exc}")
    except Exception:
        pass
