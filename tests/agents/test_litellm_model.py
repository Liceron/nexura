"""LiteLLMModel with an injected fake ``completion`` (litellm is not imported)."""

from __future__ import annotations

import sys
from typing import Any

import httpx
import pytest
from nexura_agents import Agent, Attachment, LiteLLMModel, Message, tool
from nexura_agents.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderRequestError,
    SDKValidationError,
)
from nexura_agents.models.infer import infer_model
from nexura_agents.models.resilience import RetryModel
from nexura_agents.providers.errors import (
    ProviderClientError,
    ProviderRateLimitedError,
    ProviderServerError,
)


def reply(content: str | None = "hi", **extra: Any) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    finish = extra.pop("finish_reason", "stop")
    message.update(extra.pop("message", {}))
    out: dict[str, Any] = {"choices": [{"message": message, "finish_reason": finish}]}
    out.update(extra)
    return out


class Recorder:
    def __init__(self, *results: Any) -> None:
        self.results = list(results)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, BaseException):
            raise result
        return result


class FakeLiteLLMError(Exception):
    """Plain stand-in for a litellm exception: attributes only."""

    def __init__(self, msg: str, status_code: int | None = None, headers: Any = None) -> None:
        super().__init__(msg)
        if status_code is not None:
            self.status_code = status_code
        if headers is not None:
            self.response = httpx.Response(status_code or 500, headers=headers)


class Timeout(FakeLiteLLMError):  # name-based, like litellm.Timeout
    pass


class APIConnectionError(FakeLiteLLMError):
    pass


class ServiceUnavailableError(FakeLiteLLMError):
    pass


async def gen(rec: Recorder, **kw: Any) -> Any:
    return await LiteLLMModel("ollama/llama3.1", completion=rec, **kw).generate(
        [Message(role="user", content="hello")]
    )


async def test_plain_reply_and_request_shape() -> None:
    rec = Recorder(reply("pong"))
    model = LiteLLMModel(
        "bedrock/x",
        api_key="secret-key-123",
        api_base="http://b",
        timeout=5,
        completion=rec,
        temperature=0.2,
    )
    out = await model.generate([Message(role="user", content="ping")], timeout=9)
    assert out.message.content == "pong"
    assert out.finish_reason == "stop"
    call = rec.calls[0]
    assert call["model"] == "bedrock/x"
    assert call["messages"] == [{"role": "user", "content": "ping"}]
    assert call["api_key"] == "secret-key-123" and call["api_base"] == "http://b"
    assert call["timeout"] == 9 and call["temperature"] == 0.2
    assert "tools" not in call
    assert "secret-key-123" not in repr(model)


async def test_retries_disabled_even_if_option_given() -> None:
    rec = Recorder(reply())
    await gen(rec, num_retries=5, max_retries=5)
    assert rec.calls[0]["num_retries"] == 0
    assert rec.calls[0]["max_retries"] == 0


async def test_usage_reasoning_and_cached_tokens() -> None:
    usage = {
        "prompt_tokens": 10,
        "completion_tokens": 20,
        "total_tokens": 30,
        "completion_tokens_details": {"reasoning_tokens": 7},
        "prompt_tokens_details": {"cached_tokens": 4},
    }
    out = await gen(Recorder(reply(usage=usage)))
    u = out.usage
    assert u is not None
    assert (u.prompt_tokens, u.completion_tokens, u.total_tokens) == (10, 20, 30)
    assert u.reasoning_tokens == 7 and u.cached_prompt_tokens == 4
    assert (await gen(Recorder(reply()))).usage is None


async def test_object_style_response() -> None:
    class Obj:
        def __init__(self, d: dict[str, Any]) -> None:
            self.d = d

        def model_dump(self) -> dict[str, Any]:
            return self.d

    out = await gen(Recorder(Obj(reply("obj", usage={"prompt_tokens": 1}))))
    assert out.message.content == "obj" and out.usage is not None


async def test_tool_call_round_trip_through_agent() -> None:
    @tool
    def add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    first = reply(
        None,
        finish_reason="tool_calls",
        message={
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "add", "arguments": '{"a": 2, "b": 3}'},
                }
            ]
        },
    )
    rec = Recorder(first, reply("The sum is 5"))
    agent = Agent(name="A", instructions="x", model=LiteLLMModel("m", completion=rec), tools=[add])
    result = await agent.run_async("2+3?")
    assert result.final_output == "The sum is 5"
    assert len(rec.calls) == 2
    assert rec.calls[0]["tools"][0]["function"]["name"] == "add"
    second = rec.calls[1]["messages"]
    assert any(m.get("tool_calls") for m in second if m["role"] == "assistant")
    tool_msg = next(m for m in second if m["role"] == "tool")
    assert tool_msg["tool_call_id"] == "call_1" and "5" in tool_msg["content"]


async def test_invalid_tool_json_is_response_error() -> None:
    bad = reply(
        None,
        message={
            "tool_calls": [
                {"id": "c", "type": "function", "function": {"name": "f", "arguments": "{nope"}}
            ]
        },
    )
    with pytest.raises(ProviderError, match="Malformed tool call"):
        await gen(Recorder(bad))


