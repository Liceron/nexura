"""Tests for OpenAIChatModel HTTP adapter using httpx.MockTransport."""

import json

import httpx
import pytest

from nexura_runtime import (
    Message,
    OpenAIChatModel,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderRequestError,
    ProviderResponseError,
)


@pytest.mark.asyncio
async def test_openai_model_tool_calls_parsing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        assert data["model"] == "test-model"
        assert len(data["messages"]) == 1
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-123",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_abc1",
                                    "type": "function",
                                    "function": {
                                        "name": "lookup",
                                        "arguments": '{"query": "Delhi", "limit": 5}',
                                    },
                                },
                                {
                                    "id": "call_abc2",
                                    "type": "function",
                                    "function": {
                                        "name": "get_status",
                                        "arguments": '{"active": true}',
                                    },
                                },
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 15, "completion_tokens": 25, "total_tokens": 40},
            },
        )

    transport = httpx.MockTransport(handler)
    model = OpenAIChatModel(
        model="test-model",
        api_key="sk-test-secret-key",
        transport=transport,
    )

    resp = await model.generate([Message(role="user", content="Lookup Delhi")])
    assert resp.message.role == "assistant"
    assert resp.message.content is None
    assert resp.message.tool_calls is not None
    assert len(resp.message.tool_calls) == 2

    tc1 = resp.message.tool_calls[0]
    assert tc1.id == "call_abc1"
    assert tc1.name == "lookup"
    assert tc1.arguments == {"query": "Delhi", "limit": 5}

    tc2 = resp.message.tool_calls[1]
    assert tc2.id == "call_abc2"
    assert tc2.name == "get_status"
    assert tc2.arguments == {"active": True}

    assert resp.usage is not None
    assert resp.usage.is_reported
    assert resp.usage.total_tokens == 40
    await model.close()


@pytest.mark.asyncio
async def test_openai_model_final_response_and_missing_usage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "Done!"},
                        "finish_reason": "stop",
                    }
                ],
                # Usage key omitted entirely
            },
        )

    transport = httpx.MockTransport(handler)
    model = OpenAIChatModel(model="test-model", transport=transport)

    resp = await model.generate([Message(role="user", content="Hi")])
    assert resp.message.content == "Done!"
    assert resp.message.tool_calls is None
    assert resp.usage is None  # Unknown usage distinguished from fabricated 0
    await model.close()


@pytest.mark.asyncio
async def test_openai_model_rate_limit_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            text='{"error": {"message": "Rate limit exceeded. Please wait."}}',
        )

    transport = httpx.MockTransport(handler)
    model = OpenAIChatModel(model="test-model", transport=transport)

    with pytest.raises(ProviderRateLimitError, match="Rate limit exceeded"):
        await model.generate([Message(role="user", content="Spam")])
    await model.close()


@pytest.mark.asyncio
async def test_openai_model_auth_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="Unauthorized: Invalid API Key")

    transport = httpx.MockTransport(handler)
    model = OpenAIChatModel(model="test-model", api_key="sk-bad-key", transport=transport)

    with pytest.raises(ProviderAuthError, match="HTTP 401"):
        await model.generate([Message(role="user", content="Hi")])
    await model.close()


@pytest.mark.asyncio
async def test_openai_model_server_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Internal Server Error")

    transport = httpx.MockTransport(handler)
    model = OpenAIChatModel(model="test-model", transport=transport)

    with pytest.raises(ProviderError, match="HTTP 500"):
        await model.generate([Message(role="user", content="Hi")])
    await model.close()


@pytest.mark.asyncio
async def test_openai_model_malformed_tool_call_arguments() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "type": "function",
                                    "function": {
                                        "name": "lookup",
                                        "arguments": "{malformed_json: bad",  # Broken JSON
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    model = OpenAIChatModel(model="test-model", transport=transport)

    with pytest.raises(ProviderResponseError, match="Malformed tool call JSON"):
        await model.generate([Message(role="user", content="Trigger bad args")])
    await model.close()


@pytest.mark.asyncio
async def test_openai_model_secret_masking() -> None:
    secret = "sk-live-super-secret-key-998877"
    model = OpenAIChatModel(model="gpt-4o", api_key=secret)

    # Repr masking
    repr_str = repr(model)
    assert secret not in repr_str
    assert "'***'" in repr_str

    # Error masking on network failure
    def failing_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Failed to connect")

    model.transport = httpx.MockTransport(failing_handler)
    with pytest.raises(ProviderRequestError) as exc_info:
        await model.generate([Message(role="user", content="Hi")])

    err_str = str(exc_info.value)
    assert secret not in err_str
    await model.close()


@pytest.mark.asyncio
async def test_openai_model_unicode_safety() -> None:
    indic_prompt = "नमस्ते, मुझे ऑर्डर की स्थिति बताएं। (Tell me order status)"
    indic_response = "आपका ऑर्डर रास्ते में है। (Your order is on the way)"

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        user_text = body["messages"][0]["content"]
        assert user_text == indic_prompt
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": indic_response},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    model = OpenAIChatModel(model="test-model", transport=transport)

    resp = await model.generate([Message(role="user", content=indic_prompt)])
    assert resp.message.content == indic_response
    await model.close()
