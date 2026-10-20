from __future__ import annotations

import contextlib
import json

import httpx
import pytest
from nexura_agents import Agent, FakeModel, Message, Runner, ToolCall, tool
from nexura_agents import tracing as nexura_tracing
from nexura_agents.models.openai import OpenAIChatModel
from nexura_agents.network.policy import DestinationPolicy
from nexura_agents.tracing import redact
from nexura_agents.web_tools import WebFetchRefused, _check_destination
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

SECRET = "sk-ABCDEFGHIJKLMNOPQRSTUV123456"


def _setup(capture: bool):
    mem = InMemorySpanExporter()
    prov = TracerProvider()
    prov.add_span_processor(SimpleSpanProcessor(mem))
    nexura_tracing.instrument(tracer_provider=prov, capture_content=capture)
    return mem


@pytest.fixture
def exporter():
    mem = _setup(False)
    yield mem
    nexura_tracing.uninstrument()


def _dump(exporter) -> str:
    out = []
    for s in exporter.get_finished_spans():
        out.append(s.status.description or "")
        out.append(json.dumps(dict(s.attributes or {}), default=str))
        for e in s.events:
            out.append(e.name + json.dumps(dict(e.attributes or {}), default=str))
    return "\n".join(out)


def _boom_agent() -> Agent:
    @tool
    def boom(x: int) -> str:
        raise RuntimeError(f"rejected Authorization: Bearer {SECRET} key={SECRET}")

    return Agent(
        name="A",
        instructions="",
        model=FakeModel(responses=[[ToolCall(id="c", name="boom", arguments={"x": 1})], "x"]),
        tools=[boom],
    )


async def test_tool_exception_secret_not_in_spans_without_capture_content(exporter):
    with contextlib.suppress(Exception):
        await Runner().run_async(_boom_agent(), "hi")
    dump = _dump(exporter)
    assert SECRET not in dump
    assert "ToolExecutionError" in dump  # error.type kept


async def test_tool_exception_redacted_with_capture_content():
    mem = _setup(True)
    try:
        with contextlib.suppress(Exception):
            await Runner().run_async(_boom_agent(), "hi")
        assert SECRET not in _dump(mem)
    finally:
        nexura_tracing.uninstrument()


@pytest.mark.parametrize(
    "key",
    [
        "accessToken",
        "clientSecret",
        "authToken",
        "Set-Cookie",
        "auth",
        "credentials",
        "x-amz-security-token",
        "privateKey",
    ],
)
def test_redact_covers_common_secret_key_spellings(key):
    assert redact({key: "hunter2-very-secret"})[key] == "[REDACTED]"


@pytest.mark.parametrize("key", ["author", "authority", "tokenizer", "authored_by", "keyboard"])
def test_redact_keeps_ordinary_words(key):
    assert redact({key: "plain"})[key] == "plain"


def test_redact_basic_auth_header_value_string():
    assert "dXNlcjpwYXNzd29yZA" not in redact("Authorization: Basic dXNlcjpwYXNzd29yZA==")
    assert "dXNlcjpwYXNzd29yZA" not in redact("sent Basic dXNlcjpwYXNzd29yZA== here")


async def test_stream_error_event_does_not_echo_secret_header_value():
    hv = "gw-tenant-credential-9f8e7d6c5b4a"

    def handler(request: httpx.Request) -> httpx.Response:
        body = (
            'data: {"error": {"message": "bad header value '
            + hv
            + '", "type": "server_error"}}\n\n'
        )
        return httpx.Response(
            200, content=body.encode(), headers={"content-type": "text/event-stream"}
        )

    model = OpenAIChatModel(
        "m",
        api_key="sk-live-abcdefghijklmnop",
        headers={"X-Gateway-Cred": hv},
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(Exception) as info:
        async for _ in model.stream([Message(role="user", content="hi")]):
            pass
    assert hv not in str(info.value)


async def test_run_metadata_secrets_are_redacted_on_spans(exporter):
    agent = Agent(name="A", instructions="", model=FakeModel(responses=["ok"]))
    await Runner().run_async(
        agent,
        "hi",
        metadata={
            "api_token": "tok-SECRET-VALUE-123",
            "authorization": "Bearer abcdef0123456789xyz",
            "note": "Bearer zzzz1111yyyy2222",
        },
    )
    dump = _dump(exporter)
    assert "SECRET-VALUE-123" not in dump
    assert "abcdef0123456789xyz" not in dump
    assert "zzzz1111yyyy2222" not in dump


def test_web_fetch_blocks_site_local_ipv6():
    with pytest.raises(WebFetchRefused):
        _check_destination("http://[fec0::1]/", DestinationPolicy(), ())
