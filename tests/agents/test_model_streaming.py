"""Token streaming for OpenAIChatModel (mock SSE) and LiteLLMModel (fake async completion)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from nexura_agents import Agent, LiteLLMModel, Message, OpenAIChatModel, Runner, tool
from nexura_agents import tracing as nexura_tracing
from nexura_agents.errors import (
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderRequestError,
    ProviderResponseError,
)
from nexura_agents.providers.errors import MalformedToolCallArgumentsError
from nexura_agents.runner import RunEventType
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

USER = [Message(role="user", content="hi")]


@tool
def add(a: int, b: int) -> str:
    """Add two numbers."""
    return str(a + b)


def sse(*chunks: Any, done: bool = True) -> bytes:
    lines = [f"data: {c if isinstance(c, str) else json.dumps(c)}\n\n" for c in chunks]
    if done:
        lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


def oc(delta: dict[str, Any] | None = None, finish: str | None = None) -> dict[str, Any]:
    return {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}


USAGE_CHUNK = {
    "choices": [],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
}


class SseBody(httpx.AsyncByteStream):
    """SSE body that can fail mid-stream and records that it was closed."""

    def __init__(self, parts: list[bytes], fail: Exception | None = None, hang: bool = False):
        self.parts, self.fail, self.hang = parts, fail, hang
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for part in self.parts:
            yield part
        if self.fail is not None:
            raise self.fail
        if self.hang:
            await asyncio.sleep(30)

    async def aclose(self) -> None:
        self.closed = True


def openai_model(
    body: SseBody | bytes, seen: list[dict[str, Any]] | None = None
) -> OpenAIChatModel:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(json.loads(request.content))
        stream = body if isinstance(body, SseBody) else SseBody([body])
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    return OpenAIChatModel(
        "m", api_key="sk-secret-key-value", transport=httpx.MockTransport(handler)
    )


async def collect(model: Any, agent_tools: list[Any] | None = None) -> list[Any]:
    agent = Agent(name="A", instructions="x", model=model, tools=agent_tools or [])
    return [e async for e in Runner().stream_async(agent, "hi", stream_text=True)]


def texts(events: list[Any]) -> list[str]:
    return [e.data["text"] for e in events if e.type == RunEventType.TEXT_DELTA]


# ---------------------------------------------------------------- OpenAI


async def test_openai_text_deltas_in_order_with_usage_and_finish() -> None:
    seen: list[dict[str, Any]] = []
    body = sse(
        oc({"role": "assistant", "content": ""}),
        oc({"content": "Hel"}),
        oc({"content": "lo "}),
        oc({"content": "world"}),
        oc(finish="stop"),
        USAGE_CHUNK,
    )
    events = await collect(openai_model(body, seen))
    assert texts(events) == ["Hel", "lo ", "world"]
    done = [e for e in events if e.type == RunEventType.MODEL_COMPLETED][0]
    assert done.data["finish_reason"] == "stop"
    assert done.data["usage"].total_tokens == 7
    assert seen[0]["stream"] is True
    assert seen[0]["stream_options"] == {"include_usage": True}
    final = [e for e in events if e.type == RunEventType.RUN_COMPLETED][0]
    assert "Hello world" in str(final.data)


async def test_openai_generate_unchanged_not_streaming() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        )

    model = OpenAIChatModel("m", transport=httpx.MockTransport(handler))
    assert (await model.generate(USER)).message.content == "ok"
    assert "stream" not in seen[0]
    # without stream_text the Runner keeps using generate()
    agent = Agent(name="A", instructions="x", model=model)
    events = [e async for e in Runner().stream_async(agent, "hi")]
    assert texts(events) == []
    assert "stream" not in seen[1]


async def test_openai_tool_call_arguments_assembled_across_chunks() -> None:
    body = sse(
        oc(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "add", "arguments": ""},
                    }
                ]
            }
        ),
        oc({"tool_calls": [{"index": 0, "function": {"arguments": '{"a": 2,'}}]}),
        oc({"tool_calls": [{"index": 0, "function": {"arguments": ' "b": 3}'}}]}),
        oc(finish="tool_calls"),
        USAGE_CHUNK,
    )
    model = openai_model(body)
    stream = model.stream(USER, [add.to_schema()])
    deltas = [d async for d in stream]
    assert [d.kind for d in deltas] == [
        "tool_call_delta",
        "tool_call_delta",
        "tool_call_delta",
        "finish",
        "usage_delta",
    ]
    # Through the Runner: a first turn with the streamed tool call executes the tool.
    replies = [
        sse(
            oc({"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "add"}}]}),
            oc({"tool_calls": [{"index": 0, "function": {"arguments": '{"a": 2,'}}]}),
            oc({"tool_calls": [{"index": 0, "function": {"arguments": ' "b": 3}'}}]}),
            oc(finish="tool_calls"),
        ),
        sse(oc({"content": "five"}), oc(finish="stop")),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=SseBody([replies.pop(0)]))

    events = await collect(OpenAIChatModel("m", transport=httpx.MockTransport(handler)), [add])
    started = [e for e in events if e.type == RunEventType.TOOL_STARTED][0]
    assert started.data["arguments"] == {"a": 2, "b": 3}
    assert texts(events) == ["five"]


async def test_openai_malformed_streamed_tool_arguments_never_execute() -> None:
    body = sse(
        oc({"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "add"}}]}),
        oc({"tool_calls": [{"index": 0, "function": {"arguments": '{"a": '}}]}),
        oc(finish="tool_calls"),
    )
    with pytest.raises(MalformedToolCallArgumentsError):
        await collect(openai_model(body), [add])


async def test_openai_mid_stream_error_event_is_typed_and_not_committed() -> None:
    body = sse(
        oc({"content": "partial "}),
        {"error": {"message": "rate limited sk-secret-key-value", "code": 429}},
    )
    agent = Agent(name="A", instructions="x", model=openai_model(body))
    seen_events: list[Any] = []
    with pytest.raises(ProviderRateLimitError):
        async for e in Runner().stream_async(agent, "hi", stream_text=True):
            seen_events.append(e)
    assert texts(seen_events) == ["partial "]
    types = [e.type for e in seen_events]
    assert RunEventType.RUN_FAILED in types
    assert RunEventType.MODEL_COMPLETED not in types
    assert RunEventType.RUN_COMPLETED not in types


async def test_openai_mid_stream_network_error_maps_to_request_error() -> None:
    body = SseBody([sse(oc({"content": "x"}), done=False)], fail=httpx.ReadError("boom"))
    with pytest.raises(ProviderRequestError):
        await collect(openai_model(body))


async def test_openai_truncated_stream_is_an_error() -> None:
    with pytest.raises(ProviderResponseError, match="before completion"):
        await collect(openai_model(sse(oc({"content": "cut"}), done=False)))


async def test_openai_malformed_chunk_and_empty_message() -> None:
    with pytest.raises(ProviderResponseError, match="JSON"):
        await collect(openai_model(sse("{not json")))
    with pytest.raises(ProviderResponseError, match="neither content nor tool calls"):
        await collect(openai_model(sse(oc(finish="stop"))))


async def test_openai_http_errors_map_like_generate() -> None:
    def make(status: int) -> OpenAIChatModel:
        return OpenAIChatModel(
            "m",
            transport=httpx.MockTransport(
                lambda r: httpx.Response(status, headers={"Retry-After": "3"})
            ),
        )

    with pytest.raises(ProviderAuthError):
        await collect(make(401))
    with pytest.raises(ProviderRateLimitError):
        await collect(make(429))

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    with pytest.raises(ProviderRequestError, match="timed out"):
        await collect(OpenAIChatModel("m", transport=httpx.MockTransport(timeout)))


async def test_openai_cancellation_closes_http_stream() -> None:
    body = SseBody([sse(oc({"content": "first"}), done=False)], hang=True)
    model = openai_model(body)
    agent = Agent(name="A", instructions="x", model=model)
    got_first = asyncio.Event()

    async def drive() -> None:
        async for e in Runner().stream_async(agent, "hi", stream_text=True):
            if e.type == RunEventType.TEXT_DELTA:
                got_first.set()

    task = asyncio.create_task(drive())
    await asyncio.wait_for(got_first.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert body.closed


async def test_openai_schema_turn_streams_with_the_schema() -> None:
    # F26 (2026-10-10): typed turns used to be sent without streaming. They now stream,
    # and the request must still carry the schema (response_format).
    seen: list[dict[str, Any]] = []
    body = sse(
        oc({"role": "assistant", "content": '{"n"'}),
        oc({"content": ": 1}"}),
        oc(finish="stop"),
        USAGE_CHUNK,
    )
    from pydantic import BaseModel

    class Out(BaseModel):
        n: int

    agent = Agent(name="A", instructions="x", model=openai_model(body, seen), output_type=Out)
    events = [e async for e in Runner().stream_async(agent, "hi", stream_text=True)]
    assert seen[0]["stream"] is True
    assert "response_format" in seen[0]
    assert texts(events) == ['{"n"', ": 1}"]
    final = [e for e in events if e.type == RunEventType.RUN_COMPLETED][0]
    assert final.data["output_value"] == Out(n=1)


async def test_schema_turn_is_not_streamed_without_streams_response_schema() -> None:
    # The previous behaviour still holds for models that do not declare the capability.
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": '{"n": 1}'}}]},
        )

    class NoSchemaStreaming(OpenAIChatModel):
        streams_response_schema = False

    from pydantic import BaseModel

    class Out(BaseModel):
        n: int

    agent = Agent(
        name="A",
        instructions="x",
        model=NoSchemaStreaming("m", transport=httpx.MockTransport(handler)),
        output_type=Out,
    )
    events = [e async for e in Runner().stream_async(agent, "hi", stream_text=True)]
    assert "stream" not in seen[0]
    assert "response_format" in seen[0]
    assert texts(events) == []


async def test_openai_secret_not_leaked_in_error() -> None:
    body = sse({"error": {"message": "bad key sk-secret-key-value", "code": 400}})
    with pytest.raises(Exception) as info:
        await collect(openai_model(body))
    assert "sk-secret-key-value" not in str(info.value)
    assert "sk-secret-key-value" not in repr(openai_model(body))


async def test_streaming_chat_span_ends_with_usage() -> None:
    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    nexura_tracing.instrument(tracer_provider=provider)
    try:
        body = sse(oc({"content": "hi"}), oc(finish="stop"), USAGE_CHUNK)
        await collect(openai_model(body))
        chats = [s for s in memory.get_finished_spans() if s.name.startswith("chat ")]
        assert len(chats) == 1 and chats[0].attributes is not None
        assert chats[0].attributes["gen_ai.usage.input_tokens"] == 5
        assert chats[0].attributes["gen_ai.usage.output_tokens"] == 2
    finally:
        nexura_tracing.uninstrument()


# ---------------------------------------------------------------- LiteLLM


class FakeStream:
    """Async iterator of ModelResponse-like chunks with LiteLLM's ``aclose``."""

    def __init__(self, chunks: list[Any], fail: BaseException | None = None, hang: bool = False):
        self.chunks, self.fail, self.hang = list(chunks), fail, hang
        self.closed = False

    def __aiter__(self) -> FakeStream:
        return self

    async def __anext__(self) -> Any:
        if self.chunks:
            return self.chunks.pop(0)
        if self.fail is not None:
            raise self.fail
        if self.hang:
            await asyncio.sleep(30)
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.closed = True


