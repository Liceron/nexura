"""Public tracing helpers and the conversation id on run spans (F50)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from nexura_agents import Agent, FakeModel, MemorySessionStore, Runner, ToolCall, tool, tracing
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    tracing.instrument(tracer_provider=provider, metrics=False)
    try:
        yield memory
    finally:
        tracing.uninstrument()


def by_name(memory: InMemorySpanExporter, prefix: str) -> list[Any]:
    return [s for s in memory.get_finished_spans() if s.name.startswith(prefix)]


async def test_runs_in_a_session_carry_the_conversation_id(spans: InMemorySpanExporter) -> None:
    agent = Agent(name="A", instructions="i", model=FakeModel(responses=["one", "two"]))
    runner = Runner(session_store=MemorySessionStore())
    await runner.run_async(agent, "hi", session_id="conv-7")
    await runner.run_async(agent, "again", session_id="conv-7")
    await Runner().run_async(
        Agent(name="B", instructions="i", model=FakeModel(responses=["x"])), "x"
    )
    runs = by_name(spans, "invoke_agent")
    ids = [r.attributes.get("gen_ai.conversation.id") for r in runs]
    assert ids == ["conv-7", "conv-7", None]


async def test_conversation_id_is_kept_on_resume(spans: InMemorySpanExporter) -> None:
    @tool(requires_approval=True)
    def charge() -> str:
        """Charge."""
        return "ok"

    model = FakeModel(responses=[[ToolCall(id="c1", name="charge", arguments={})], "done"])
    agent = Agent(name="A", instructions="i", model=model, tools=[charge])
    runner = Runner(session_store=MemorySessionStore())
    paused = await runner.run_async(agent, "go", session_id="conv-9")
    await runner.resume_async(agent, paused.interruption, approve=True)
    runs = by_name(spans, "invoke_agent")
    assert [r.attributes.get("gen_ai.conversation.id") for r in runs] == ["conv-9", "conv-9"]


async def test_trace_groups_runs_under_one_parent(spans: InMemorySpanExporter) -> None:
    agent = Agent(name="A", instructions="i", model=FakeModel(responses=["one", "two"]))
    with tracing.trace("nightly batch", group_id="batch-42", metadata={"owner": "ops"}):
        await Runner().run_async(agent, "a")
        await Runner().run_async(agent, "b")
    (parent,) = by_name(spans, "trace nightly batch")
    assert parent.attributes["nexura.trace.group_id"] == "batch-42"
    assert parent.attributes["nexura.trace.metadata.owner"] == "ops"
    runs = by_name(spans, "invoke_agent")
    assert len(runs) == 2
    assert all(r.parent is not None and r.parent.span_id == parent.context.span_id for r in runs)
    assert all(r.context.trace_id == parent.context.trace_id for r in runs)


async def test_span_inside_a_tool_nests_under_the_tool_call(spans: InMemorySpanExporter) -> None:
    @tool
    def fetch(city: str) -> str:
        """Fetch."""
        with tracing.span("load cache", city=city, hits=3) as step:
            step.set("warm", True)
        return "ok"

    model = FakeModel(
        responses=[[ToolCall(id="c1", name="fetch", arguments={"city": "Pune"})], "d"]
    )
    await Runner().run_async(Agent(name="A", instructions="i", model=model, tools=[fetch]), "go")
    (custom,) = by_name(spans, "load cache")
    (tool_span,) = by_name(spans, "execute_tool fetch")
    assert custom.parent is not None and custom.parent.span_id == tool_span.context.span_id
    assert custom.attributes["city"] == "Pune" and custom.attributes["hits"] == 3
    assert custom.attributes["warm"] is True


def test_span_attributes_are_filtered_and_redacted(spans: InMemorySpanExporter) -> None:
    with tracing.span(
        "step",
        note="Authorization: Bearer abc.def.ghi",
        api_key="sk-live-123",
        obj=object(),
        nan=float("nan"),
        ok=1.5,
    ) as step:
        step.set("password", "hunter2")
        step.set("items", [1, 2])
    (s,) = by_name(spans, "step")
    attrs = dict(s.attributes or {})
    assert "abc.def.ghi" not in attrs["note"]
    assert "api_key" not in attrs and "password" not in attrs
    assert "obj" not in attrs and "nan" not in attrs and "items" not in attrs
    assert attrs["ok"] == 1.5


def test_a_failing_block_marks_the_span_and_reraises(spans: InMemorySpanExporter) -> None:
    from opentelemetry.trace import StatusCode

    with pytest.raises(ValueError, match="boom"), tracing.span("risky"):
        raise ValueError("boom")
    (s,) = by_name(spans, "risky")
    assert s.status.status_code == StatusCode.ERROR
    assert s.attributes["error.type"] == "ValueError"


def test_helpers_do_nothing_when_tracing_is_off() -> None:
    assert not tracing.is_instrumented()
    with tracing.trace("t", group_id="g") as handle, tracing.span("s", a=1) as inner:
        inner.set("b", 2)
    assert handle is tracing.NOOP


async def test_custom_spans_record_no_duration_metrics() -> None:
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    reader = InMemoryMetricReader()
    tracing.instrument(
        tracer_provider=TracerProvider(), meter_provider=MeterProvider(metric_readers=[reader])
    )
    try:
        with tracing.span("mine"):
            pass
    finally:
        tracing.uninstrument()
    data = reader.get_metrics_data()
    assert data is None or not any(
        m.name == "gen_ai.client.operation.duration"
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
    )
