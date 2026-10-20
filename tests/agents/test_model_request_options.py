"""Constructor-level headers, extra_body and query_params on OpenAIChatModel."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from nexura_agents import Message, ModelSettings
from nexura_agents.errors import SDKValidationError
from nexura_agents.models.openai import OpenAIChatModel

OK = {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
SSE = 'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
MSGS = [Message(role="user", content="hi")]


def _model(seen: list[httpx.Request], *, stream: bool = False, **kw: Any) -> OpenAIChatModel:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if stream:
            return httpx.Response(200, text=SSE, headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=OK)

    return OpenAIChatModel("m", transport=httpx.MockTransport(handler), **kw)


async def test_generate_applies_headers_body_query() -> None:
    seen: list[httpx.Request] = []
    model = _model(
        seen,
        api_key="sk-secret",
        headers={"X-Team": "a"},
        extra_body={"user": "u", "metadata": {"k": 1}},
        query_params={"api-version": "v1"},
    )
    await model.generate(MSGS)
    req = seen[0]
    assert req.headers["x-team"] == "a"
    assert req.headers["authorization"] == "Bearer sk-secret"
    assert req.headers["content-type"] == "application/json"
    assert req.url.params["api-version"] == "v1"
    body = json.loads(req.content)
    assert body["user"] == "u" and body["metadata"] == {"k": 1} and body["model"] == "m"


async def test_stream_applies_headers_body_query() -> None:
    seen: list[httpx.Request] = []
    model = _model(
        seen,
        stream=True,
        headers={"X-Team": "a"},
        extra_body={"user": "u"},
        query_params={"api-version": "v1"},
    )
    deltas = [d async for d in model.stream(MSGS)]
    assert deltas
    req = seen[0]
    assert req.headers["x-team"] == "a"
    assert req.headers["accept"] == "text/event-stream"
    assert req.url.params["api-version"] == "v1"
    body = json.loads(req.content)
    assert body["user"] == "u" and body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}


async def test_per_call_extra_and_settings_win() -> None:
    seen: list[httpx.Request] = []
    model = _model(seen, extra_body={"user": "default", "temperature": 0.9, "keep": 1})
    settings = ModelSettings(temperature=0.1, extra={"user": "call"})
    await model.generate(MSGS, model_settings=settings)
    body = json.loads(seen[0].content)
    assert body["user"] == "call"
    assert body["temperature"] == 0.1
    assert body["keep"] == 1


async def test_defaults_not_mutated_between_calls() -> None:
    seen: list[httpx.Request] = []
    nested = {"a": [1]}
    model = _model(seen, extra_body={"n": nested})
    nested["a"].append(2)  # caller mutation after construction is ignored
    await model.generate(MSGS)
    await model.generate(MSGS)
    assert [json.loads(r.content)["n"] for r in seen] == [{"a": [1]}, {"a": [1]}]


@pytest.mark.parametrize("key", ["model", "messages", "tools", "stream", "stream_options"])
def test_core_body_keys_refused(key: str) -> None:
    with pytest.raises(SDKValidationError, match=key):
        OpenAIChatModel("m", extra_body={key: "x"})


@pytest.mark.parametrize("name", ["Authorization", "authorization", "Content-Type", "ACCEPT"])
def test_sdk_headers_refused_with_api_key(name: str) -> None:
    with pytest.raises(SDKValidationError) as info:
        OpenAIChatModel("m", api_key="sk-secret", headers={name: "super-secret-value"})
    assert "super-secret-value" not in str(info.value)
    assert "sk-secret" not in str(info.value)


def test_content_type_refused_without_key_but_authorization_allowed() -> None:
    with pytest.raises(SDKValidationError):
        OpenAIChatModel("m", headers={"Content-Type": "text/plain"})
    OpenAIChatModel("m", headers={"Authorization": "Token abc"})


async def test_own_authorization_sent_without_api_key() -> None:
    seen: list[httpx.Request] = []
    model = _model(seen, headers={"Authorization": "Token abc"})
    await model.generate(MSGS)
    assert seen[0].headers["authorization"] == "Token abc"


def test_invalid_types_refused_without_values() -> None:
    with pytest.raises(SDKValidationError):
        OpenAIChatModel("m", headers={"X": 1})  # type: ignore[dict-item]
    with pytest.raises(SDKValidationError):
        OpenAIChatModel("m", extra_body={1: "x"})  # type: ignore[dict-item]


def test_header_values_not_in_repr() -> None:
    model = OpenAIChatModel("m", api_key="sk-secret", headers={"X-Token": "hdr-secret"})
    text = repr(model)
    assert "hdr-secret" not in text and "sk-secret" not in text
    assert "X-Token" in text


async def test_header_values_not_in_http_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    model = OpenAIChatModel(
        "m", headers={"X-Token": "hdr-secret"}, transport=httpx.MockTransport(handler)
    )
    with pytest.raises(Exception) as info:
        await model.generate(MSGS)
    assert "hdr-secret" not in str(info.value)


# Review findings (F56 adversarial review), each reproduced before the fix.


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"headers": ["a"]}, "headers must be a dict"),
        ({"extra_body": ["a"]}, "extra_body must be a dict"),
        ({"query_params": 5}, "query_params must be a dict"),
        ({"headers": {"Content-Length": "1"}}, "headers cannot set"),
        ({"headers": {"Host": "evil"}}, "headers cannot set"),
        ({"headers": {"Transfer-Encoding": "chunked"}}, "headers cannot set"),
        ({"headers": {"X-A": "1", "x-a": "2"}}, "different letter case"),
        ({"headers": {" Authorization": "x"}}, "HTTP tokens"),
        ({"headers": {"X-Note": "a\r\nInjected: 1"}}, "single line"),
        ({"extra_body": {"x": object()}}, "JSON-serialisable"),
    ],
)
def test_bad_request_options_are_refused_at_construction(kwargs: Any, match: str) -> None:
    with pytest.raises(SDKValidationError, match=match):
        OpenAIChatModel(model="m", api_key="sk-test", **kwargs)


def test_query_params_are_copied() -> None:
    tags = ["a"]
    model = OpenAIChatModel(model="m", api_key="sk-test", query_params={"tags": tags})
    tags.append("evil")
    assert model._query_params == {"tags": ["a"]}