async def test_attachments_forwarded_as_content_parts() -> None:
    rec = Recorder(reply("ok"))
    att = Attachment.from_bytes(b"\x89PNG\r\n", "image/png")
    await LiteLLMModel("m", completion=rec).generate(
        [Message(role="user", content="look", attachments=[att])]
    )
    parts = rec.calls[0]["messages"][0]["content"]
    assert parts[0] == {"type": "text", "text": "look"}
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (FakeLiteLLMError("bad key", 401), ProviderAuthError),
        (FakeLiteLLMError("forbidden", 403), ProviderAuthError),
        (FakeLiteLLMError("slow", 429), ProviderRateLimitedError),
        (FakeLiteLLMError("boom", 500), ProviderServerError),
        (FakeLiteLLMError("bad gw", 502), ProviderServerError),
        (ServiceUnavailableError("down"), ProviderServerError),
        (FakeLiteLLMError("bad request", 400), ProviderClientError),
        (FakeLiteLLMError("no model", 404), ProviderClientError),
        (FakeLiteLLMError("context too long", 400), ProviderClientError),
        (Timeout("slow", 408), ProviderRequestError),
        (TimeoutError("t"), ProviderRequestError),
        (APIConnectionError("dns"), ProviderRequestError),
    ],
)
async def test_error_mapping(exc: Exception, expected: type[BaseException]) -> None:
    with pytest.raises(expected) as info:
        await gen(Recorder(exc))
    assert type(info.value) is expected or isinstance(info.value, expected)


async def test_rate_limit_retry_after_header() -> None:
    err = FakeLiteLLMError("slow", 429, headers={"Retry-After": "7"})
    with pytest.raises(ProviderRateLimitedError) as info:
        await gen(Recorder(err))
    assert info.value.retry_after == 7.0
    assert isinstance(info.value, ProviderRateLimitError)
    with pytest.raises(ProviderRateLimitedError) as info2:
        await gen(Recorder(FakeLiteLLMError("slow", 429)))
    assert info2.value.retry_after is None


async def test_secrets_redacted_in_errors() -> None:
    secret = "sk-" + "a" * 30
    with pytest.raises(ProviderAuthError) as info:
        await gen(Recorder(FakeLiteLLMError(f"invalid key {secret}", 401)))
    assert secret not in str(info.value)


async def test_unrecognised_exception_propagates() -> None:
    with pytest.raises(ZeroDivisionError):
        await gen(Recorder(ZeroDivisionError()))


async def test_retry_model_retries_503_but_not_400() -> None:
    rec = Recorder(FakeLiteLLMError("down", 503), reply("recovered"))
    retry = RetryModel(
        LiteLLMModel("m", completion=rec), max_attempts=3, base_delay=0.001, max_delay=0.01
    )
    out = await retry.generate([Message(role="user", content="x")])
    assert out.message.content == "recovered" and len(rec.calls) == 2

    rec2 = Recorder(FakeLiteLLMError("bad", 400))
    retry2 = RetryModel(
        LiteLLMModel("m", completion=rec2), max_attempts=3, base_delay=0.001, max_delay=0.01
    )
    with pytest.raises(ProviderClientError):
        await retry2.generate([Message(role="user", content="x")])
    assert len(rec2.calls) == 1


async def test_missing_package_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "litellm", None)  # makes `import litellm` fail
    model = LiteLLMModel("ollama/llama3.1")
    with pytest.raises(ImportError, match=r"nexura-agents\[litellm\]"):
        await model.generate([Message(role="user", content="x")])


def test_infer_model_litellm_prefix() -> None:
    model = infer_model("litellm:bedrock/anthropic.claude-3-5-sonnet")
    assert isinstance(model, LiteLLMModel)
    assert model.model == "bedrock/anthropic.claude-3-5-sonnet"
    with pytest.raises(SDKValidationError):
        infer_model("litellm:")


def _usage_reply() -> dict[str, Any]:
    return reply(
        usage={
            "prompt_tokens": 100,
            "completion_tokens": 5,
            "total_tokens": 105,
            "cache_creation_input_tokens": 30,
            "cache_read_input_tokens": 40,
            "prompt_tokens_details": {"cached_tokens": 40},
        }
    )


async def test_cache_creation_and_read_tokens_mapped() -> None:
    model = LiteLLMModel("anthropic/x", completion=Recorder(_usage_reply()))
    usage = (await model.generate([Message(role="user", content="hi")])).usage
    assert usage is not None
    assert usage.cache_creation_input_tokens == 30
    assert usage.cache_read_input_tokens == 40
    assert usage.cached_prompt_tokens == 40


@pytest.mark.parametrize(
    "key", ["AIzaSyA1234567890abcdefghijklmnopqrstuv", "gsk_abcDEF1234567890xyz"]
)
async def test_api_key_redacted_from_mapped_errors(key: str) -> None:
    for status in (500, 400, 401, 429):
        err = FakeLiteLLMError(f"upstream rejected key {key}", status_code=status)
        model = LiteLLMModel("gemini/x", api_key=key, completion=Recorder(err))
        with pytest.raises(ProviderError) as info:
            await model.generate([Message(role="user", content="hi")])
        e = info.value
        assert key not in str(e) and key not in repr(e)
        assert key not in str(getattr(e, "original_error", ""))
        assert e.__cause__ is None and e.__context__ is None
    timeout = Timeout(f"slow {key}")
    model = LiteLLMModel("gemini/x", api_key=key, completion=Recorder(timeout))
    with pytest.raises(ProviderRequestError) as info:
        await model.generate([Message(role="user", content="hi")])
    assert key not in str(info.value)
    assert key not in str(info.value.original_error)


def test_stream_option_rejected_clearly() -> None:
    with pytest.raises(SDKValidationError, match="stream"):
        LiteLLMModel("openai/gpt-4o", stream=True)


async def test_streaming_result_is_closed_with_clear_error() -> None:
    class FakeStream:
        closed = False

        def __aiter__(self) -> FakeStream:
            return self

        async def __anext__(self) -> Any:
            raise StopAsyncIteration

        async def aclose(self) -> None:
            self.closed = True

    stream = FakeStream()
    model = LiteLLMModel("openai/x", completion=Recorder(stream))
    with pytest.raises(Exception, match="streaming is not supported"):
        await model.generate([Message(role="user", content="hi")])
    assert stream.closed
