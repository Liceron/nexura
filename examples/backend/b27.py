"""Module B27 Standalone Keyless Demonstration.

Demonstrates:
1. Tracing a nested multi-step workflow locally without external services.
2. Emitting ordered canonical events with parent/child run and span IDs.
3. Strict monotonic sequence numbers and repeated handler distinction.
4. Payload redaction and privacy preservation in event records.
5. Exporter failure isolation under best-effort policy.
6. Operational metrics aggregation and token usage preservation.
7. OpenTelemetry standard OTLP trace payload formatting.
8. B00/B01 module contract and CLI subprocess invocation.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from nexura_runtime.contracts.context import ExecutionContext
from nexura_runtime.types import Usage

from nexura_runtime.observability import (
    CompositeEventSink,
    EventRecord,
    EventSink,
    MetricsCollector,
    NexuraTracer,
    ObservabilityModule,
    SqliteEventSink,
    export_spans_otlp_payload,
)


class MockFailingRemoteSink(EventSink):
    """Mock sink simulating a transient network outage or remote collector failure."""

    def emit(self, event: EventRecord) -> None:
        raise ConnectionResetError("Remote OTLP collector endpoint unreachable (simulated).")

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass

    def get_events(self, run_id: str | None = None) -> list[EventRecord]:
        return []


def run_demonstration() -> None:
    print("=== Module B27: Tracing, Events, Metrics and Audit ===")

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = str(Path(tmp_dir) / "audit_events.db")
        sqlite_sink = SqliteEventSink(db_path, payload_export=True)
        tracer = NexuraTracer(service_name="payment_orchestrator")
        metrics = MetricsCollector()

        # 1. Tracing a Nested Workflow Locally
        print("\n1. Tracing nested customer refund workflow locally...")
        run_id = "run_refund_9981"

        # Root Workflow Span
        with tracer.span("workflow_customer_refund", attributes={"run_id": run_id}) as root_span:
            assert root_span is not None

            # Emit run_started event
            evt_start = EventRecord(
                seq_num=1,
                run_id=run_id,
                event_type="run_started",
                trace_id=root_span.trace_id,
                span_id=root_span.span_id,
                timestamp=time.time(),
            )
            sqlite_sink.emit(evt_start)
            metrics.record_event(evt_start)

            # Child Step 1: Account Lookup
            print("  Executing child step: lookup_account...")
            with tracer.span(
                "lookup_account", attributes={"customer.id": "cust_402"}
            ) as step1_span:
                assert step1_span is not None
                time.sleep(0.01)
                evt_step1 = EventRecord(
                    seq_num=2,
                    run_id=run_id,
                    node_id="node_lookup",
                    node_name="lookup_account",
                    event_type="node_completed",
                    trace_id=root_span.trace_id,
                    span_id=step1_span.span_id,
                    parent_span_id=root_span.span_id,
                    duration_ms=12.4,
                    payload={"account_tier": "premium"},
                )
                sqlite_sink.emit(evt_step1)
                metrics.record_event(evt_step1)

            # Child Step 2: Fraud Evaluation (Repeated step in loop simulation)
            print("  Executing child step: fraud_risk_evaluation (cycle 0)...")
            with tracer.span("fraud_risk_evaluation") as step2_span:
                assert step2_span is not None
                time.sleep(0.01)
                evt_step2 = EventRecord(
                    seq_num=3,
                    run_id=run_id,
                    node_id="node_fraud_eval_0",
                    node_name="fraud_risk_evaluation",
                    cycle_index=0,
                    event_type="node_completed",
                    trace_id=root_span.trace_id,
                    span_id=step2_span.span_id,
                    parent_span_id=root_span.span_id,
                    duration_ms=18.6,
                    usage=Usage(prompt_tokens=42, completion_tokens=18, total_tokens=60),
                )
                sqlite_sink.emit(evt_step2)
                metrics.record_event(evt_step2)

            # Child Step 3: Gateway Refund Execution with sensitive credit card
            print("  Executing child step: execute_refund (with sensitive PII payload)...")
            with tracer.span("execute_refund") as step3_span:
                assert step3_span is not None
                time.sleep(0.01)
                evt_step3 = EventRecord(
                    seq_num=4,
                    run_id=run_id,
                    node_id="node_gateway_refund",
                    node_name="execute_refund",
                    event_type="tool_completed",
                    trace_id=root_span.trace_id,
                    span_id=step3_span.span_id,
                    parent_span_id=root_span.span_id,
                    duration_ms=35.2,
                    payload={
                        "card_number": "4111-2222-3333-4444",
                        "auth_token": "Bearer nxk_secret_refund_auth_token_99",
                        "amount_usd": 150.00,
                    },
                )
                sqlite_sink.emit(evt_step3)
                metrics.record_event(evt_step3)

            # Emit run_completed event
            evt_end = EventRecord(
                seq_num=5,
                run_id=run_id,
                event_type="run_completed",
                trace_id=root_span.trace_id,
                span_id=root_span.span_id,
                duration_ms=75.5,
            )
            sqlite_sink.emit(evt_end)
            metrics.record_event(evt_end)

        # 2. Inspect Ordered Canonical Events
        print("\n2. Inspecting ordered canonical events from persistent SQLite store...")
        stored_events = sqlite_sink.get_events(run_id)
        print(f"  Total events recorded: {len(stored_events)}")
        for evt in stored_events:
            print(
                f"    [seq={evt.seq_num}] type={evt.event_type:<14} "
                f"node={evt.node_name or 'none':<22} duration={evt.duration_ms or 0:.1f}ms"
            )

        # 3. Verify Monotonicity and Redaction
        print("\n3. Verifying payload redaction and sequence order...")
        assert [e.seq_num for e in stored_events] == [1, 2, 3, 4, 5]
        refund_event = stored_events[3]
        print("  Raw emitted card: 4111-2222-3333-4444")
        print(f"  Stored card: {refund_event.payload.get('card_number')}")
        print(f"  Stored token: {refund_event.payload.get('auth_token')}")
        assert "[REDACTED_CARD]" in refund_event.payload["card_number"]
        assert "Bearer [REDACTED]" in refund_event.payload["auth_token"]
        print("  [SUCCESS] Monotonic ordering verified and sensitive secrets sanitized.")

        # 4. Exporter Failure Resilience (Best-Effort Policy)
        print("\n4. Demonstrating best-effort exporter failure isolation...")
        failing_remote_sink = MockFailingRemoteSink()
        composite = CompositeEventSink(
            sinks=[sqlite_sink, failing_remote_sink],
            failure_policy="best_effort",
        )
        test_evt = EventRecord(
            seq_num=6,
            run_id=run_id,
            event_type="custom",
            node_name="health_ping",
        )
        composite.emit(test_evt)
        print(f"  Captured exporter failure: {composite.sink_errors[0]}")
        print("  [SUCCESS] Remote exporter failure did not disrupt primary pipeline execution.")

        # 5. Operational Metrics Snapshot
        print("\n5. Inspecting aggregated operational metrics...")
        summary = metrics.get_summary()
        print(f"  Runs Succeeded: {summary.runs_succeeded}")
        print(f"  Nodes Executed: {summary.nodes_total}")
        print(f"  Tools Executed: {summary.tools_total}")
        print(f"  Total Tokens:   {summary.total_tokens}")
        print(f"  Avg Duration:   {summary.avg_duration_ms} ms")
        assert summary.runs_succeeded == 1
        assert summary.tools_total == 1
        assert summary.total_tokens == 60

        # 6. OpenTelemetry Standard OTLP Trace Export
        print("\n6. Exporting recorded spans to standard OpenTelemetry OTLP format...")
        spans = tracer.get_spans()
        print(f"  Recorded Spans count: {len(spans)}")
        otlp_export = export_spans_otlp_payload(spans, service_name="payment_orchestrator")
        resource_spans = otlp_export["resourceSpans"][0]
        scope_spans = resource_spans["scopeSpans"][0]["spans"]
        svc_val = resource_spans["resource"]["attributes"][0]["value"]["stringValue"]
        print(f"  OTLP Resource Service: {svc_val}")
        for s in scope_spans:
            print(f"    - Span: {s['name']:<26} Status: {s['status']['code']}")
        assert len(scope_spans) == 4

        # 7. B00/B01 Module Contract Invocation
        print("\n7. Executing ObservabilityModule via Python contract...")
        obs_module = ObservabilityModule(tracer=tracer, metrics=metrics)
        ctx = ExecutionContext(run_id="run_b27_demo", node_id="obs_node")
        mod_res = asyncio.run(
            obs_module.invoke(
                ctx,
                inputs={"operation": "get_metrics"},
                config={},
            )
        )
        print(f"  Module Status: {mod_res.status}")
        print(f"  Metrics Nodes Total: {mod_res.outputs['result']['nodes_total']}")
        assert mod_res.status == "success"

        # 8. B01 CLI Subprocess Invocation
        print("\n8. Invoking ObservabilityModule via B01 CLI subprocess...")
        cmd = [
            sys.executable,
            "-m",
            "nexura_runtime.modules",
            "run",
            "nexura.observability",
            "--input",
            json.dumps({"operation": "get_metrics"}),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        print(f"  CLI Exit Code: {proc.returncode}")
        print(f"  CLI Output: {proc.stdout.strip()}")
        assert proc.returncode == 0
        assert "nodes_total" in proc.stdout

    print("\n=== All B27 Demonstration Steps Completed Successfully ===")


if __name__ == "__main__":
    run_demonstration()
