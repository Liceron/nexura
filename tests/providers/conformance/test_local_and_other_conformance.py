"""Ollama (OpenAI-compatible, local), Sarvam and LiteLLM conformance."""

from __future__ import annotations

from typing import Any

import pytest
from conformance_harness import (
    WEATHER_TOOL,
    Replay,
    assert_close,
    drain,
    expected_cost,
    load,
    priced_context,
    spent,
)
from nexura_agents import LiteLLMModel
from nexura_agents.errors import ProviderAuthError
from nexura_agents.models.infer import infer_model
from nexura_agents.models.openai import OpenAIChatModel
from nexura_agents.models.resilience import RetryModel
from nexura_agents.providers.errors import (
    ProviderContentFilterError,
    ProviderQuotaExceededError,
    ProviderServerError,
)
from nexura_agents.providers.openai import OpenAIProvider
from nexura_agents.providers.sarvam import SarvamProvider
from nexura_agents.providers.types import GenerationRequest
from nexura_agents.types import Message


def ask(text: str = "Hello", **kw: object) -> GenerationRequest:
    return GenerationRequest(messages=[Message(role="user", content=text)], **kw)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- Ollama


def ollama(replay: Replay) -> OpenAIProvider:
    model = infer_model("ollama:qwen3:0.6b")
    assert isinstance(model, OpenAIProvider)
    model.transport = replay.transport
    return model


async def test_ollama_plain_reply_and_request_shape() -> None:
    replay = Replay("ollama", "text")
    resp = await ollama(replay).generate(ask(max_tokens=32))
    assert resp.message.content == "Hello! How can I help you today?"
    assert resp.usage is not None and resp.usage.total_tokens == 29
    req = replay.requests[0]
    assert str(req.url) == "http://localhost:11434/v1/chat/completions"
    body = replay.body()
    assert body["model"] == "qwen3:0.6b"
    # Ollama documents max_tokens only (no max_completion_tokens).
    assert body["max_tokens"] == 32 and "max_completion_tokens" not in body


async def test_ollama_tool_call_and_single_chunk_stream() -> None:
    resp = await ollama(Replay("ollama", "tool_call")).generate(ask(tools=[WEATHER_TOOL]))
    assert [(c.id, c.arguments) for c in resp.message.tool_calls or []] == [
        ("call_n1kq6b2u", {"format": "celsius", "location": "Paris, FR"})
    ]
    out = await drain(ollama(Replay("ollama", "stream_tool_call")), ask(tools=[WEATHER_TOOL]))
    assert [(c.id, c.name) for c in out.tool_calls] == [("call_x9", "get_current_weather")]
    assert out.usage is not None and out.usage.prompt_tokens == 120
    assert "tool_calls" in out.finish_reasons


# --------------------------------------------------------------------------- Sarvam


def sarvam(replay: Replay) -> SarvamProvider:
    return SarvamProvider(api_key="sk_sarvam_conformance_000000", transport=replay.transport)


async def test_sarvam_reply_usage_and_default_model() -> None:
    replay = Replay("sarvam", "text")
    ctx, budget = priced_context("sarvam-105b")
    resp = await sarvam(replay).generate(ask(), context=ctx)
    assert resp.message.content == "नमस्ते! मैं आपकी कैसे मदद कर सकता हूँ?"  # no reasoning
    u = resp.usage
    assert u is not None and (u.prompt_tokens, u.completion_tokens) == (18, 24)
    assert u.reasoning_tokens == 10
    req = replay.requests[0]
    assert req.url.path == "/v1/chat/completions"
    assert req.headers["api-subscription-key"] == "sk_sarvam_conformance_000000"
    # V1 serves only sarvam-105b and sarvam-105b-conversations (the old default was sarvam-2b).
    assert replay.body()["model"] == "sarvam-105b"
    assert_close(spent(budget), expected_cost(uncached=18, output=24))


async def test_sarvam_stream_reports_usage() -> None:
    replay = Replay("sarvam", "stream_text")
    out = await drain(sarvam(replay), ask())
    assert out.text == "नमस्ते!"
    assert out.usage is not None and out.usage.total_tokens == 21
    assert out.usage.reasoning_tokens == 4
    assert replay.body()["stream_options"] == {"include_usage": True}


async def test_sarvam_errors() -> None:
    replay = Replay("sarvam", "error_429_quota")
    with pytest.raises(ProviderQuotaExceededError):
        await RetryModel(sarvam(replay), max_attempts=3, max_delay=0).generate(
            [Message(role="user", content="hi")]
        )
    assert len(replay.requests) == 1
    with pytest.raises(ProviderAuthError):
        await sarvam(Replay("sarvam", "error_403")).generate(ask())
    with pytest.raises(ProviderServerError):
        await drain(sarvam(Replay("sarvam", "error_500")), ask())


async def test_sarvam_truncated_stream_is_an_error() -> None:
    from nexura_agents.errors import ProviderResponseError

    doc = load("sarvam", "stream_text")
    truncated = {**doc, "response": {**doc["response"], "sse": doc["response"]["sse"][:1]}}

    class Cut(Replay):
        def __init__(self) -> None:
            self.docs = [truncated]
            self.requests = []

    with pytest.raises(ProviderResponseError):
        await drain(sarvam(Cut()), ask())


# -------------------------------------------------------------------------- LiteLLM


def _litellm(response: Any) -> tuple[LiteLLMModel, list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []

    async def completion(**kwargs: Any) -> Any:
        calls.append(kwargs)
        if isinstance(response, BaseException):
            raise response
        return response

    return LiteLLMModel("openai/gpt-5.4-nano", completion=completion), calls


async def test_litellm_reply_tool_calls_and_usage() -> None:
    body = load("openai_chat", "tool_calls_parallel")["response"]["json"]
    model, calls = _litellm(body)
    resp = await model.generate([Message(role="user", content="Weather?")], [WEATHER_TOOL])
    assert [c.id for c in resp.message.tool_calls or []] == [
        "call_FthC9qRpsL5kBpwwyw6c7j4k",
        "call_RpEDoB8O0FTL9JoKTuCVFOyR",
    ]
    assert resp.usage is not None
    assert resp.usage.cached_prompt_tokens == 1024 and resp.usage.reasoning_tokens == 32
    assert calls[0]["tools"] == [WEATHER_TOOL]


class _LiteLLMError(Exception):
    def __init__(self, message: str, status_code: int, code: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        if code:
            self.code = code


class RateLimitError(_LiteLLMError):
    pass


class ContentPolicyViolationError(_LiteLLMError):
    pass


async def test_litellm_quota_and_content_policy_errors() -> None:
    model, _ = _litellm(
        RateLimitError(
            "OpenAIException - You exceeded your current quota (insufficient_quota)", 429
        )
    )
    with pytest.raises(ProviderQuotaExceededError):
        await model.generate([Message(role="user", content="hi")])
    model, _ = _litellm(ContentPolicyViolationError("blocked by content policy", 400))
    with pytest.raises(ProviderContentFilterError):
        await model.generate([Message(role="user", content="hi")])


async def test_openai_chat_model_reports_cached_and_reasoning_tokens() -> None:
    replay = Replay("openai_chat", "text")
    model = OpenAIChatModel("gpt-5.4-nano", api_key="k", transport=replay.transport)
    resp = await model.generate([Message(role="user", content="hi")])
    assert resp.usage is not None
    assert resp.usage.cached_prompt_tokens == 1024 and resp.usage.reasoning_tokens == 32
