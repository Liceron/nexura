"""P38 C2: workflow tracing on the pinned GenAI conventions, plus Nexura approval and effect
events, checked with the real OpenTelemetry SDK (in-memory span exporter and metric reader).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from nexura_agents import tracing
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from nexura_runtime.integrations.nexura_v2.exporter import _attributes
from nexura_runtime.observability.models import SpanRecord
from nexura_runtime.workflow import (
    MemoryWorkflowStore,
    ModuleRegistry,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
    request_approval,
)


class Telemetry:
    def __init__(self) -> None:
        self.spans = InMemorySpanExporter()
        self.metrics = InMemoryMetricReader()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(self.spans))
        tracing.instrument(
            tracer_provider=provider, meter_provider=MeterProvider(metric_readers=[self.metrics])
        )

    def finished(self) -> list[ReadableSpan]:
        return list(self.spans.get_finished_spans())

    def named(self, name: str) -> list[ReadableSpan]:
        return [s for s in self.finished() if s.name == name]

    def events(self, name: str) -> list[dict[str, Any]]:
        return [
            dict(e.attributes or {}) for s in self.finished() for e in s.events if e.name == name
        ]

    def points(self, name: str) -> list[Any]:
        data = self.metrics.get_metrics_data()
        if data is None:
            return []
        return [
            p
            for rm in data.resource_metrics
            for sm in rm.scope_metrics
            for m in sm.metrics
            if m.name == name
            for p in m.data.data_points
        ]


@pytest.fixture
def otel() -> Iterator[Telemetry]:
    telemetry = Telemetry()
    try:
        yield telemetry
    finally:
        tracing.uninstrument()


def gated_spec() -> WorkflowSpec:
    return WorkflowSpec(
        name="refunds",
        entry_node="a",
        nodes={
            "a": WorkflowNode(id="a", handler="note", default_transition="b"),
            "b": WorkflowNode(id="b", handler="pay", requires_approval=True),
        },
        terminal_nodes=["b"],
    )


def registry(paid: list[int]) -> ModuleRegistry:
    reg = ModuleRegistry()

    @reg.register_handler("note", effect="pure")
    def note(state: dict[str, Any]) -> dict[str, Any]:
        return {"noted": True}

    def pay(state: dict[str, Any]) -> dict[str, Any]:
        paid.append(1)
        return {"paid": "card 4111 charged"}

    reg.register("pay", pay)  # undeclared effect: non_idempotent, journaled
    return reg


async def test_invoke_workflow_span_and_metric(otel: Telemetry) -> None:
    spec = WorkflowSpec(
        name="pipeline",
        entry_node="a",
        nodes={"a": WorkflowNode(id="a", handler="note")},
        terminal_nodes=["a"],
    )
    result = await WorkflowEngine(spec, registry([])).run({}, workflow_run_id="p1")
    assert result.status == "completed"
    (run,) = otel.named("invoke_workflow pipeline")
    assert run.kind == SpanKind.INTERNAL
    assert run.attributes["gen_ai.operation.name"] == "invoke_workflow"
    assert run.attributes["gen_ai.workflow.name"] == "pipeline"
    assert run.attributes["nexura.operation.name"] == "workflow"  # deprecated alias
    points = otel.points("gen_ai.invoke_workflow.duration")
    assert [dict(p.attributes) for p in points] == [{"gen_ai.workflow.name": "pipeline"}]
    old = otel.points("gen_ai.client.operation.duration")
    # The deprecated histogram keeps the pre-0.4.0 operation value until it is removed.
    assert {p.attributes["gen_ai.operation.name"] for p in old} == {"workflow"}


async def test_workflow_approval_and_effect_events(otel: Telemetry) -> None:
    paid: list[int] = []
    engine = WorkflowEngine(gated_spec(), registry(paid), store=MemoryWorkflowStore())
    paused = await engine.run({}, workflow_run_id="w1")
    assert paused.status == "paused" and paused.pending_approval is not None
    approval_id = paused.pending_approval.approval_id

    (requested,) = otel.events("nexura.approval.requested")
    assert requested == {
        "nexura.approval.id": approval_id,
        "nexura.approval.kind": "workflow_node",
        "nexura.approval.reason": "approval",
        "nexura.workflow.node.id": "b",
    }
    (gate,) = otel.named("workflow.node b")
    assert [e.name for e in gate.events] == ["nexura.approval.requested"]

    otel.spans.clear()
    done = await engine.resume("w1", approval_id, actor_role="admin")
    assert done.status == "completed" and paid == [1]
    (resumed,) = otel.named("invoke_workflow refunds")
    decided = [dict(e.attributes) for e in resumed.events if e.name == "nexura.approval.decided"]
    assert decided == [
        {
            "nexura.approval.id": approval_id,
            "nexura.approval.kind": "workflow_node",
            "nexura.approval.decision": "approve",
            "nexura.approval.inputs_edited": False,
        }
    ]
    # Exactly one decided event: nested spans do not repeat it.
    assert len(otel.events("nexura.approval.decided")) == 1

    intents = otel.events("nexura.effect.intent")
    outcomes = otel.events("nexura.effect.outcome")
    assert len(intents) == 1 and len(outcomes) == 1
    assert intents[0]["nexura.effect.class"] == "non_idempotent"
    assert intents[0]["nexura.approval.id"] == approval_id
    assert intents[0]["nexura.effect.id"] == outcomes[0]["nexura.effect.id"]
    assert outcomes[0]["nexura.run_id"] == "w1"
    dump = repr([(s.attributes, [e.attributes for e in s.events]) for s in otel.finished()])
    assert "4111" not in dump  # step results are never recorded


async def test_rejected_workflow_approval(otel: Telemetry) -> None:
    paid: list[int] = []
    engine = WorkflowEngine(gated_spec(), registry(paid), store=MemoryWorkflowStore())
    paused = await engine.run({}, workflow_run_id="w2")
    assert paused.pending_approval is not None
    otel.spans.clear()
    rejected = await engine.resume(
        "w2", paused.pending_approval.approval_id, decision="reject", actor_role="admin"
    )
    assert rejected.status == "rejected" and paid == []
    decided = otel.events("nexura.approval.decided")
    assert [d["nexura.approval.decision"] for d in decided] == ["reject"]
    assert otel.events("nexura.effect.intent") == []
    # A rejection runs nothing: no invoke_workflow.duration point for it (review m7); the
    # deprecated histogram keeps recording it as before 0.4.0.
    before = sum(p.count for p in otel.points("gen_ai.invoke_workflow.duration"))
    assert before == 1  # only the first run, which paused
    legacy = otel.points("gen_ai.client.operation.duration")
    assert sum(p.count for p in legacy if p.attributes["gen_ai.operation.name"] == "workflow") == 2


async def test_node_requested_approval_event(otel: Telemetry) -> None:
    reg = ModuleRegistry()

    def ask(state: dict[str, Any]) -> dict[str, Any]:
        request_approval({"amount": 40})
        return {"ok": True}

    reg.register("ask", ask)
    spec = WorkflowSpec(
        name="asks",
        entry_node="n",
        nodes={"n": WorkflowNode(id="n", handler="ask")},
        terminal_nodes=["n"],
    )
    paused = await WorkflowEngine(spec, reg, store=MemoryWorkflowStore()).run(
        {}, workflow_run_id="q1"
    )
    assert paused.status == "paused" and paused.pending_approval is not None
    (requested,) = otel.events("nexura.approval.requested")
    assert requested["nexura.approval.reason"] == "node_request"
    assert requested["nexura.approval.id"] == paused.pending_approval.approval_id
    # The request details are content: never recorded.
    assert set(requested) == {
        "nexura.approval.id",
        "nexura.approval.kind",
        "nexura.approval.reason",
        "nexura.workflow.node.id",
    }
    assert 40 not in requested.values() and "amount" not in repr(requested)


def test_v2_exporter_forwards_provider_name_and_new_usage() -> None:
    record = SpanRecord(
        trace_id="a" * 32,
        span_id="b" * 16,
        name="chat m",
        start_time=1.0,
        end_time=2.0,
        attributes={
            "gen_ai.provider.name": "openai",
            "gen_ai.system": "openai",
            "gen_ai.usage.cache_read.input_tokens": 4,
            "gen_ai.usage.reasoning.output_tokens": 2,
            "gen_ai.input.messages": "[]",  # content is never forwarded
        },
    )
    out = _attributes(record)
    assert out["gen_ai.provider.name"] == "openai" and out["gen_ai.system"] == "openai"
    assert out["gen_ai.usage.cache_read.input_tokens"] == 4
    assert out["gen_ai.usage.reasoning.output_tokens"] == 2
    assert "gen_ai.input.messages" not in out
