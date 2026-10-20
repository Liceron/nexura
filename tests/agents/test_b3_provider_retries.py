"""Regression tests: RetryModel retries 429/5xx only and honours Retry-After (b3)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from nexura_agents.errors import ProviderError
from nexura_agents.models.openai import OpenAIChatModel
from nexura_agents.models.resilience import RetryModel
from nexura_agents.providers.anthropic.provider import AnthropicNativeProvider
from nexura_agents.providers.gemini.provider import GeminiNativeProvider
from nexura_agents.providers.openai.provider import OpenAIProvider
from nexura_agents.providers.sarvam.provider import SarvamProvider
from nexura_agents.types import Message

OK = {
    "openai": {
        "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]
    },
    "anthropic": {
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    },
    "gemini": {"candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}]},
}
OK["chatmodel"] = OK["openai"]
OK["sarvam"] = OK["openai"]


def build(kind: str, transport: httpx.AsyncBaseTransport) -> Any:
    kw: dict[str, Any] = {"api_key": "k", "transport": transport}
    if kind == "openai":
        return OpenAIProvider("m", **kw)
    if kind == "chatmodel":
        return OpenAIChatModel("m", **kw)
    if kind == "anthropic":
        return AnthropicNativeProvider("m", **kw)
    if kind == "gemini":
        return GeminiNativeProvider("m", **kw)
    return SarvamProvider("m", **kw)


KINDS = ["openai", "chatmodel", "anthropic", "gemini", "sarvam"]


def flaky(kind: str, status: int, headers: dict[str, str] | None = None):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(status, headers=headers or {}, json={"error": "x"})
        return httpx.Response(200, json=OK[kind])

    return calls, httpx.MockTransport(handler)


async def run(kind: str, status: int, headers=None, sleeps=None, monkeypatch=None):
    calls, transport = flaky(kind, status, headers)
    model = RetryModel(build(kind, transport), max_attempts=2, base_delay=0.5)
    if monkeypatch is not None:

        async def fake_sleep(d: float) -> None:
            sleeps.append(d)

        monkeypatch.setattr("nexura_agents.models.resilience.asyncio.sleep", fake_sleep)
    try:
        await model.generate([Message(role="user", content="hi")])
        err = None
    except ProviderError as e:
        err = e
    return calls["n"], err


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("status", [500, 502, 503])
async def test_5xx_is_retried(kind: str, status: int, monkeypatch: pytest.MonkeyPatch) -> None:
    attempts, err = await run(kind, status, sleeps=[], monkeypatch=monkeypatch)
    assert err is None
    assert attempts == 2


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(("value", "expected"), [("0", 0.0), ("3", 3.0)])
async def test_retry_after_is_honoured(
    kind: str, value: str, expected: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleeps: list[float] = []
    attempts, err = await run(kind, 429, {"Retry-After": value}, sleeps, monkeypatch)
    assert err is None
    assert attempts == 2
    assert sleeps == [expected]


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_client_errors_are_not_retried(
    kind: str, status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts, err = await run(kind, status, sleeps=[], monkeypatch=monkeypatch)
    assert err is not None
    assert attempts == 1
