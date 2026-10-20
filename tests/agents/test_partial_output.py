"""Streaming structured output: PARTIAL_OUTPUT events, and providers that stream schemas."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from nexura_agents import Agent, Runner
from nexura_agents.events import RunEventType
from nexura_agents.limits import ModelSettings
from nexura_agents.providers.gemini.provider import GeminiNativeProvider
from nexura_agents.providers.openai.provider import OpenAIProvider
from nexura_agents.providers.types import ProviderCapabilities, StreamDelta
from nexura_agents.runner import _PartialOutput
from nexura_agents.types import ModelResponse
from pydantic import BaseModel


class Ticket(BaseModel):
    title: str
    tags: list[str]


class Streamer:
    """A legacy streaming model that replays text chunks."""

    capabilities = ProviderCapabilities(can_stream=True)
    supports_response_schema = True
    streams_response_schema = True

    def __init__(self, chunks: list[str]) -> None:
        self.chunks = chunks
        self.schemas: list[Any] = []

    async def generate(self, messages: Any, tools: Any = None, **kw: Any) -> ModelResponse:
        raise AssertionError("stream_text should stream")

    async def stream(
        self, messages: Any, tools: Any = None, *, timeout: Any = None, response_schema: Any = None
    ) -> Any:
        self.schemas.append(response_schema)
        for chunk in self.chunks:
            yield StreamDelta(kind="text_delta", text=chunk)
        yield StreamDelta(kind="finish", finish_reason="stop")


@pytest.fixture
def every_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Parse after every chunk (no throttling), to test the content of the events."""
    monkeypatch.setattr(_PartialOutput, "MIN_STEP", 0)


async def events_for(model: Any, **kw: Any) -> list[Any]:
    agent = Agent(name="A", instructions="i", model=model, output_type=Ticket, **kw)
    return [e async for e in Runner().stream_async(agent, "go", stream_text=True)]


def partials(events: list[Any]) -> list[Any]:
    return [e.data["output"] for e in events if e.type == RunEventType.PARTIAL_OUTPUT]


@pytest.mark.usefixtures("every_chunk")
async def test_partial_outputs_grow_as_json_arrives() -> None:
    model = Streamer(['{"title": "Re', 'fund", "ta', 'gs": ["billing"', ', "urgent"]}'])
    events = await events_for(model)
    assert partials(events) == [
        {"title": "Re"},
        {"title": "Refund"},
        {"title": "Refund", "tags": ["billing"]},
        {"title": "Refund", "tags": ["billing", "urgent"]},
    ]
    final = [e for e in events if e.type == RunEventType.RUN_COMPLETED][0]
    assert final.data["output_value"] == Ticket(title="Refund", tags=["billing", "urgent"])
    assert model.schemas[0] is not None  # the native schema went with the stream


@pytest.mark.usefixtures("every_chunk")
async def test_unchanged_values_are_not_repeated_and_fences_are_skipped() -> None:
    model = Streamer(["```json\n", '{"title": "A"', "   ", ', "tags": []}', "\n```"])
    assert partials(await events_for(model)) == [{"title": "A"}, {"title": "A", "tags": []}]


async def test_no_partials_without_output_type_or_without_streaming() -> None:
    model = Streamer(['{"title": "A", "tags": []}'])
    agent = Agent(name="A", instructions="i", model=model)
    events = [e async for e in Runner().stream_async(agent, "go", stream_text=True)]
    assert partials(events) == []


@pytest.mark.usefixtures("every_chunk")
def test_partial_parser_ignores_prose_and_copies_values() -> None:
    parser = _PartialOutput()
    assert parser.feed("Sure, here it is") is None
    other = _PartialOutput()
    first = other.feed('{"a": [1')
    assert first == {"a": [1]}
    first["a"].append(99)  # a consumer mutating the event must not affect later events
    assert other.feed(", 2]}") == {"a": [1, 2]}


# ---------------------------------------------------------------- providers


def sse_response(chunks: list[str]) -> httpx.Response:
    body = "".join(f"data: {c}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())


async def test_openai_provider_streams_with_schema_and_settings() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        chunk = lambda d, f=None: json.dumps(  # noqa: E731
            {"choices": [{"index": 0, "delta": d, "finish_reason": f}]}
        )
        return sse_response(
            [
                chunk({"content": '{"title": "T", '}),
                chunk({"content": '"tags": []}'}),
                chunk({}, "stop"),
            ]
        )

    provider = OpenAIProvider("gpt-x", api_key="k", transport=httpx.MockTransport(handler))
    events = await events_for(
        provider, model_settings=ModelSettings(temperature=0.2, stop_sequences=["##"])
    )
    assert seen[0]["stream"] is True
    assert seen[0]["response_format"]["type"] == "json_schema"
    assert seen[0]["temperature"] == 0.2 and seen[0]["stop"] == ["##"]
    assert partials(events)[-1] == {"title": "T", "tags": []}


async def test_gemini_provider_stream_sends_generation_config() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        part = lambda text, finish=None: json.dumps(  # noqa: E731
            {
                "candidates": [
                    {"content": {"role": "model", "parts": [{"text": text}]}}
                    | ({"finishReason": finish} if finish else {})
                ]
            }
        )
        return sse_response([part('{"title": "G", '), part('"tags": ["x"]}', "STOP")])

    provider = GeminiNativeProvider(
        "gemini-2.5-flash", api_key="k", transport=httpx.MockTransport(handler)
    )
    events = await events_for(
        provider, model_settings=ModelSettings(temperature=0.1, max_tokens=50)
    )
    config = seen[0]["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert "responseSchema" in config
    assert config["temperature"] == 0.1 and config["maxOutputTokens"] == 50
    assert partials(events)[-1] == {"title": "G", "tags": ["x"]}


# ---------------------------------------------------------------- review fixes


async def test_parsing_is_throttled_but_the_last_partial_is_complete() -> None:
    items = [{"id": i, "name": f"item {i}"} for i in range(2000)]
    text = json.dumps({"title": "big", "tags": [str(i["id"]) for i in items]})
    chunks = [text[i : i + 10] for i in range(0, len(text), 10)]
    events = await events_for(Streamer(chunks))
    got = partials(events)
    assert len(got) < len(chunks) / 10  # far fewer parses than chunks
    assert got[-1] == json.loads(text)  # flushed at the end of the stream
    assert len(got) >= 3  # still progressive


def test_parse_cost_is_linear() -> None:
    import time

    body = json.dumps([{"k": i, "v": "x" * 20} for i in range(8000)])
    parser = _PartialOutput()
    started = time.monotonic()
    for i in range(0, len(body), 100):
        parser.feed(body[i : i + 100])
    parser.flush()
    assert time.monotonic() - started < 3


async def test_streamed_refusal_keeps_its_text() -> None:
    class Refuser(Streamer):
        async def stream(self, messages: Any, tools: Any = None, **kw: Any) -> Any:
            yield StreamDelta(kind="finish", finish_reason="refusal", text="I cannot help")
            yield StreamDelta(kind="finish", finish_reason="stop")

    from nexura_agents.errors import OutputValidationError

    agent = Agent(name="A", instructions="i", model=Refuser([]), output_type=Ticket)
    with pytest.raises(OutputValidationError) as info:
        async for _ in Runner().stream_async(agent, "go", stream_text=True):
            pass
    assert "Model Refusal: I cannot help" in str(info.value.raw_output)
