"""Provider responses must fail before unsafe or ambiguous tool dispatch."""

from typing import Any

import httpx
import pytest

from nexura_runtime import Message, OpenAIChatModel, ProviderError, ProviderResponseError


@pytest.mark.parametrize("status", [400, 429, 500])
async def test_error_body_does_not_leak_into_public_error(status: int) -> None:
    secret = "private-token-and-customer-payload"
    model = OpenAIChatModel(
        "test", transport=httpx.MockTransport(lambda _: httpx.Response(status, text=secret))
    )
    try:
        with pytest.raises(ProviderError) as caught:
            await model.generate([Message(role="user", content="hello")])
        assert secret not in str(caught.value)
        assert str(status) in str(caught.value)
    finally:
        await model.close()


@pytest.mark.parametrize(
    "payload",
    [
        {"choices": "invalid"},
        {"choices": [None]},
        {"choices": [{"message": None}]},
        {"choices": [{"message": {}}]},
        {"choices": [{"message": {"content": {"invalid": True}}}]},
        {"choices": [{"message": {"tool_calls": "invalid"}}]},
    ],
)
async def test_malformed_response_raises_structured_error(payload: dict[str, Any]) -> None:
    model = OpenAIChatModel(
        "test", transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    )
    try:
        with pytest.raises(ProviderResponseError):
            await model.generate([Message(role="user", content="hello")])
    finally:
        await model.close()


@pytest.mark.parametrize("call_id", ["", None, 1, "duplicate"])
async def test_invalid_tool_call_ids_rejected(call_id: Any) -> None:
    calls = [
        {"id": call_id, "function": {"name": "effect", "arguments": "{}"}},
        {"id": "duplicate", "function": {"name": "effect", "arguments": "{}"}},
    ]
    payload = {"choices": [{"message": {"tool_calls": calls}}]}
    model = OpenAIChatModel(
        "test", transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    )
    try:
        with pytest.raises(ProviderResponseError):
            await model.generate([Message(role="user", content="hello")])
    finally:
        await model.close()
