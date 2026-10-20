"""P19: context-window overflow is classified and can be recovered by trimming old turns.

Each provider's HTTP error goes through ``http_status_error``; a 4xx that says the prompt is
too long becomes ``ProviderContextLengthError``. ``RetryModel(trim_history=N)`` drops the oldest
whole turn (never splitting a tool call from its result) and calls again, up to N times.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from nexura_agents.errors import SDKValidationError
from nexura_agents.models.openai import OpenAIChatModel
from nexura_agents.models.resilience import RetryModel, trim_oldest_turn
from nexura_agents.providers.anthropic.provider import AnthropicNativeProvider
from nexura_agents.providers.errors import (
    ProviderClientError,
    ProviderContextLengthError,
    http_status_error,
)
from nexura_agents.providers.gemini.provider import GeminiNativeProvider
from nexura_agents.providers.openai.provider import OpenAIProvider
from nexura_agents.types import Message, ToolCall

OVERFLOW = {
    "openai": {
        "error": {
            "code": "context_length_exceeded",
            "message": "This model's maximum context length is 8192 tokens.",
        }
    },
    "anthropic": {
        "type": "error",
        "error": {"type": "invalid_request_error", "message": "prompt is too long: 9000 tokens"},
    },
    "gemini": {
        "error": {
            "code": 400,
            "message": "The input token count (9000) exceeds the maximum number of tokens",
        }
    },
}
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
OVERFLOW["chatmodel"] = OVERFLOW["openai"]
KINDS = ["openai", "chatmodel", "anthropic", "gemini"]


def build(kind: str, transport: httpx.AsyncBaseTransport) -> Any:
    kw: dict[str, Any] = {"api_key": "k", "transport": transport}
    if kind == "openai":
        return OpenAIProvider("m", **kw)
    if kind == "chatmodel":
        return OpenAIChatModel("m", **kw)
    if kind == "anthropic":
        return AnthropicNativeProvider("m", **kw)
    return GeminiNativeProvider("m", **kw)


def conversation(turns: int) -> list[Message]:
    msgs = [Message(role="system", content="be brief")]
    for i in range(turns):
        msgs.append(Message(role="user", content=f"question {i}"))
        msgs.append(Message(role="assistant", content=f"answer {i}"))
    msgs.append(Message(role="user", content="latest"))
    return msgs


def overflow_until(kind: str, max_user_turns: int) -> tuple[list[int], httpx.MockTransport]:
    """Reject with a context error while the request holds more than ``max_user_turns``."""
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        items = body.get("messages") or body.get("contents") or []
        users = sum(1 for m in items if m.get("role") == "user")
        seen.append(users)
        if users > max_user_turns:
            return httpx.Response(400, json=OVERFLOW[kind])
        return httpx.Response(200, json=OK[kind])

    return seen, httpx.MockTransport(handler)


@pytest.mark.parametrize(
    "message",
    [
        "This model's maximum context length is 8192 tokens. context_length_exceeded",
        "prompt is too long: 210000 tokens > 200000 maximum",
        "The input token count (1048577) exceeds the maximum number of tokens allowed",
        "Please reduce the length of the messages or completion.",
    ],
)
def test_context_errors_are_classified(message: str) -> None:
    err = http_status_error(400, message)
    assert isinstance(err, ProviderContextLengthError)
    assert isinstance(err, ProviderClientError)  # still a non-transient client error


def test_other_client_errors_are_not_context_errors() -> None:
    err = http_status_error(400, "invalid value for 'temperature'")
    assert type(err) is ProviderClientError
    assert not isinstance(http_status_error(500, "context window"), ProviderContextLengthError)


@pytest.mark.parametrize("kind", KINDS)
async def test_trim_history_recovers_from_overflow(kind: str) -> None:
    seen, transport = overflow_until(kind, max_user_turns=2)
    model = RetryModel(build(kind, transport), max_attempts=1, trim_history=5)
    result = await model.generate(conversation(4))
    assert result is not None
    # One turn dropped per call until the request fits (Gemini also counts the system prompt).
    assert [a - b for a, b in zip(seen, seen[1:], strict=False)] == [1] * (len(seen) - 1)
    assert seen[-1] <= 2 < seen[-2]
    assert model.turns_trimmed == len(seen) - 1


@pytest.mark.parametrize("kind", KINDS)
async def test_without_trim_history_the_error_is_raised(kind: str) -> None:
    seen, transport = overflow_until(kind, max_user_turns=2)
    with pytest.raises(ProviderContextLengthError):
        await RetryModel(build(kind, transport), max_attempts=3).generate(conversation(4))
    assert len(seen) == 1  # not retried: the same request would fail again


async def test_trimming_stops_at_the_limit_and_at_the_last_turn() -> None:
    seen, transport = overflow_until("openai", max_user_turns=0)
    model = RetryModel(build("openai", transport), trim_history=2)
    with pytest.raises(ProviderContextLengthError):
        await model.generate(conversation(4))
    assert seen == [5, 4, 3]
    seen2, transport2 = overflow_until("openai", max_user_turns=0)
    with pytest.raises(ProviderContextLengthError):
        await RetryModel(build("openai", transport2), trim_history=10).generate(conversation(1))
    assert seen2 == [2, 1]  # the latest turn is never dropped


def test_trim_keeps_system_and_never_splits_a_tool_call() -> None:
    call = ToolCall(id="c1", name="lookup", arguments={})
    msgs = [
        Message(role="system", content="s"),
        Message(role="user", content="u1"),
        Message(role="assistant", tool_calls=[call]),
        Message(role="tool", content="r1", tool_call_id="c1"),
        Message(role="assistant", content="a1"),
        Message(role="user", content="u2"),
    ]
    trimmed = trim_oldest_turn(msgs)
    assert trimmed is not None
    assert [(m.role, m.content) for m in trimmed] == [("system", "s"), ("user", "u2")]
    assert trim_oldest_turn(trimmed) is None


def test_trim_history_must_not_be_negative() -> None:
    with pytest.raises(SDKValidationError):
        RetryModel(
            build("openai", httpx.MockTransport(lambda r: httpx.Response(200))), trim_history=-1
        )


# --- audit13 PROV: streaming overflow, RetryModel.stream trim, new wordings -----------------


class _UnreadBody(httpx.AsyncByteStream):
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def __aiter__(self) -> Any:
        yield self.data


def _unread_transport(status: int, body: dict[str, Any]) -> httpx.MockTransport:
    return httpx.MockTransport(
        lambda r: httpx.Response(
            status,
            headers={"content-type": "application/json"},
            stream=_UnreadBody(json.dumps(body).encode()),
        )
    )


@pytest.mark.parametrize("name", ["openai", "anthropic", "gemini"])
async def test_streamed_overflow_with_unread_body_is_typed(name: str) -> None:
    from nexura_agents.providers.types import GenerationRequest

    provider = build(name, _unread_transport(400, OVERFLOW[name]))
    req = GenerationRequest(messages=[Message(role="user", content="hi")])
    with pytest.raises(ProviderContextLengthError):
        async for _ in provider.stream(req):
            pass


async def test_anthropic_stream_error_is_never_response_not_read() -> None:
    from nexura_agents.providers.types import GenerationRequest

    provider = build("anthropic", _unread_transport(400, {"error": {"message": "bad field"}}))
    req = GenerationRequest(messages=[Message(role="user", content="hi")])
    with pytest.raises(ProviderClientError) as info:
        async for _ in provider.stream(req):
            pass
    assert not isinstance(info.value, ProviderContextLengthError)


class _OverflowStreamer:
    def __init__(self, fit: int) -> None:
        self.fit = fit
        self.calls: list[int] = []

    async def stream(self, messages: list[Message], *a: Any, **k: Any) -> Any:
        users = sum(1 for m in messages if m.role == "user")
        self.calls.append(users)
        if users > self.fit:
            raise ProviderContextLengthError("maximum context length is 10 tokens")
        yield "ok"


def _turns(n: int) -> list[Message]:
    out = [Message(role="system", content="s")]
    for i in range(n):
        out += [Message(role="user", content=f"q{i}"), Message(role="assistant", content=f"a{i}")]
    return [*out, Message(role="user", content="latest")]


async def test_retry_model_stream_trims_before_first_delta() -> None:
    inner = _OverflowStreamer(fit=2)
    model = RetryModel(inner, trim_history=5)  # type: ignore[arg-type]
    assert [d async for d in model.stream(_turns(4))] == ["ok"]
    assert inner.calls == [5, 4, 3, 2]
    assert model.turns_trimmed == 3


async def test_retry_model_stream_without_trim_history_raises() -> None:
    model = RetryModel(_OverflowStreamer(fit=2))  # type: ignore[arg-type]
    with pytest.raises(ProviderContextLengthError):
        [d async for d in model.stream(_turns(4))]


async def test_fallback_of_retry_stream_trims_on_primary() -> None:
    from nexura_agents.models.resilience import FallbackModel

    primary, other = _OverflowStreamer(fit=2), _OverflowStreamer(fit=99)
    fb = FallbackModel(RetryModel(primary, trim_history=5), other)  # type: ignore[arg-type]
    assert [d async for d in fb.stream(_turns(4))] == ["ok"]
    assert other.calls == []


@pytest.mark.parametrize(
    "message",
    [
        "This model's maximum prompt length is 131072 but the request contains 150000 tokens.",
        "the request exceeds the available context size, try increasing it",
        "input length and `max_tokens` exceed context limit: 1 + 2 > 3",
    ],
)
def test_new_overflow_wordings_are_recognised(message: str) -> None:
    from nexura_agents.providers.errors import is_context_length_message

    assert is_context_length_message(message)


def test_classifier_stays_specific() -> None:
    from nexura_agents.providers.errors import is_context_length_message

    assert not is_context_length_message("please explain what a context window is")
    assert not is_context_length_message("invalid value for max_tokens")


def test_litellm_context_window_exceeded_class_is_recognised() -> None:
    from nexura_agents.models.litellm import map_litellm_exception

    class ContextWindowExceededError(Exception):
        status_code = 400

    err = map_litellm_exception(ContextWindowExceededError("Too many tokens: 500000"))
    assert isinstance(err, ProviderContextLengthError)

    class BadRequestError(Exception):
        status_code = 400

    plain = map_litellm_exception(BadRequestError("bad field"))
    assert isinstance(plain, ProviderClientError)
    assert not isinstance(plain, ProviderContextLengthError)
