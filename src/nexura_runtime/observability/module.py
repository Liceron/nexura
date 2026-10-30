# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""B00/B01 module contract adapter for observability, tracing, and metrics."""

from __future__ import annotations

import time
from typing import Any

from nexura_agents.contracts.base import BaseModule
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.manifest import EffectPolicy, ModuleManifest
from nexura_agents.contracts.result import ModuleResult
from nexura_agents.types import Usage

from nexura_runtime.observability.metrics import MetricsCollector
from nexura_runtime.observability.models import EventRecord
from nexura_runtime.observability.otlp import export_spans_otlp_payload
from nexura_runtime.observability.sinks import MemoryEventSink
from nexura_runtime.observability.tracer import NexuraTracer


class ObservabilityModule(BaseModule):
    """B00/B01 module for querying events, exporting traces, and aggregating metrics."""

    def __init__(
        self,
        sink: MemoryEventSink | None = None,
        tracer: NexuraTracer | None = None,
        metrics: MetricsCollector | None = None,
        *,
        module_id: str = "nexura.observability",
        version: str = "1.0.0",
        effect_policy: EffectPolicy = "read",
    ) -> None:
        self.sink = sink or MemoryEventSink(payload_export=True)
        self.tracer = tracer or NexuraTracer()
        self.metrics = metrics or MetricsCollector()

        self._manifest = ModuleManifest(
            schema_version=1,
            module_id=module_id,
            version=version,
            kind="service",
            internal=True,
            title="Observability and Tracing Engine",
            description=(
                "Emits and queries ordered canonical execution events, "
                "traces nested workflows, and aggregates operational metrics."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": [
                            "record_event",
                            "query_events",
                            "get_metrics",
                            "export_traces",
                        ],
                    },
                    "run_id": {"type": "string"},
                    "parent_run_id": {"type": "string"},
                    "node_id": {"type": "string"},
                    "node_name": {"type": "string"},
                    "attempt_num": {"type": "integer"},
                    "cycle_index": {"type": "integer"},
                    "event_type": {"type": "string"},
                    "duration_ms": {"type": "number"},
                    "payload": {"type": "object"},
                    "usage": {"type": "object"},
                    "error_code": {"type": "string"},
                    "error_message": {"type": "string"},
                },
                "required": ["operation"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                    "result": {"type": "object"},
                },
                "required": ["status"],
            },
            config_schema={"type": "object", "properties": {}, "additionalProperties": True},
            capabilities={"network": False, "streaming": False},
            effect_policy=effect_policy,
        )

    @property
    def manifest(self) -> ModuleManifest:
        return self._manifest

    async def invoke(
        self,
        context: ExecutionContext,
        inputs: dict[str, Any],
        config: dict[str, Any],
    ) -> ModuleResult:
        operation = inputs.get("operation")
        try:
            if operation == "record_event":
                run_id = inputs.get("run_id") or context.run_id
                seq = self.sink.next_seq(run_id)

                usage_data = inputs.get("usage")
                usage = Usage(**usage_data) if usage_data else None

                event = EventRecord(
                    seq_num=seq,
                    run_id=run_id,
                    parent_run_id=inputs.get("parent_run_id"),
                    node_id=inputs.get("node_id"),
                    node_name=inputs.get("node_name"),
                    attempt_num=inputs.get("attempt_num", 1),
                    cycle_index=inputs.get("cycle_index", 0),
                    event_type=inputs.get("event_type", "custom"),
                    timestamp=time.time(),
                    duration_ms=inputs.get("duration_ms"),
                    payload=inputs.get("payload", {}),
                    usage=usage,
                    error_code=inputs.get("error_code"),
                    error_message=inputs.get("error_message"),
                )
                self.sink.emit(event)
                self.metrics.record_event(event)

                return ModuleResult.success(
                    {
                        "status": "success",
                        "result": {
                            "event_id": event.event_id,
                            "seq_num": event.seq_num,
                        },
                    }
                )

            if operation == "query_events":
                run_id = inputs.get("run_id")
                events = self.sink.get_events(run_id=run_id)
                filter_type = inputs.get("event_type")
                if filter_type:
                    events = [e for e in events if e.event_type == filter_type]
                return ModuleResult.success(
                    {
                        "status": "success",
                        "result": {
                            "count": len(events),
                            "events": [e.model_dump(mode="json") for e in events],
                        },
                    }
                )

            if operation == "get_metrics":
                summary = self.metrics.get_summary()
                return ModuleResult.success(
                    {
                        "status": "success",
                        "result": summary.model_dump(),
                    }
                )

            if operation == "export_traces":
                spans = self.tracer.get_spans()
                otlp_payload = export_spans_otlp_payload(spans)
                return ModuleResult.success(
                    {
                        "status": "success",
                        "result": otlp_payload,
                    }
                )

            return ModuleResult.failure(
                error_code="INVALID_OPERATION",
                error_message=f"Unsupported operation '{operation}'.",
            )
        except Exception as err:
            return ModuleResult.failure(
                error_code="OBSERVABILITY_ERROR",
                error_message=str(err),
            )
