"""Unit and acceptance tests for B10: OpenAI-Compatible and Local Model Adapter."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from nexura_runtime.contracts.context import ExecutionContext
from nexura_runtime.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderResponseError,
)
from nexura_runtime.providers import CapabilityError, GenerationRequest, ProviderCapabilities
from nexura_runtime.providers.openai import OpenAIProvider, OpenAIProviderModule
from nexura_runtime.types import Message


@pytest.mark.asyncio
async def test_openai_mock_non_stream_call() -> None:
    """Acceptance Case 1a: Mock non-stream chat completion with tool calls and usage."""

    def handler(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        assert data["model"] == "gpt-4o"
        assert len(data["messages"]) == 1
        assert "tools" in data
        assert request.headers["Authorization"] == "Bearer sk-test-key-12345"

        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test-001",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "Running analysis...",
                            "tool_calls": [
                                {
                                    "id": "call_stock_01",
                                    "type": "function",
                                    "function": {
                                        "name": "get_stock_price",
                                        "arguments": '{"ticker": "INFY.BO"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 15, "total_tokens": 35},
            },
        )

    provider = OpenAIProvider(
        model="gpt-4o",
        api_key="sk-test-key-12345",
        transport=httpx.MockTransport(handler),
    )

    req = GenerationRequest(
        messages=[Message(role="user", content="Check INFY stock")],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "get_stock_price",
                    "parameters": {"type": "object", "properties": {"ticker": {"type": "string"}}},
                },
            }
        ],
    )

    resp = await provider.generate(req)
    assert resp.message.content == "Running analysis..."
    assert resp.message.tool_calls is not None
    assert len(resp.message.tool_calls) == 1
    tc = resp.message.tool_calls[0]
    assert tc.id == "call_stock_01"
    assert tc.name == "get_stock_price"
    assert tc.arguments == {"ticker": "INFY.BO"}
    assert resp.usage is not None
    assert resp.usage.total_tokens == 35
    assert resp.usage.is_complete is True


@pytest.mark.asyncio
async def test_openai_mock_streamed_call() -> None:
    """Acceptance Case 1b: Mock streaming chat completion with SSE delta parsing."""
    sse_chunks = [
        {"id": "chatcmpl-stream-01", "choices": [{"index": 0, "delta": {"content": "Real-time "}}]},
        {"id": "chatcmpl-stream-01", "choices": [{"index": 0, "delta": {"content": "streaming!"}}]},
        {
            "id": "chatcmpl-stream-01",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_calc",
                                "function": {"name": "calculator", "arguments": '{"val": 42}'},
                            }
                        ]
                    },
                }
            ],
        },
        {
            "id": "chatcmpl-stream-01",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        },
        {
            "id": "chatcmpl-stream-01",
            "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
        },
    ]
    sse_lines = [f"data: {json.dumps(c)}" for c in sse_chunks] + ["data: [DONE]"]
    sse_body = "\n\n".join(sse_lines) + "\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        assert data.get("stream") is True
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=sse_body.encode("utf-8"),
        )

    provider = OpenAIProvider(
        model="gpt-4o-mini",
        api_key="sk-test-stream",
        transport=httpx.MockTransport(handler),
    )

    req = GenerationRequest(messages=[Message(role="user", content="Stream me")])

    text_chunks: list[str] = []
    tool_deltas: list[Any] = []
    continuation_tokens: list[str] = []
    finish_reasons: list[str] = []
    usage_reports: list[Any] = []

    async for delta in provider.stream(req):
        if delta.kind == "text_delta" and delta.text:
            text_chunks.append(delta.text)
        elif delta.kind == "tool_call_delta":
            tool_deltas.append(delta)
        elif delta.kind == "continuation_delta" and delta.continuation:
            continuation_tokens.append(delta.continuation.token)
        elif delta.kind == "finish":
            if delta.finish_reason:
                finish_reasons.append(delta.finish_reason)
            if delta.usage:
                usage_reports.append(delta.usage)

    assert "".join(text_chunks) == "Real-time streaming!"
    assert len(tool_deltas) == 1
    assert tool_deltas[0].tool_call_id == "call_calc"
    assert tool_deltas[0].tool_name == "calculator"
    assert "chatcmpl-stream-01" in continuation_tokens
    assert "stop" in finish_reasons
    assert len(usage_reports) == 1
    assert usage_reports[0].total_tokens == 20


@pytest.mark.asyncio
async def test_openai_error_matrix_and_rate_limits() -> None:
    """Acceptance Case 1c: Error status codes, 429 with Retry-After, and malformed body."""

    # 1. 429 Rate limit with Retry-After header
    def rate_limit_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"Retry-After": "45"},
            json={"error": {"message": "Rate limit reached"}},
        )

    p_429 = OpenAIProvider(
        model="gpt-4o",
        api_key="sk-rate-key",
        transport=httpx.MockTransport(rate_limit_handler),
    )
    with pytest.raises(ProviderRateLimitError) as exc_429:
        await p_429.generate(GenerationRequest(messages=[Message(role="user", content="Hi")]))
    assert "429" in str(exc_429.value)
    assert "Retry-After: 45" in str(exc_429.value)

    # 2. 401 Auth Error
    def auth_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "Invalid API key"}})

    p_401 = OpenAIProvider(
        model="gpt-4o",
        api_key="sk-bad-secret",
        transport=httpx.MockTransport(auth_handler),
    )
    with pytest.raises(ProviderAuthError):
        await p_401.generate(GenerationRequest(messages=[Message(role="user", content="Hi")]))

    # 3. 500 Server Error
    def server_error_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "Internal error"}})

    p_500 = OpenAIProvider(
        model="gpt-4o",
        transport=httpx.MockTransport(server_error_handler),
    )
    with pytest.raises(ProviderError):
        await p_500.generate(GenerationRequest(messages=[Message(role="user", content="Hi")]))

    # 4. Malformed JSON Response Body
    def malformed_body_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>Bad Gateway</html>")

    p_malformed = OpenAIProvider(
        model="gpt-4o",
        transport=httpx.MockTransport(malformed_body_handler),
    )
    with pytest.raises(ProviderResponseError):
        await p_malformed.generate(GenerationRequest(messages=[Message(role="user", content="Hi")]))


@pytest.mark.asyncio
async def test_openai_refused_outputs() -> None:
    """Acceptance Case 1d: Refused outputs are surfaced explicitly."""

    def refusal_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-refused-001",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "refusal": "I cannot provide instructions for creating malware.",
                        },
                        "finish_reason": "refusal",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18},
            },
        )

    provider = OpenAIProvider(
        model="gpt-4o",
        transport=httpx.MockTransport(refusal_handler),
    )

    resp = await provider.generate(
        GenerationRequest(messages=[Message(role="user", content="Generate exploit")])
    )
    assert resp.finish_reason == "refusal"
    assert resp.message.content is not None
    assert "Refusal: I cannot provide instructions" in resp.message.content


@pytest.mark.asyncio
async def test_openai_usage_omissions() -> None:
    """Acceptance Case 1e: Omitted usage remains None rather than fabricated zeros."""

    def no_usage_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-no-usage",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "Response without token counts",
                        },
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    provider = OpenAIProvider(
        model="gpt-4o",
        transport=httpx.MockTransport(no_usage_handler),
    )

    resp = await provider.generate(GenerationRequest(messages=[Message(role="user", content="Hi")]))
    assert resp.usage is None


@pytest.mark.asyncio
async def test_local_compatible_stub() -> None:
    """Acceptance Case 2: Configured for local OpenAI-compatible endpoints (Ollama / vLLM)."""
    recorded_urls: list[str] = []
    recorded_headers: list[dict[str, str]] = []

    def local_handler(request: httpx.Request) -> httpx.Response:
        recorded_urls.append(str(request.url))
        recorded_headers.append(dict(request.headers))
        return httpx.Response(
            200,
            json={
                "id": "ollama-response-1",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "Hello from local Ollama Llama-3!",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
            },
        )

    # Local Ollama endpoint without api_key
    local_provider = OpenAIProvider(
        model="llama3",
        base_url="http://localhost:11434/v1",
        api_key=None,
        transport=httpx.MockTransport(local_handler),
    )

    resp = await local_provider.generate(
        GenerationRequest(messages=[Message(role="user", content="Ping local LLM")])
    )
    assert resp.message.content == "Hello from local Ollama Llama-3!"
    assert recorded_urls[0] == "http://localhost:11434/v1/chat/completions"
    assert "authorization" not in recorded_headers[0]


@pytest.mark.asyncio
async def test_unsupported_native_features_reject_not_downgrade() -> None:
    """Acceptance Case 3: Unsupported features reject with CapabilityError rather than downgrade."""
    # Provider without structured output capability
    dumb_provider = OpenAIProvider(
        model="legacy-llm",
        capabilities=ProviderCapabilities(
            can_generate=True,
            can_stream=False,
            can_structured_output=False,
        ),
    )

    # Request with structured schema must reject early
    with pytest.raises(CapabilityError) as exc_info:
        await dumb_provider.generate(
            GenerationRequest(
                messages=[Message(role="user", content="Output json")],
                schema={"type": "object", "properties": {"answer": {"type": "string"}}},
            )
        )
    assert "structured_output" in exc_info.value.details["capability"]

    # Request with explicit unsupported feature in metadata
    with pytest.raises(CapabilityError) as exc_unsupported:
        await dumb_provider.generate(
            GenerationRequest(
                messages=[Message(role="user", content="Speak")],
                provider_metadata={"unsupported_feature": "audio_speech"},
            )
        )
    assert "audio_speech" in exc_unsupported.value.details["capability"]


@pytest.mark.asyncio
async def test_openai_provider_module_b00_contract() -> None:
    """Acceptance Case 4: B00/B01 module contract compliance."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-module-01",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "Contract execution response"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
            },
        )

    provider = OpenAIProvider(
        model="gpt-4o-mini",
        transport=httpx.MockTransport(handler),
    )
    module = OpenAIProviderModule(provider=provider)
    ctx = ExecutionContext(run_id="run_b10_test", node_id="node_b10")

    result = await module.execute(
        ctx,
        {
            "operation": "generate",
            "messages": [{"role": "user", "content": "Contract ping"}],
        },
        {},
    )
    assert result.status == "success"
    assert result.outputs["content"] == "Contract execution response"
    assert result.usage is not None
    assert result.usage.total_tokens == 12
