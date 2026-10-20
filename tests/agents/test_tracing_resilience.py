"""Tracing must never fail a run: a tracer that raises is ignored."""

from __future__ import annotations

from typing import Any

from nexura_agents import Agent, FakeModel, ToolCall, tool, tracing

from nexura_runtime.workflow import ModuleRegistry, WorkflowEngine, WorkflowNode, WorkflowSpec


class BrokenTracer:
    def start_span(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("collector down")

    def start_as_current_span(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("collector down")


class BrokenProvider:
    def get_tracer(self, *args: Any, **kwargs: Any) -> BrokenTracer:
        return BrokenTracer()


@tool
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


async def test_a_broken_tracer_never_fails_an_agent_run() -> None:
    tracing.instrument(tracer_provider=BrokenProvider())
    try:
        model = FakeModel(
            responses=[[ToolCall(id="1", name="add", arguments={"a": 1, "b": 2})], "3"]
        )
        result = await Agent(name="A", instructions="", model=model, tools=[add]).run_async("go")
        assert result.output == "3"
    finally:
        tracing.uninstrument()


async def test_a_broken_tracer_never_fails_a_parallel_workflow() -> None:
    registry = ModuleRegistry()
    registry.register("a", lambda s: {"x": 1})
    registry.register("b", lambda s: {"y": 2})
    spec = WorkflowSpec(
        name="w",
        entry_node="p",
        nodes={"p": WorkflowNode(id="p", handler="", parallel=["a", "b"])},
        terminal_nodes=["p"],
    )
    tracing.instrument(tracer_provider=BrokenProvider())
    try:
        result = await WorkflowEngine(spec, registry).run({})
        assert result.status == "completed" and result.state == {"x": 1, "y": 2}
    finally:
        tracing.uninstrument()
