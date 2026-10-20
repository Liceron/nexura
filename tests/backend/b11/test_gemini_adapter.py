"""Unit and acceptance tests for B11: Native Gemini Adapter."""

from __future__ import annotations

import json

import httpx
import pytest
from nexura_runtime.contracts.context import ExecutionContext
from nexura_runtime.providers import CapabilityError, GenerationRequest, StreamCancelledError
from nexura_runtime.providers.gemini import (
    GeminiNativeProvider,
    GeminiProviderModule,
)
from nexura_runtime.types import Message


@pytest.mark.asyncio
async def test_gemini_multi_turn_tool_flow_with_metadata() -> None:
    """Acceptance Case 1: Multi-turn tool flow with functionCall, functionResponse, and thought."""
    turn_counter = 0

    def gemini_handler(request: httpx.Request) -> httpx.Response:
        nonlocal turn_counter
        turn_counter += 1
        data = json.loads(request.content)
        assert "contents" in data

        if turn_counter == 1:
            # Turn 1: Model responds with reasoning thought and functionCall
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "content": {
                                "role": "model",
                                "parts": [
                                    {
                                        "thought": (
                                            "User is asking for foreign exchange rate. "
                                            "Need to call get_forex_rate for INR to USD."
                                        ),
                                        "signature": "sig_thought_99",
                                    },
                                    {
                                        "functionCall": {
                                            "name": "get_forex_rate",
                                            "args": {"from_currency": "USD", "to_currency": "INR"},
                                        }
                                    },
                                ],
                            },
                            "finishReason": "STOP",
                            "index": 0,
                        }
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 25,
                        "candidatesTokenCount": 35,
                        "totalTokenCount": 60,
                    },
                    "modelVersion": "gemini-2.5-flash",
                },
            )
        else:
            # Turn 2: User sends functionResponse; model produces final text
            assert any(
                any("functionResponse" in p for p in c.get("parts", []))
                for c in data.get("contents", [])
            )
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "content": {
                                "role": "model",
                                "parts": [
                                    {
                                        "text": "1 USD is currently equal to ₹83.45 INR.",
                                    }
                                ],
                            },
                            "finishReason": "STOP",
                            "index": 0,
                        }
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 70,
                        "candidatesTokenCount": 15,
                        "totalTokenCount": 85,
                    },
                    "modelVersion": "gemini-2.5-flash",
                },
            )

    provider = GeminiNativeProvider(
        model="gemini-2.5-flash",
        api_key="gm-test-key-12345",
        transport=httpx.MockTransport(gemini_handler),
    )

    # Turn 1
    messages: list[Message] = [
        Message(role="user", content="What is the current USD to INR exchange rate?")
    ]
    tools = [
        {
            "function": {
                "name": "get_forex_rate",
                "description": "Get live exchange rate between two currencies.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "from_currency": {"type": "string"},
                        "to_currency": {"type": "string"},
                    },
                },
            }
        }
    ]

    resp1 = await provider.generate(GenerationRequest(messages=messages, tools=tools))
    assert resp1.message.tool_calls is not None
    assert len(resp1.message.tool_calls) == 1
    tc = resp1.message.tool_calls[0]
    assert tc.name == "get_forex_rate"
    assert tc.arguments["from_currency"] == "USD"
    assert tc.arguments["to_currency"] == "INR"

    # Turn 2: Add tool result
    messages.append(resp1.message)
    messages.append(
        Message(
            role="tool",
            name=tc.name,
            tool_call_id=tc.id,
            content=json.dumps({"rate": 83.45, "timestamp": "2026-10-04T12:00:00Z"}),
        )
    )

    resp2 = await provider.generate(GenerationRequest(messages=messages, tools=tools))
    assert resp2.message.content == "1 USD is currently equal to ₹83.45 INR."
    assert resp2.usage is not None
    assert resp2.usage.total_tokens == 85