class Chunk:
    """Pydantic-ish object: exposes ``model_dump`` like litellm's ModelResponseStream."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data

    def model_dump(self) -> dict[str, Any]:
        return self.data


def lchunks(*parts: dict[str, Any]) -> list[Chunk]:
    return [Chunk(p) for p in parts]


class Completion:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class UpstreamError(Exception):
    def __init__(self, msg: str, status_code: int) -> None:
        super().__init__(msg)
        self.status_code = status_code


LUSAGE = {
    "choices": [],
    "usage": {
        "prompt_tokens": 4,
        "completion_tokens": 3,
        "total_tokens": 7,
        "completion_tokens_details": {"reasoning_tokens": 1},
    },
}


async def test_litellm_text_deltas_usage_finish_and_request_shape() -> None:
    stream = FakeStream(
        lchunks(
            oc({"role": "assistant", "content": "Foo"}),
            oc({"content": "bar"}),
            oc({"content": None}, finish="stop"),
            LUSAGE,
        )
    )
    completion = Completion(stream)
    model = LiteLLMModel("openai/x", completion=completion, temperature=0.1)
    events = await collect(model)
    assert texts(events) == ["Foo", "bar"]
    done = [e for e in events if e.type == RunEventType.MODEL_COMPLETED][0]
    assert done.data["finish_reason"] == "stop"
    assert done.data["usage"].reasoning_tokens == 1
    kwargs = completion.calls[0]
    assert kwargs["stream"] is True
    assert kwargs["stream_options"] == {"include_usage": True}
    assert kwargs["num_retries"] == 0 and kwargs["max_retries"] == 0
    assert kwargs["temperature"] == 0.1
    assert stream.closed


async def test_litellm_user_stream_option_still_rejected() -> None:
    from nexura_agents.errors import SDKValidationError

    with pytest.raises(SDKValidationError, match="stream"):
        LiteLLMModel("openai/x", stream=True)


async def test_litellm_tool_call_arguments_assembled_across_chunks() -> None:
    def first() -> FakeStream:
        return FakeStream(
            lchunks(
                oc(
                    {
                        "tool_calls": [
                            {"index": 0, "id": "c1", "function": {"name": "add", "arguments": ""}}
                        ]
                    }
                ),
                oc({"tool_calls": [{"index": 0, "id": None, "function": {"arguments": '{"a":'}}]}),
                oc({"tool_calls": [{"index": 0, "function": {"arguments": ' 4, "b": 1}'}}]}),
                oc(finish="tool_calls"),
                LUSAGE,
            )
        )

    streams = [first(), FakeStream(lchunks(oc({"content": "five"}), oc(finish="stop")))]

    async def completion(**kwargs: Any) -> Any:
        return streams.pop(0)

    events = await collect(LiteLLMModel("openai/x", completion=completion), [add])
    started = [e for e in events if e.type == RunEventType.TOOL_STARTED][0]
    assert started.data["arguments"] == {"a": 4, "b": 1}
    assert texts(events) == ["five"]


async def test_litellm_mid_stream_error_is_mapped_and_closes_stream() -> None:
    stream = FakeStream(
        lchunks(oc({"content": "part"})), fail=UpstreamError("overloaded key-abc123", 429)
    )
    model = LiteLLMModel("openai/x", api_key="key-abc123", completion=Completion(stream))
    agent = Agent(name="A", instructions="x", model=model)
    got: list[Any] = []
    with pytest.raises(ProviderRateLimitError) as info:
        async for e in Runner().stream_async(agent, "hi", stream_text=True):
            got.append(e)
    assert "key-abc123" not in str(info.value)
    assert texts(got) == ["part"]
    assert RunEventType.MODEL_COMPLETED not in [e.type for e in got]
    assert RunEventType.RUN_COMPLETED not in [e.type for e in got]
    assert stream.closed


async def test_litellm_error_before_first_chunk_and_truncation() -> None:
    model = LiteLLMModel("openai/x", completion=Completion(UpstreamError("nope", 401)))
    with pytest.raises(ProviderAuthError):
        await collect(model)
    cut = FakeStream(lchunks(oc({"content": "cut"})))
    with pytest.raises(ProviderResponseError, match="before completion"):
        await collect(LiteLLMModel("openai/x", completion=Completion(cut)))
    assert cut.closed
    with pytest.raises(ProviderResponseError, match="did not return a stream"):
        await collect(LiteLLMModel("openai/x", completion=Completion({"choices": []})))


async def test_litellm_cancellation_closes_stream() -> None:
    stream = FakeStream(lchunks(oc({"content": "first"})), hang=True)
    agent = Agent(
        name="A", instructions="x", model=LiteLLMModel("openai/x", completion=Completion(stream))
    )
    got_first = asyncio.Event()

    async def drive() -> None:
        async for e in Runner().stream_async(agent, "hi", stream_text=True):
            if e.type == RunEventType.TEXT_DELTA:
                got_first.set()

    task = asyncio.create_task(drive())
    await asyncio.wait_for(got_first.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed


async def test_litellm_schema_turn_streams_with_the_schema() -> None:
    # F26: typed turns now stream; the schema still goes to LiteLLM (response_format).
    from pydantic import BaseModel

    class Out(BaseModel):
        n: int

    stream = FakeStream(
        lchunks(
            oc({"role": "assistant", "content": '{"n": '}),
            oc({"content": "2}"}),
            oc({"content": None}, finish="stop"),
            LUSAGE,
        )
    )
    completion = Completion(stream)
    agent = Agent(
        name="A",
        instructions="x",
        model=LiteLLMModel("openai/x", completion=completion),
        output_type=Out,
    )
    events = [e async for e in Runner().stream_async(agent, "hi", stream_text=True)]
    assert completion.calls[0]["stream"] is True
    assert "response_format" in completion.calls[0]
    assert "".join(texts(events)) == '{"n": 2}'


async def test_litellm_generate_still_rejects_unexpected_stream() -> None:
    stream = FakeStream([])
    with pytest.raises(ProviderResponseError, match="streaming"):
        await LiteLLMModel("openai/x", completion=Completion(stream)).generate(USER)
    assert stream.closed


@pytest.mark.asyncio
async def test_same_index_distinct_ids_parallel_calls_regression():
    body = sse(
        oc(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_a",
                        "type": "function",
                        "function": {"name": "add", "arguments": '{"a":1,"b":2}'},
                    }
                ]
            }
        ),
        oc(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_b",
                        "type": "function",
                        "function": {"name": "add", "arguments": '{"a":3,"b":4}'},
                    }
                ]
            }
        ),
        oc(finish="tool_calls"),
    )
    deltas = [d async for d in openai_model(body).stream([Message(role="user", content="x")])]
    ids = {
        d.tool_call_index: d.tool_call_id
        for d in deltas
        if d.kind == "tool_call_delta" and d.tool_call_id
    }
    assert ids == {0: "call_a", 1: "call_b"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code,exc",
    [
        ("rate_limit_exceeded", ProviderRateLimitError),
        ("insufficient_quota", ProviderRateLimitError),
        ("invalid_api_key", ProviderAuthError),
    ],
)
async def test_instream_error_string_codes_typed(code, exc):
    body = sse({"error": {"message": "boom", "type": "x", "code": code}}, done=False)
    with pytest.raises(exc):
        await collect(openai_model(body))


@pytest.mark.asyncio
async def test_multiline_data_event_and_keepalive():
    chunk = json.dumps(oc({"content": "hello"}, "stop"), indent=1).split("\n")
    raw = (
        b"data:\n\n"
        + ("".join(f"data: {x}\n" for x in chunk) + "\n").encode()
        + b"data: [DONE]\n\n"
    )
    events = await collect(openai_model(raw))
    assert "".join(texts(events)) == "hello"
