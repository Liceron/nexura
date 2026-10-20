"""OpenTelemetry metrics next to spans: token usage and operation duration (F44)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from nexura_agents import Agent, FakeModel, Runner, ToolCall, tool
from nexura_agents import tracing as nexura_tracing
from nexura_agents.errors import ToolExecutionError
from nexura_agents.types import Usage
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider


class Named(FakeModel):
    model = "small-model"


@tool
def lookup(city: str) -> str:
    """Look up."""
    return "sunny"


@tool
def broken() -> str:
    """Always fails."""
    raise RuntimeError("down")


@pytest.fixture
def reader() -> Iterator[InMemoryMetricReader]:
    memory = InMemoryMetricReader()
    nexura_tracing.instrument(
        tracer_provider=TracerProvider(), meter_provider=MeterProvider(metric_readers=[memory])
    )
    try:
        yield memory
    finally:
        nexura_tracing.uninstrument()


def points(reader: InMemoryMetricReader, name: str) -> list[Any]:
    data = reader.get_metrics_data()
    if data is None:
        return []
    return [
        point
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == name
        for point in metric.data.data_points
    ]


def agent(*responses: Any, tools: list[Any] | None = None) -> Agent:
    usage = Usage(prompt_tokens=30, completion_tokens=5, total_tokens=35)
    return Agent(
        name="Support",
        instructions="i",
        model=Named(responses=list(responses), default_usage=usage),
        tools=tools or [],
    )


async def test_token_usage_is_recorded_per_model_call_by_type(
    reader: InMemoryMetricReader,
) -> None:
    calls = [ToolCall(id="c1", name="lookup", arguments={"city": "Pune"})]
    await Runner().run_async(agent(calls, "done", tools=[lookup]), "go")
    by_type = {
        p.attributes["gen_ai.token.type"]: p for p in points(reader, "gen_ai.client.token.usage")
    }
    assert (by_type["input"].sum, by_type["input"].count) == (60, 2)  # two model calls
    assert (by_type["output"].sum, by_type["output"].count) == (10, 2)
    assert by_type["input"].attributes["gen_ai.request.model"] == "small-model"
    assert by_type["input"].attributes["gen_ai.operation.name"] == "chat"


async def test_durations_cover_runs_model_calls_and_tools(reader: InMemoryMetricReader) -> None:
    calls = [ToolCall(id="c1", name="lookup", arguments={"city": "Pune"})]
    await Runner().run_async(agent(calls, "done", tools=[lookup]), "go")
    seen = {
        p.attributes["gen_ai.operation.name"]: p
        for p in points(reader, "gen_ai.client.operation.duration")
    }
    assert seen["invoke_agent"].count == 1
    assert seen["invoke_agent"].attributes["gen_ai.agent.name"] == "Support"
    assert seen["chat"].count == 2
    assert seen["execute_tool"].attributes["gen_ai.tool.name"] == "lookup"
    for point in seen.values():
        assert point.sum >= 0
        # Low-cardinality attributes only: no call ids or run ids on metrics.
        assert "gen_ai.tool.call.id" not in point.attributes
        assert "nexura.run_id" not in point.attributes


async def test_failures_carry_the_error_type(reader: InMemoryMetricReader) -> None:
    calls = [ToolCall(id="c1", name="broken", arguments={})]
    with pytest.raises(ToolExecutionError):
        await Runner().run_async(agent(calls, "done", tools=[broken]), "go")
    tools = [
        p
        for p in points(reader, "gen_ai.client.operation.duration")
        if p.attributes["gen_ai.operation.name"] == "execute_tool"
    ]
    assert [p.attributes.get("error.type") for p in tools] == ["ToolExecutionError"]
    runs = [
        p
        for p in points(reader, "gen_ai.client.operation.duration")
        if p.attributes["gen_ai.operation.name"] == "invoke_agent"
    ]
    assert [p.attributes.get("error.type") for p in runs] == ["ToolExecutionError"]


async def test_metrics_can_be_switched_off() -> None:
    memory = InMemoryMetricReader()
    nexura_tracing.instrument(
        tracer_provider=TracerProvider(),
        meter_provider=MeterProvider(metric_readers=[memory]),
        metrics=False,
    )
    try:
        await Runner().run_async(agent("hi"), "go")
    finally:
        nexura_tracing.uninstrument()
    assert points(memory, "gen_ai.client.token.usage") == []
    assert points(memory, "gen_ai.client.operation.duration") == []


async def test_nothing_is_recorded_after_uninstrument() -> None:
    memory = InMemoryMetricReader()
    nexura_tracing.instrument(
        tracer_provider=TracerProvider(), meter_provider=MeterProvider(metric_readers=[memory])
    )
    nexura_tracing.uninstrument()
    await Runner().run_async(agent("hi"), "go")
    assert points(memory, "gen_ai.client.operation.duration") == []


async def test_a_tool_waiting_for_approval_is_not_timed(reader: InMemoryMetricReader) -> None:
    @tool(requires_approval=True)
    def charge() -> str:
        """Charge."""
        return "ok"

    paused = await Runner().run_async(
        agent([ToolCall(id="c1", name="charge", arguments={})], "done", tools=[charge]), "go"
    )
    assert paused.status == "paused"
    tools = [
        p
        for p in points(reader, "gen_ai.client.operation.duration")
        if p.attributes["gen_ai.operation.name"] == "execute_tool"
    ]
    assert tools == []
