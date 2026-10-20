"""WorkflowEngine.stream: node events, pauses, failures, sub-workflows and early close."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from nexura_agents.events import RunEvent, RunEventType
from nexura_runtime.errors import WorkflowExecutionError

from nexura_runtime.workflow import (
    MemoryWorkflowStore,
    ModuleRegistry,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
    interrupt,
)

T = RunEventType


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
    reg.register("three", lambda s: {"c": 3})
    reg.register("ask", lambda s: {"answer": interrupt("name?")})

    def bad(state: dict[str, Any]) -> dict[str, Any]:
        raise ValueError("nope")

    reg.register("bad", bad)
    return reg


async def collect(engine: WorkflowEngine, state: dict[str, Any] | None = None) -> list[RunEvent]:
    return [e async for e in engine.stream(state or {})]


def kinds(events: list[RunEvent]) -> list[tuple[str, str | None]]:
    return [(e.type.value, e.data.get("node_id")) for e in events]


async def test_node_events_in_order_and_result_matches_run() -> None:
    spec = chain("pipe", WorkflowNode(id="n1", handler="one"), WorkflowNode(id="n2", handler="two"))
    events = await collect(WorkflowEngine(spec, registry()))
    assert kinds(events) == [
        ("workflow_started", None),
        ("node_started", "n1"),
        ("node_completed", "n1"),
        ("node_started", "n2"),
        ("node_completed", "n2"),
        ("workflow_completed", None),
    ]
    assert all(e.data["workflow"] == "pipe" for e in events)
    assert events[1].data["step"] == 1 and events[3].data["step"] == 2
    assert events[2].data["updated_keys"] == ["a"] and events[2].data["attempts"] == 1
    final = events[-1].data["result"]
    plain = await WorkflowEngine(spec, registry()).run({})
    assert (final.status, final.state, final.step_count) == (
        plain.status,
        plain.state,
        plain.step_count,
    )
    assert len({e.run_id for e in events}) == 1 and events[0].data["workflow_run_id"] == (
        final.workflow_run_id
    )


async def test_approval_gate_emits_node_paused_and_resume_stream_continues() -> None:
    spec = chain(
        "gated",
        WorkflowNode(id="n1", handler="one"),
        WorkflowNode(id="n2", handler="two", requires_approval=True),
    )
    engine = WorkflowEngine(spec, registry(), store=MemoryWorkflowStore())
    events = await collect(engine)
    assert kinds(events)[-2:] == [("node_paused", "n2"), ("workflow_paused", None)]
    assert events[-2].data["reason"] == "approval"
    paused = events[-1].data["result"]
    assert paused.status == "paused" and paused.pending_approval is not None

    resumed = [
        e
        async for e in engine.resume_stream(
            paused.workflow_run_id, paused.pending_approval.approval_id
        )
    ]
    assert ("node_started", "n2") in kinds(resumed)
    assert resumed[-1].type == T.WORKFLOW_COMPLETED
    assert resumed[-1].data["result"].state == {"a": 1, "b": 2}


async def test_interrupt_emits_node_paused() -> None:
    spec = chain("ask", WorkflowNode(id="ask", handler="ask"))
    events = await collect(WorkflowEngine(spec, registry(), store=MemoryWorkflowStore()))
    assert kinds(events)[-3:] == [
        ("node_started", "ask"),
        ("node_paused", "ask"),
        ("workflow_paused", None),
    ]
    assert events[-2].data["reason"] == "interrupt"


async def test_failure_yields_node_failed_workflow_failed_then_raises() -> None:
    spec = chain("fail", WorkflowNode(id="n1", handler="one"), WorkflowNode(id="b", handler="bad"))
    seen: list[RunEvent] = []
    with pytest.raises(WorkflowExecutionError):
        async for event in WorkflowEngine(spec, registry()).stream({}):
            seen.append(event)
    assert kinds(seen)[-2:] == [("node_failed", "b"), ("workflow_failed", None)]
    assert "nope" in seen[-2].data["error"]
    assert seen[-1].data["error_type"] == "WorkflowExecutionError"


async def test_subworkflow_nodes_are_tagged_with_their_workflow() -> None:
    inner = chain("inner", WorkflowNode(id="i1", handler="one"))
    outer = chain("outer", WorkflowNode(id="sub", handler="", subgraph=inner))
    events = await collect(WorkflowEngine(outer, registry()))
    tagged = {(e.data["workflow"], e.data["node_id"]) for e in events if e.type == T.NODE_STARTED}
    assert tagged == {("outer", "sub"), ("inner", "i1")}
    assert events[-1].type == T.WORKFLOW_COMPLETED and events[-1].data["workflow"] == "outer"


async def test_parallel_step_reports_as_one_node() -> None:
    spec = chain("par", WorkflowNode(id="gather", handler="", parallel=["one", "three"]))
    events = await collect(WorkflowEngine(spec, registry()))
    assert [k for k in kinds(events) if k[1]] == [
        ("node_started", "gather"),
        ("node_completed", "gather"),
    ]
    assert events[-1].type == T.WORKFLOW_COMPLETED


async def test_early_close_cancels_the_run() -> None:
    cancelled = asyncio.Event()
    reg = ModuleRegistry()

    async def slow(state: dict[str, Any]) -> dict[str, Any]:
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return {}

    reg.register("slow", slow)
    reg.register("after", lambda s: {"late": True})
    spec = chain(
        "slow", WorkflowNode(id="s", handler="slow"), WorkflowNode(id="t", handler="after")
    )
    stream = WorkflowEngine(spec, reg).stream({})
    assert (await anext(stream)).type == T.WORKFLOW_STARTED
    assert (await anext(stream)).type == T.NODE_STARTED
    await asyncio.sleep(0.05)
    await stream.aclose()
    assert cancelled.is_set()


async def test_run_without_stream_emits_nothing_and_is_unchanged() -> None:
    spec = chain("pipe", WorkflowNode(id="n1", handler="one"))
    result = await WorkflowEngine(spec, registry()).run({})
    assert result.status == "completed" and result.state == {"a": 1}
