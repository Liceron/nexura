# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Observability, tracing, canonical events, and operational metrics."""

from __future__ import annotations

from nexura_runtime.modules.registry import default_registry
from nexura_runtime.observability.errors import (
    ExporterFailedError,
    InvalidSpanError,
    ObservabilityError,
    SinkClosedError,
)
from nexura_runtime.observability.metrics import MetricsCollector
from nexura_runtime.observability.models import (
    EventRecord,
    ExporterFailurePolicy,
    MetricsSummary,
    SpanRecord,
    SpanStatus,
)
from nexura_runtime.observability.module import ObservabilityModule
from nexura_runtime.observability.otlp import (
    export_spans_otlp_payload,
    span_to_otlp_dict,
)
from nexura_runtime.observability.sinks import (
    CompositeEventSink,
    EventSink,
    JsonlEventSink,
    MemoryEventSink,
    SqliteEventSink,
)
from nexura_runtime.observability.tracer import NexuraTracer, SpanContext

__all__ = [
    "ObservabilityError",
    "ExporterFailedError",
    "InvalidSpanError",
    "SinkClosedError",
    "EventRecord",
    "SpanRecord",
    "MetricsSummary",
    "SpanStatus",
    "ExporterFailurePolicy",
    "NexuraTracer",
    "SpanContext",
    "EventSink",
    "MemoryEventSink",
    "JsonlEventSink",
    "SqliteEventSink",
    "CompositeEventSink",
    "MetricsCollector",
    "span_to_otlp_dict",
    "export_spans_otlp_payload",
    "ObservabilityModule",
]

_default_obs_module = ObservabilityModule()
if not default_registry.has(_default_obs_module.manifest.module_id):
    default_registry.register(_default_obs_module)