@pytest.mark.asyncio
async def test_gemini_streaming_tool_arguments() -> None:
    """Acceptance Case 2: Streaming tool arguments and thought parts via SSE."""
    chunks = [
        {"candidates": [{"content": {"parts": [{"thought": "Thinking..."}]}}]},
        {"candidates": [{"content": {"parts": [{"text": "Calling "}]}}]},
        {"candidates": [{"content": {"parts": [{"text": "tool..."}]}}]},
        {
            "candidates": [
                {"content": {"parts": [{"functionCall": {"name": "lookup", "args": {"id": 101}}}]}}
            ]
        },
        {
            "candidates": [{"finishReason": "STOP"}],
            "usageMetadata": {
                "promptTokenCount": 10,
                "candidatesTokenCount": 15,
                "totalTokenCount": 25,
            },
        },
    ]
    sse_lines = [f"data: {json.dumps(c)}" for c in chunks] + ["data: [DONE]"]
    sse_body = "\n\n".join(sse_lines) + "\n\n"

    def sse_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=sse_body.encode("utf-8"),
        )

    provider = GeminiNativeProvider(
        model="gemini-2.5-flash",
        transport=httpx.MockTransport(sse_handler),
    )

    text_parts: list[str] = []
    tool_deltas: list[str] = []
    finish_reasons: list[str] = []
    total_tokens: int | None = None

    async for delta in provider.stream(
        GenerationRequest(messages=[Message(role="user", content="Ping")])
    ):
        if delta.kind == "text_delta" and delta.text:
            text_parts.append(delta.text)
        elif delta.kind == "tool_call_delta" and delta.tool_name:
            tool_deltas.append(delta.tool_name)
        elif delta.kind == "finish":
            if delta.finish_reason:
                finish_reasons.append(delta.finish_reason)
            if delta.usage:
                total_tokens = delta.usage.total_tokens

    assert "".join(text_parts) == "Calling tool..."
    assert "lookup" in tool_deltas
    assert "stop" in finish_reasons
    assert total_tokens == 25


@pytest.mark.asyncio
async def test_gemini_cli_alias_rejected() -> None:
    """Acceptance Case 3: Rejects coding-agent CLI alias as API model ID."""
    with pytest.raises(CapabilityError) as exc_info:
        GeminiNativeProvider(model="gemini-cli")
    assert "never hardcode coding-agent cli aliases" in exc_info.value.message.lower()

    with pytest.raises(CapabilityError):
        GeminiNativeProvider(model="cli")


@pytest.mark.asyncio
async def test_gemini_unsupported_feature_rejection() -> None:
    """Acceptance Case 4: Unsupported native features reject early."""
    provider = GeminiNativeProvider(model="gemini-2.5-flash")

    req = GenerationRequest(
        messages=[Message(role="user", content="Test")],
        provider_metadata={"unsupported_feature": "custom_tpu_kernel"},
    )
    with pytest.raises(CapabilityError) as exc:
        await provider.generate(req)
    assert "custom_tpu_kernel" in exc.value.message


@pytest.mark.asyncio
async def test_gemini_cancellation_and_stream_cleanup() -> None:
    """Acceptance Case 5: Stream cancellation terminates and cleans up immediately."""

    def infinite_sse_handler(request: httpx.Request) -> httpx.Response:
        sse_chunk = 'data: {"candidates": [{"content": {"parts": [{"text": "tick "}]}}]}\n\n'
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=sse_chunk.encode("utf-8"),
        )

    provider = GeminiNativeProvider(
        model="gemini-2.5-flash",
        transport=httpx.MockTransport(infinite_sse_handler),
    )

    cancelled_ctx = ExecutionContext(
        run_id="run_gemini_cancel",
        node_id="node_cancel",
        is_cancelled=True,
    )

    with pytest.raises(StreamCancelledError):
        async for _ in provider.stream(
            GenerationRequest(messages=[Message(role="user", content="Infinite")]),
            context=cancelled_ctx,
        ):
            pass


@pytest.mark.asyncio
async def test_gemini_unknown_usage() -> None:
    """Acceptance Case 6: Missing usageMetadata remains None."""

    def no_usage_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {"parts": [{"text": "No tokens reported"}], "role": "model"},
                        "finishReason": "STOP",
                    }
                ]
            },
        )

    provider = GeminiNativeProvider(
        model="gemini-2.5-flash",
        transport=httpx.MockTransport(no_usage_handler),
    )

    resp = await provider.generate(GenerationRequest(messages=[Message(role="user", content="Hi")]))
    assert resp.usage is None


@pytest.mark.asyncio
async def test_gemini_provider_module_b00_contract() -> None:
    """Acceptance Case 7: B00/B01 module contract compliance."""

    def gemini_mock(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {"parts": [{"text": "Gemini contract output"}], "role": "model"},
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 5,
                    "candidatesTokenCount": 4,
                    "totalTokenCount": 9,
                },
            },
        )

    provider = GeminiNativeProvider(
        model="gemini-2.5-flash",
        transport=httpx.MockTransport(gemini_mock),
    )
    module = GeminiProviderModule(provider=provider)
    ctx = ExecutionContext(run_id="run_gemini_b00", node_id="node_b00")

    result = await module.execute(
        ctx,
        {
            "operation": "generate",
            "messages": [{"role": "user", "content": "Ping module"}],
        },
        {},
    )
    assert result.status == "success"
    assert result.outputs["content"] == "Gemini contract output"
    assert result.usage is not None
    assert result.usage.total_tokens == 9
