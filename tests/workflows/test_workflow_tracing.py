"""OpenTelemetry spans for the workflow engine: runs, nodes, sub-workflows, parallel steps."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from nexura_agents import tracing
from nexura_runtime.errors import WorkflowExecutionError
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from nexura_runtime.workflow import (
    MemoryWorkflowStore,
    ModuleRegistry,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
    interrupt,
)


@pytest.fixture
def exporter() -> Iterator[InMemorySpanExporter]:
    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    tracing.instrument(tracer_provider=provider)
    yield memory
    tracing.uninstrument()


def named(exporter: InMemorySpanExporter, prefix: str) -> list[ReadableSpan]:
    return [s for s in exporter.get_finished_spans() if s.name.startswith(prefix)]


def one(exporter: InMemorySpanExporter, name: str) -> ReadableSpan:
    found = [s for s in exporter.get_finished_spans() if s.name == name]
    assert len(found) == 1, [s.name for s in exporter.get_finished_spans()]
    return found[0]


def parent_of(span: ReadableSpan) -> int | None:
    return span.parent.span_id if span.parent else None


def chain(name: str, *nodes: WorkflowNode) -> WorkflowSpec:
    linked = {}
    for i, node in enumerate(nodes):
        node.default_transition = nodes[i + 1].id if i + 1 < len(nodes) else None
        linked[node.id] = node
    return WorkflowSpec(
        name=name, entry_node=nodes[0].id, nodes=linked, terminal_nodes=[nodes[-1].id]
    )


def registry() -> ModuleRegistry:
    reg = ModuleRegistry()
    reg.register("one", lambda s: {"a": 1})
    reg.register("two", lambda s: {"b": s["a"] + 1})
    reg.register("ask", lambda s: {"answer": interrupt("name?")})
    return reg


async def test_run_node_spans_and_attributes(exporter: InMemorySpanExporter) -> None:
    spec = chain(
        "pipeline", WorkflowNode(id="n1", handler="one"), WorkflowNode(id="n2", handler="two")
    )
    result = await WorkflowEngine(spec, registry()).run({}, workflow_run_id="run-1")
    assert result.status == "completed"

    run = one(exporter, "workflow pipeline")
    assert run.attributes is not None
    assert run.attributes["gen_ai.operation.name"] == "workflow"
    assert run.attributes["nexura.workflow.run_id"] == "run-1"
    assert run.attributes["nexura.workflow.status"] == "completed"
    assert run.attributes["nexura.workflow.step_count"] == 2
    assert run.attributes["nexura.workflow.operation"] == "run"
    assert parent_of(run) is None
    for node_id in ("n1", "n2"):
        node = one(exporter, f"workflow.node {node_id}")
        assert parent_of(node) == run.context.span_id
        assert node.attributes is not None
        assert node.attributes["nexura.workflow.node.attempts"] == 1


async def test_retry_attempts_are_counted_and_not_an_error(
    exporter: InMemorySpanExporter,
) -> None:
    calls: list[int] = []
    reg = ModuleRegistry()

    def flaky(state: dict[str, Any]) -> dict[str, Any]:
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionError("reset")
        return {"ok": True}

    reg.register("flaky", flaky)
    spec = chain(
        "retrying",
        WorkflowNode(id="f", handler="flaky", retry={"max_attempts": 3, "initial_delay": 0.001}),
    )
    await WorkflowEngine(spec, reg).run({})
    node = one(exporter, "workflow.node f")
    assert node.attributes is not None
    assert node.attributes["nexura.workflow.node.attempts"] == 3
    assert node.status.status_code is not StatusCode.ERROR
    assert [e.name for e in node.events].count("nexura.workflow.retry") == 2


async def test_failing_node_marks_node_and_workflow_as_error(
    exporter: InMemorySpanExporter,
) -> None:
    reg = ModuleRegistry()

    def bad(state: dict[str, Any]) -> dict[str, Any]:
        raise ValueError("malformed")

    reg.register("bad", bad)
    spec = chain("failing", WorkflowNode(id="b", handler="bad"))
    with pytest.raises(WorkflowExecutionError):
        await WorkflowEngine(spec, reg).run({})
    node = one(exporter, "workflow.node b")
    run = one(exporter, "workflow failing")
    assert node.status.status_code is StatusCode.ERROR
    assert any(e.name == "exception" for e in node.events)
    assert run.status.status_code is StatusCode.ERROR
    assert run.attributes is not None and run.attributes["nexura.workflow.status"] == "failed"


async def test_approval_pause_and_resume(exporter: InMemorySpanExporter) -> None:
    spec = chain(
        "gated",
        WorkflowNode(id="n1", handler="one"),
        WorkflowNode(id="n2", handler="two", requires_approval=True),
    )
    engine = WorkflowEngine(spec, registry(), store=MemoryWorkflowStore())
    paused = await engine.run({}, workflow_run_id="g1")
    assert paused.status == "paused" and paused.pending_approval is not None

    run = one(exporter, "workflow gated")
    assert run.status.status_code is not StatusCode.ERROR
    assert run.attributes is not None and run.attributes["nexura.workflow.status"] == "paused"
    gate = one(exporter, "workflow.node n2")
    assert gate.attributes is not None
    assert gate.attributes["nexura.workflow.pause_reason"] == "approval"
    assert gate.status.status_code is not StatusCode.ERROR
    assert parent_of(gate) == run.context.span_id

    exporter.clear()
    done = await engine.resume("g1", paused.pending_approval.approval_id, actor_role="admin")
    assert done.status == "completed"
    resumed = one(exporter, "workflow gated")
    assert resumed.attributes is not None
    assert resumed.attributes["nexura.workflow.operation"] == "resume"
    assert resumed.attributes["nexura.workflow.status"] == "completed"
    assert resumed.attributes["nexura.workflow.run_id"] == "g1"
    assert parent_of(one(exporter, "workflow.node n2")) == resumed.context.span_id


async def test_rejected_approval_has_a_resume_span(exporter: InMemorySpanExporter) -> None:
    spec = chain("gated", WorkflowNode(id="n2", handler="two", requires_approval=True))
    engine = WorkflowEngine(spec, registry(), store=MemoryWorkflowStore())
    paused = await engine.run({"a": 1}, workflow_run_id="g2")
    assert paused.pending_approval is not None
    exporter.clear()
    rejected = await engine.resume(
        "g2", paused.pending_approval.approval_id, decision="reject", actor_role="admin"
    )
    assert rejected.status == "rejected"
    span = one(exporter, "workflow gated")
    assert span.attributes is not None
    assert span.attributes["nexura.workflow.status"] == "rejected"


async def test_interrupt_pause_resume_interrupt_and_fork(exporter: InMemorySpanExporter) -> None:
    spec = chain("asking", WorkflowNode(id="q", handler="ask"))
    engine = WorkflowEngine(spec, registry(), store=MemoryWorkflowStore())
    paused = await engine.run({}, workflow_run_id="i1")
    assert paused.interrupt is not None
    node = one(exporter, "workflow.node q")
    assert node.attributes is not None
    assert node.attributes["nexura.workflow.pause_reason"] == "interrupt"
    assert node.status.status_code is not StatusCode.ERROR

    exporter.clear()
    done = await engine.resume_interrupt("i1", paused.interrupt["interrupt_id"], "Asha")
    assert done.status == "completed"
    run = one(exporter, "workflow asking")
    assert run.attributes is not None
    assert run.attributes["nexura.workflow.operation"] == "resume_interrupt"

    exporter.clear()
    forked = await engine.fork("i1", 0, new_run_id="i1-fork")
    assert forked.status == "paused"
    fork = one(exporter, "workflow asking")
    assert fork.attributes is not None
    assert fork.attributes["nexura.workflow.operation"] == "fork"
    assert fork.attributes["nexura.workflow.run_id"] == "i1-fork"


async def test_sub_workflow_nests_under_its_parent_node(exporter: InMemorySpanExporter) -> None:
    child = chain("child", WorkflowNode(id="c1", handler="one"))
    spec = chain(
        "parent",
        WorkflowNode(id="sub", handler="", subgraph=child),
        WorkflowNode(id="after", handler="two"),
    )
    await WorkflowEngine(spec, registry(), store=MemoryWorkflowStore()).run({})
    parent_run = one(exporter, "workflow parent")
    sub_node = one(exporter, "workflow.node sub")
    child_run = one(exporter, "workflow child")
    child_node = one(exporter, "workflow.node c1")
    assert parent_of(sub_node) == parent_run.context.span_id
    assert parent_of(child_run) == sub_node.context.span_id
    assert parent_of(child_node) == child_run.context.span_id
    assert child_run.context.trace_id == parent_run.context.trace_id


async def test_sub_workflow_pause_marks_the_parent_node(exporter: InMemorySpanExporter) -> None:
    child = chain("child", WorkflowNode(id="c1", handler="ask"))
    spec = chain("parent", WorkflowNode(id="sub", handler="", subgraph=child))
    paused = await WorkflowEngine(spec, registry(), store=MemoryWorkflowStore()).run({})
    assert paused.status == "paused"
    sub_node = one(exporter, "workflow.node sub")
    assert sub_node.attributes is not None
    assert sub_node.attributes["nexura.workflow.pause_reason"] == "interrupt"
    assert sub_node.status.status_code is not StatusCode.ERROR


async def test_parallel_branches_keep_the_node_as_parent(exporter: InMemorySpanExporter) -> None:
    reg = ModuleRegistry()

    async def slow(state: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(0.02)
        return {"slow": 1}

    async def fast(state: dict[str, Any]) -> dict[str, Any]:
        return {"fast": 1}

    reg.register("slow", slow)
    reg.register("fast", fast)
    spec = chain("fan", WorkflowNode(id="gather", handler="", parallel=["slow", "fast"]))
    await WorkflowEngine(spec, reg).run({})
    node = one(exporter, "workflow.node gather")
    branches = named(exporter, "workflow.branch")
    assert sorted(b.name for b in branches) == ["workflow.branch fast", "workflow.branch slow"]
    assert {parent_of(b) for b in branches} == {node.context.span_id}


async def test_map_items_get_child_spans_and_sync_handlers_inherit_context(
    exporter: InMemorySpanExporter,
) -> None:
    from opentelemetry import trace

    seen: list[int] = []
    reg = ModuleRegistry()

    def classify(state: dict[str, Any]) -> dict[str, Any]:
        # Sync handlers run in a worker thread; the current span must still be the item span.
        seen.append(trace.get_current_span().get_span_context().span_id)
        return {"labels": state["item_index"]}

    reg.register("classify", classify)
    spec = chain(
        "triage",
        WorkflowNode(
            id="each",
            handler="classify",
            map_over="items",
            map_as="item",
            reducers={"labels": "append"},
            max_concurrency=2,
        ),
    )
    await WorkflowEngine(spec, reg).run({"items": ["a", "b", "c"]})
    node = one(exporter, "workflow.node each")
    items = named(exporter, "workflow.map_item")
    assert len(items) == 3
    assert {parent_of(i) for i in items} == {node.context.span_id}
    assert sorted(seen) == sorted(i.context.span_id for i in items)


async def test_state_is_not_recorded_by_default(exporter: InMemorySpanExporter) -> None:
    spec = chain("plain", WorkflowNode(id="n1", handler="one"))
    await WorkflowEngine(spec, registry()).run({"customer": "Asha"})
    for span in exporter.get_finished_spans():
        assert not span.events, span.name


async def test_capture_content_records_redacted_state() -> None:
    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    tracing.instrument(tracer_provider=provider, capture_content=True)
    try:
        spec = chain("plain", WorkflowNode(id="n1", handler="one"))
        await WorkflowEngine(spec, registry()).run(
            {"customer": "Asha", "api_key": "sk-abcdefghijklmnop"}
        )
    finally:
        tracing.uninstrument()
    run = one(memory, "workflow plain")
    event = next(e for e in run.events if e.name == "nexura.workflow.input")
    assert event.attributes is not None
    state = str(event.attributes["nexura.workflow.state"])
    assert "Asha" in state and "sk-abcdefghijklmnop" not in state and "[REDACTED]" in state
    assert any(e.name == "nexura.workflow.output" for e in run.events)
    node = one(memory, "workflow.node n1")
    assert any(e.name == "nexura.workflow.output" for e in node.events)


async def test_uninstrument_stops_workflow_spans() -> None:
    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    tracing.instrument(tracer_provider=provider)
    spec = chain("plain", WorkflowNode(id="n1", handler="one"))
    await WorkflowEngine(spec, registry()).run({})
    emitted = len(memory.get_finished_spans())
    assert emitted == 2
    tracing.uninstrument()
    await WorkflowEngine(spec, registry()).run({})
    assert len(memory.get_finished_spans()) == emitted


async def test_agent_used_inside_a_workflow_node_nests_under_the_node(
    exporter: InMemorySpanExporter,
) -> None:
    from nexura_agents import Agent, FakeModel, Runner

    reg = ModuleRegistry()

    async def ask_agent(state: dict[str, Any]) -> dict[str, Any]:
        agent = Agent(name="Helper", instructions="", model=FakeModel(responses=["hi"]))
        result = await Runner().run_async(agent, "hello")
        return {"reply": result.final_output}

    reg.register("ask_agent", ask_agent)
    spec = chain("agentic", WorkflowNode(id="think", handler="ask_agent"))
    await WorkflowEngine(spec, reg).run({})
    node = one(exporter, "workflow.node think")
    assert parent_of(one(exporter, "invoke_agent Helper")) == node.context.span_id
