"""Regressions from the review of TOOL_CALL_DELTA events and OTel metrics (B14)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from nexura_agents import Agent, FakeModel, Runner, ToolCall, tool
from nexura_agents import tracing as nt
from nexura_agents.events import RunEventType
from nexura_agents.providers.fake import StreamingFakeProvider
from nexura_agents.providers.types import StreamDelta
from nexura_agents.types import Usage
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider

SEEN: list[Any] = []


@tool
def lookup(city: str) -> str:
    """Look up."""
    SEEN.append(city)
    return f"sunny in {city}"


def piece(index, args, call_id=None, name=None):
    return StreamDelta(
        kind="tool_call_delta",
        tool_call_index=index,
        tool_call_id=call_id,
        tool_name=name,
        tool_arguments_delta=args,
    )


def fin(reason="tool_calls"):
    return StreamDelta(kind="finish", finish_reason=reason)


def text(t):
    return StreamDelta(kind="text_delta", text=t)


def sagent(*turns):
    model = StreamingFakeProvider("fake-stream", queued_deltas=list(turns))
    return Agent(name="A", instructions="i", model=model, tools=[lookup])


# ---------------------------------------------------------------- F43


async def test_empty_string_id_or_name_in_continuation_chunk_does_not_clobber_call():
    """Compat servers repeat id/name as "" on later chunks. Assembler uses `is not None`,
    so "" overwrites the real id and name (call becomes call_0 / unnamed_tool)."""
    SEEN.clear()
    agent = sagent(
        [
            piece(0, '{"city":', "c1", "lookup"),
            piece(0, ' "Pune"}', "", ""),
            fin(),
        ],
        [text("done"), fin("stop")],
    )
    events = [e async for e in Runner().stream_async(agent, "go", stream_text=True)]
    deltas = [e.data for e in events if e.type == RunEventType.TOOL_CALL_DELTA]
    assert deltas[-1]["id"] == "c1" and deltas[-1]["name"] == "lookup"  # event view
    assert SEEN == ["Pune"], [e.type for e in events]  # the executed call must match


async def test_without_a_provider_id_events_keep_none_and_the_call_runs_as_call_index():
    agent = sagent(
        [piece(0, '{"city": "Goa"}', None, "lookup"), fin()],
        [text("done"), fin("stop")],
    )
    events = [e async for e in Runner().stream_async(agent, "go", stream_text=True)]
    delta_ids = {e.data["id"] for e in events if e.type == RunEventType.TOOL_CALL_DELTA}
    started = [e.data for e in events if e.type == RunEventType.TOOL_STARTED]
    exec_ids = {d.get("id") or d.get("tool_call_id") for d in started}
    # Documented: the event id stays None and the call runs as call_<index>; match by index.
    assert delta_ids == {None}
    assert exec_ids == {"call_0"}


async def test_empty_keepalive_tool_delta_emits_no_event():
    agent = sagent(
        [
            piece(0, '{"city": "Goa"}', "c1", "lookup"),
            StreamDelta(kind="tool_call_delta"),  # nothing at all in it
            fin(),
        ],
        [text("done"), fin("stop")],
    )
    events = [e async for e in Runner().stream_async(agent, "go", stream_text=True)]
    ds = [e.data for e in events if e.type == RunEventType.TOOL_CALL_DELTA]
    assert all(d["arguments_delta"] or d["id"] or d["name"] for d in ds) and len(ds) == 1


# ---------------------------------------------------------------- F44


def _reader_state() -> InMemoryMetricReader:
    memory = InMemoryMetricReader()
    nt.instrument(
        tracer_provider=TracerProvider(), meter_provider=MeterProvider(metric_readers=[memory])
    )
    return memory


@pytest.fixture
def reader() -> Iterator[InMemoryMetricReader]:
    memory = _reader_state()
    try:
        yield memory
    finally:
        nt.uninstrument()


def pts(reader, name):
    data = reader.get_metrics_data()
    if data is None:
        return []
    return [
        p
        for r in data.resource_metrics
        for s in r.scope_metrics
        for m in s.metrics
        if m.name == name
        for p in m.data.data_points
    ]


def durations(reader, op):
    return [
        p
        for p in pts(reader, "gen_ai.client.operation.duration")
        if p.attributes["gen_ai.operation.name"] == op
    ]


async def test_cancelled_tool_duration_carries_error_type(reader):
    started = asyncio.Event()

    @tool
    async def slow() -> str:
        """Slow."""
        started.set()
        await asyncio.sleep(30)
        return "x"

    agent = Agent(
        name="S",
        instructions="i",
        model=FakeModel(responses=[[ToolCall(id="c1", name="slow", arguments={})], "done"]),
        tools=[slow],
    )
    task = asyncio.create_task(Runner().run_async(agent, "go"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    tools = durations(reader, "execute_tool")
    runs = durations(reader, "invoke_agent")
    # Cancelled operations were recorded as plain successes (no error.type).
    assert [p.attributes.get("error.type") for p in tools] != [None], tools
    assert [p.attributes.get("error.type") for p in runs] != [None], runs


async def test_abandoned_stream_chat_duration_not_recorded_as_success(reader):
    """Consumer closes the stream mid model call: chat.end() in `finally` records a success."""
    agent = sagent(
        [text("a"), text("b"), text("c"), fin("stop")],
    )
    gen = Runner().stream_async(agent, "go", stream_text=True)
    async for e in gen:
        if e.type == RunEventType.TEXT_DELTA:
            break
    await gen.aclose()
    chats = durations(reader, "chat")
    assert all(p.attributes.get("error.type") for p in chats), [dict(p.attributes) for p in chats]


async def test_instrument_twice_does_not_double_count():
    a, b = InMemoryMetricReader(), InMemoryMetricReader()
    nt.instrument(
        tracer_provider=TracerProvider(), meter_provider=MeterProvider(metric_readers=[a])
    )
    nt.instrument(
        tracer_provider=TracerProvider(), meter_provider=MeterProvider(metric_readers=[b])
    )
    try:
        usage = Usage(prompt_tokens=3, completion_tokens=2, total_tokens=5)
        agent = Agent(
            name="S", instructions="i", model=FakeModel(responses=["hi"], default_usage=usage)
        )
        await Runner().run_async(agent, "go")
    finally:
        nt.uninstrument()
    assert pts(a, "gen_ai.client.operation.duration") == []
    assert sum(p.count for p in durations(b, "chat")) == 1
    assert sum(p.sum for p in pts(b, "gen_ai.client.token.usage")) == 5


async def test_uninstrument_mid_run_keeps_counts_consistent(reader):
    @tool
    def flip() -> str:
        """Uninstrument while running."""
        nt.uninstrument()
        return "ok"

    usage = Usage(prompt_tokens=3, completion_tokens=2, total_tokens=5)
    agent = Agent(
        name="S",
        instructions="i",
        model=FakeModel(
            responses=[[ToolCall(id="c1", name="flip", arguments={})], "done"], default_usage=usage
        ),
        tools=[flip],
    )
    await Runner().run_async(agent, "go")
    ops = {
        p.attributes["gen_ai.operation.name"]
        for p in pts(reader, "gen_ai.client.operation.duration")
    }
    # chat#1, tool and run all started instrumented; the tool must still have been timed.
    assert {"invoke_agent", "chat", "execute_tool"} <= ops, ops


async def test_old_api_without_bucket_advisory():
    class OldMeter:
        def __init__(self, real):
            self.real = real

        def create_histogram(self, name, unit="", description=""):
            return self.real.create_histogram(name, unit=unit, description=description)

    class OldProvider:
        def __init__(self, real):
            self.real = real

        def get_meter(self, name, *a, **k):
            return OldMeter(self.real.get_meter(name))

    mem = InMemoryMetricReader()
    nt.instrument(
        tracer_provider=TracerProvider(),
        meter_provider=OldProvider(MeterProvider(metric_readers=[mem])),
    )
    try:
        agent = Agent(name="S", instructions="i", model=FakeModel(responses=["hi"]))
        await Runner().run_async(agent, "go")
    finally:
        nt.uninstrument()
    assert durations(mem, "invoke_agent")


async def test_team_does_not_double_count_tokens(reader):
    from nexura_agents import SequentialAgent

    usage = Usage(prompt_tokens=3, completion_tokens=2, total_tokens=5)

    def mk(n):
        return Agent(
            name=n, instructions="i", model=FakeModel(responses=["x"], default_usage=usage)
        )

    team = SequentialAgent("T", [mk("A"), mk("B")])
    await Runner().run_team_async(team, "go") if hasattr(
        Runner, "run_team_async"
    ) else await team.run_async("go")
    total = sum(p.sum for p in pts(reader, "gen_ai.client.token.usage"))
    assert total == 10
    assert sum(p.count for p in durations(reader, "chat")) == 2
    assert sum(p.count for p in durations(reader, "invoke_team")) == 1


async def test_cancel_token_stop_records_error_type(reader):
    from nexura_agents.cancel import CancelToken

    token = CancelToken()

    @tool
    def stopper() -> str:
        """Cancel."""
        token.cancel()
        return "x"

    agent = Agent(
        name="S",
        instructions="i",
        model=FakeModel(responses=[[ToolCall(id="c1", name="stopper", arguments={})], "done"]),
        tools=[stopper],
    )
    events = [e async for e in Runner().stream_async(agent, "go", cancel=token)]
    assert events[-1].type == RunEventType.RUN_CANCELLED
    runs = durations(reader, "invoke_agent")
    assert [p.attributes.get("error.type") for p in runs] != [None]
