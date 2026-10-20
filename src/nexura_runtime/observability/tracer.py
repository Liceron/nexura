# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""ContextVar-isolated execution tracer with OpenTelemetry-compatible span models."""

from __future__ import annotations

import contextvars
import threading
import time
import uuid
from types import TracebackType
from typing import Any

from nexura_runtime.observability.models import SpanRecord

_current_span: contextvars.ContextVar[SpanRecord | None] = contextvars.ContextVar(
    "nexura_current_span", default=None
)


class SpanContext:
    """Context manager controlling the lifecycle of a single execution span."""

    def __init__(
        self,
        tracer: NexuraTracer,
        span: SpanRecord | None,
        token: contextvars.Token[SpanRecord | None] | None,
    ) -> None:
        self.tracer = tracer
        self.span = span
        self.token = token

    def __enter__(self) -> SpanRecord | None:
        return self.span

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if not self.span or not self.tracer.enabled:
            return

        end_time = time.time()
        self.span.end_time = end_time
        self.span.duration_ms = (end_time - self.span.start_time) * 1000.0

        if exc_val is not None:
            self.span.status = "error"
            self.span.error = str(exc_val)
        elif self.span.status == "unset":
            self.span.status = "ok"

        # Reset active ContextVar to parent
        if self.token is not None:
            _current_span.reset(self.token)

        self.tracer._record_span(self.span)


class NexuraTracer:
    """Local, lightweight tracer supporting span hierarchies, contextvars, and isolation."""

    def __init__(
        self,
        service_name: str = "nexura",
        enabled: bool = True,
    ) -> None:
        self.service_name = service_name
        self.enabled = enabled
        self._lock = threading.Lock()
        self._spans: list[SpanRecord] = []

    def span(
        self,
        name: str,
        attributes: dict[str, Any] | None = None,
        parent_span: SpanRecord | None = None,
    ) -> SpanContext:
        """Create a managed span context for synchronous or asynchronous tracing."""
        if not self.enabled:
            return SpanContext(self, None, None)

        # Determine parent span
        active_parent = parent_span or _current_span.get()

        if active_parent:
            trace_id = active_parent.trace_id
            parent_span_id = active_parent.span_id
        else:
            trace_id = uuid.uuid4().hex
            parent_span_id = None

        span_id = uuid.uuid4().hex[:16]
        attrs = dict(attributes or {})
        attrs.setdefault("service.name", self.service_name)

        record = SpanRecord(
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=parent_span_id,
            name=name,
            start_time=time.time(),
            attributes=attrs,
        )

        token = _current_span.set(record)
        return SpanContext(self, record, token)

    def _record_span(self, span: SpanRecord) -> None:
        with self._lock:
            self._spans.append(span)

    def get_current_span(self) -> SpanRecord | None:
        """Return the active span in the current async/thread execution context."""
        return _current_span.get()

    def get_spans(self) -> list[SpanRecord]:
        """Return a copy of all completed spans recorded by this tracer."""
        with self._lock:
            return list(self._spans)

    def clear(self) -> None:
        """Clear all recorded spans."""
        with self._lock:
            self._spans.clear()
