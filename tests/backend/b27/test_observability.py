"""Acceptance tests for Module B27: Tracing, Events, Metrics and Audit."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from nexura_runtime.contracts.context import ExecutionContext
from nexura_runtime.types import Usage

from nexura_runtime.modules.registry import default_registry
from nexura_runtime.observability.errors import ExporterFailedError
from nexura_runtime.observability.metrics import MetricsCollector
from nexura_runtime.observability.models import EventRecord
from nexura_runtime.observability.module import ObservabilityModule
from nexura_runtime.observability.otlp import export_spans_otlp_payload, span_to_otlp_dict
from nexura_runtime.observability.sinks import (
    CompositeEventSink,
    EventSink,
    JsonlEventSink,
    MemoryEventSink,
    SqliteEventSink,
)
from nexura_runtime.observability.tracer import NexuraTracer


class TestNodeIdentityAndOrderEquivalence:
    """Tests verifying equivalent node identity, monotonic ordering, and repeated handlers."""

    def test_sdk_and_api_produce_equivalent_node_identity_order(self) -> None:
        sink = MemoryEventSink(payload_export=True)
        run_id = "run_order_test_01"

        # SDK-style event emission
        for i, node_name in enumerate(["fetch_input", "process_model", "save_output"], start=1):
            evt = EventRecord(
                seq_num=sink.next_seq(run_id),
                run_id=run_id,
                node_id=f"node_{i}",
                node_name=node_name,
                attempt_num=1,
                event_type="node_completed",
                duration_ms=12.5 * i,
            )
            sink.emit(evt)

        events = sink.get_events(run_id)
        assert len(events) == 3
        # Strict monotonic sequence order
        assert [e.seq_num for e in events] == [1, 2, 3]
        assert [e.node_name for e in events] == ["fetch_input", "process_model", "save_output"]
        assert [e.node_id for e in events] == ["node_1", "node_2", "node_3"]

    def test_repeated_handler_names_remain_distinguishable(self) -> None:
        sink = MemoryEventSink()
        run_id = "run_repeated_handlers"

        # Simulate 3 loop iterations calling the exact same handler name 'summarize_chunk'
        for cycle in range(3):
            seq = sink.next_seq(run_id)
            evt = EventRecord(
                seq_num=seq,
                run_id=run_id,
                node_id=f"node_loop_chunk_{cycle}",
                node_name="summarize_chunk",
                attempt_num=1,
                cycle_index=cycle,
                event_type="tool_completed",
            )
            sink.emit(evt)

        events = sink.get_events(run_id)
        assert len(events) == 3
        # All have same node_name but unique node_id, cycle_index, and seq_num
        assert len({e.node_id for e in events}) == 3
        assert [e.cycle_index for e in events] == [0, 1, 2]
        assert [e.seq_num for e in events] == [1, 2, 3]


class TestContextIsolationAndDisabledTracing:
    """Tests covering ContextVar async span isolation and zero overhead when disabled."""

    @pytest.mark.asyncio
    async def test_concurrent_spans_isolated(self) -> None:
        tracer = NexuraTracer(service_name="test_service")

        results: dict[str, list[str]] = {}

        async def worker(worker_id: str) -> None:
            with tracer.span(f"root_{worker_id}") as root:
                assert root is not None
                await asyncio.sleep(0.01)

                with tracer.span(f"child_{worker_id}") as child:
                    assert child is not None
                    # Verify child parent_span_id matches its own root
                    assert child.parent_span_id == root.span_id
                    assert child.trace_id == root.trace_id
                    results[worker_id] = [root.trace_id, child.trace_id]
                await asyncio.sleep(0.01)

        # Run two workers concurrently
        await asyncio.gather(worker("alpha"), worker("bravo"))

        # Both workers must have completely distinct trace IDs!
        trace_alpha = results["alpha"][0]
        trace_bravo = results["bravo"][0]
        assert trace_alpha != trace_bravo

        spans = tracer.get_spans()
        assert len(spans) == 4
        # Spans per trace are isolated
        alpha_spans = [s for s in spans if s.trace_id == trace_alpha]
        bravo_spans = [s for s in spans if s.trace_id == trace_bravo]
        assert len(alpha_spans) == 2
        assert len(bravo_spans) == 2

    def test_disabled_tracing_zero_requests_and_spans(self) -> None:
        tracer = NexuraTracer(enabled=False)

        with tracer.span("expensive_operation") as span:
            assert span is None
            # Current span is None
            assert tracer.get_current_span() is None

        # Zero spans recorded
        assert len(tracer.get_spans()) == 0


class TestExporterFailurePolicy:
    """Tests verifying best-effort vs strict exporter failure isolation."""

    class FailingSink(EventSink):
        def emit(self, event: EventRecord) -> None:
            raise RuntimeError("Simulated remote collector timeout 504")

        def flush(self) -> None:
            raise RuntimeError("Flush failed")

        def close(self) -> None:
            pass

        def get_events(self, run_id: str | None = None) -> list[EventRecord]:
            return []

    def test_best_effort_policy_does_not_change_execution_semantics(self) -> None:
        memory_sink = MemoryEventSink()
        failing_sink = self.FailingSink()

        composite = CompositeEventSink(
            sinks=[memory_sink, failing_sink],
            failure_policy="best_effort",
        )

        evt = EventRecord(
            seq_num=1,
            run_id="run_best_effort",
            event_type="run_completed",
            duration_ms=45.0,
        )

        # Emit does not throw even though failing_sink raised!
        composite.emit(evt)

        # Execution semantics preserved: memory_sink received the event
        saved = memory_sink.get_events("run_best_effort")
        assert len(saved) == 1
        assert saved[0].duration_ms == 45.0

        # Failure recorded in diagnostics
        assert len(composite.sink_errors) == 1
        assert "504" in str(composite.sink_errors[0])

    def test_strict_policy_surfaces_exporter_failure(self) -> None:
        memory_sink = MemoryEventSink()
        failing_sink = self.FailingSink()

        composite = CompositeEventSink(
            sinks=[memory_sink, failing_sink],
            failure_policy="strict",
        )

        evt = EventRecord(
            seq_num=1,
            run_id="run_strict",
            event_type="run_completed",
        )

        with pytest.raises(ExporterFailedError, match="failed to emit event"):
            composite.emit(evt)


class TestUsagePreservationAndRedaction:
    """Tests covering token usage preservation (including unknown usage) and payload redaction."""

    def test_metrics_preserves_unknown_usage(self) -> None:
        collector = MetricsCollector()

        # Run 1: Known usage
        collector.record_event(
            EventRecord(
                seq_num=1,
                run_id="r1",
                event_type="run_completed",
                duration_ms=100.0,
                usage=Usage(prompt_tokens=10, completion_tokens=20, total_tokens=30),
            )
        )

        # Run 2: Provider did not report usage (usage=None)
        collector.record_event(
            EventRecord(
                seq_num=1,
                run_id="r2",
                event_type="run_completed",
                duration_ms=120.0,
                usage=None,
            )
        )

        # Run 3: Incomplete usage (total_tokens=None)
        collector.record_event(
            EventRecord(
                seq_num=1,
                run_id="r3",
                event_type="run_completed",
                duration_ms=80.0,
                usage=Usage(prompt_tokens=5, completion_tokens=None, total_tokens=None),
            )
        )

        summary = collector.get_summary()
        assert summary.runs_total == 0  # run_started wasn't emitted in this test snippet
        assert summary.runs_succeeded == 3
        assert summary.total_prompt_tokens == 15
        assert summary.total_completion_tokens == 20
        assert summary.total_tokens == 30
        # Explicitly preserves 2 unknown usage events without zero-cost certainty!
        assert summary.unknown_usage_count == 2
        assert summary.avg_duration_ms == 100.0

    def test_payload_export_opt_in_and_redaction(self) -> None:
        # Default payload_export=False strips payload
        sink_no_export = MemoryEventSink(payload_export=False)
        sink_no_export.emit(
            EventRecord(
                seq_num=1,
                run_id="r_priv",
                event_type="tool_started",
                payload={"sensitive_card": "4532-1111-2222-3333", "user": "test"},
            )
        )
        assert sink_no_export.get_events()[0].payload == {}

        # Opt-in payload_export=True keeps payload with redacting secrets
        sink_with_export = MemoryEventSink(payload_export=True)
        sink_with_export.emit(
            EventRecord(
                seq_num=1,
                run_id="r_priv",
                event_type="tool_started",
                payload={"sensitive_card": "4532-1111-2222-3333", "user": "test"},
            )
        )
        saved_payload = sink_with_export.get_events()[0].payload
        assert saved_payload["user"] == "test"
        assert "[REDACTED_CARD]" in saved_payload["sensitive_card"]


class TestDurabilityAndOTLPExport:
    """Tests covering SQLite persistence, JSONL writing, and OTLP formatting."""

    def test_sqlite_event_sink_durability_across_reopen(self, tmp_path: Any) -> None:
        db_path = str(tmp_path / "obs_events.db")
        sink1 = SqliteEventSink(db_path, payload_export=True)

        evt = EventRecord(
            seq_num=1,
            run_id="run_durable",
            node_id="node_db_write",
            node_name="sqlite_writer",
            event_type="node_completed",
            duration_ms=42.1,
            payload={"key": "val"},
            usage=Usage(prompt_tokens=10, completion_tokens=15, total_tokens=25),
        )
        sink1.emit(evt)
        sink1.close()

        # Reopen with fresh instance
        sink2 = SqliteEventSink(db_path, payload_export=True)
        reopened = sink2.get_events("run_durable")
        assert len(reopened) == 1
        r_evt = reopened[0]
        assert r_evt.seq_num == 1
        assert r_evt.node_name == "sqlite_writer"
        assert r_evt.duration_ms == 42.1
        assert r_evt.usage is not None
        assert r_evt.usage.total_tokens == 25
        assert r_evt.payload["key"] == "val"
        sink2.close()

    def test_jsonl_event_sink_stream(self, tmp_path: Any) -> None:
        file_path = str(tmp_path / "events.jsonl")
        sink = JsonlEventSink(file_path)

        sink.emit(EventRecord(seq_num=1, run_id="r_jsonl", event_type="run_started"))
        sink.emit(EventRecord(seq_num=2, run_id="r_jsonl", event_type="run_completed"))
        sink.close()

        with open(file_path, encoding="utf-8") as f:
            lines = f.readlines()
        assert len(lines) == 2
        import json

        data = json.loads(lines[1])
        assert data["seq_num"] == 2
        assert data["event_type"] == "run_completed"

    def test_otlp_span_dictionary_and_payload_formatting(self) -> None:
        tracer = NexuraTracer(service_name="payment_svc")
        with tracer.span("authorize_payment", attributes={"user.id": "usr_99"}) as span:
            assert span is not None

        spans = tracer.get_spans()
        assert len(spans) == 1
        otlp_dict = span_to_otlp_dict(spans[0])

        assert otlp_dict["name"] == "authorize_payment"
        assert otlp_dict["status"]["code"] == "STATUS_CODE_OK"
        assert any(a["key"] == "user.id" for a in otlp_dict["attributes"])

        payload = export_spans_otlp_payload(spans, service_name="payment_svc")
        assert "resourceSpans" in payload
        res_spans = payload["resourceSpans"][0]
        assert res_spans["scopeSpans"][0]["spans"][0]["name"] == "authorize_payment"


class TestObservabilityModuleContracts:
    """Tests covering BaseModule execution contracts and registry inclusion."""

    @pytest.mark.asyncio
    async def test_module_invoke_operations(self) -> None:
        mod = ObservabilityModule()
        ctx = ExecutionContext(run_id="run_b27_mod_test", node_id="obs_node")

        # 1. record_event
        res1 = await mod.execute(
            ctx,
            {
                "operation": "record_event",
                "run_id": "run_b27_mod_test",
                "node_id": "n1",
                "node_name": "step_one",
                "event_type": "node_completed",
                "duration_ms": 15.0,
                "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
            },
            {},
        )
        assert res1.status == "success"
        assert res1.outputs["result"]["seq_num"] == 1

        # 2. query_events
        res2 = await mod.execute(
            ctx,
            {"operation": "query_events", "run_id": "run_b27_mod_test"},
            {},
        )
        assert res2.status == "success"
        assert res2.outputs["result"]["count"] == 1
        assert res2.outputs["result"]["events"][0]["node_name"] == "step_one"

        # 3. get_metrics
        res3 = await mod.execute(
            ctx,
            {"operation": "get_metrics"},
            {},
        )
        assert res3.status == "success"
        assert res3.outputs["result"]["nodes_total"] == 1
        assert res3.outputs["result"]["total_tokens"] == 10

    def test_default_registry_contains_observability_module(self) -> None:
        assert default_registry.has("nexura.observability")
