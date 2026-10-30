# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Small bounded OTLP/HTTP JSON processor. All network I/O runs on a daemon thread."""

from __future__ import annotations

import atexit
import base64
import json
import math
import queue
import threading
import time
from collections.abc import Mapping, Sequence
from typing import Any

import httpx


def _value(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {
            "doubleValue": (
                value
                if math.isfinite(value)
                else ("NaN" if math.isnan(value) else ("Infinity" if value > 0 else "-Infinity"))
            )
        }
    if isinstance(value, bytes):
        return {"bytesValue": base64.b64encode(value).decode()}
    if isinstance(value, Sequence) and not isinstance(value, str):
        return {"arrayValue": {"values": [_value(item) for item in value]}}
    return {"stringValue": str(value)}


def _attrs(values: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    return [{"key": key, "value": _value(value)} for key, value in (values or {}).items()]


def payload(spans: list[Any]) -> dict[str, Any]:
    resources: dict[str, dict[str, Any]] = {}
    for span in spans:
        attrs = _attrs(span.resource.attributes)
        resource_key = json.dumps(attrs, sort_keys=True)
        resource = resources.setdefault(
            resource_key, {"resource": {"attributes": attrs}, "scopeSpans": []}
        )
        context = span.context
        parent = span.parent
        item = {
            "traceId": f"{context.trace_id:032x}",
            "spanId": f"{context.span_id:016x}",
            "parentSpanId": f"{parent.span_id:016x}" if parent else "",
            "name": span.name,
            "kind": span.kind.value + 1,
            "startTimeUnixNano": str(span.start_time),
            "endTimeUnixNano": str(span.end_time),
            "attributes": _attrs(span.attributes),
            "events": [
                {
                    "name": event.name,
                    "timeUnixNano": str(event.timestamp),
                    "attributes": _attrs(event.attributes),
                }
                for event in span.events
            ],
            "status": {
                "code": span.status.status_code.value,
                "message": span.status.description or "",
            },
            "links": [
                {
                    "traceId": f"{link.context.trace_id:032x}",
                    "spanId": f"{link.context.span_id:016x}",
                    "attributes": _attrs(link.attributes),
                }
                for link in span.links
            ],
        }
        scope = span.instrumentation_scope
        resource["scopeSpans"].append(
            {
                "scope": {
                    "name": scope.name if scope else "nexura-agents",
                    "version": (scope.version or "") if scope else "",
                },
                "spans": [item],
            }
        )
    return {"resourceSpans": list(resources.values())}


class HostedTraceProcessor:
    """Duck-typed OTel SpanProcessor, requiring no exporter dependency.

    ``on_end`` only enqueues. Export failures and queue overflow increment counters and never
    reach the user's run. ``force_flush``/``shutdown`` are explicitly bounded waiting APIs.
    """

    def __init__(
        self,
        url: str,
        api_key: str,
        *,
        queue_size: int = 2048,
        batch_size: int = 128,
        interval: float = 1.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if queue_size < 1 or batch_size < 1 or interval <= 0:
            raise ValueError("Queue size, batch size and flush interval must be positive.")
        self.url = url.rstrip("/") + "/v1/traces"
        self._client = httpx.Client(
            transport=transport, timeout=3.0, follow_redirects=False, headers={"X-API-Key": api_key}
        )
        self._queue: queue.Queue[Any] = queue.Queue(queue_size)
        self._batch_size = min(batch_size, 5000)
        self._interval = interval
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._condition = threading.Condition()
        self._pending = 0
        self._closed = False
        self.dropped_spans = 0
        self.exported_spans = 0
        self.rejected_spans = 0
        self.retries = 0
        self._thread = threading.Thread(target=self._work, name="nexura-otlp", daemon=True)
        self._thread.start()
        atexit.register(self.shutdown)

    def on_start(self, span: Any, parent_context: Any = None) -> None:
        pass

    def _on_ending(self, span: Any) -> None:
        # Added by recent OpenTelemetry SDKs; no work until the immutable on_end snapshot.
        pass

    def on_end(self, span: Any) -> None:
        try:
            with self._condition:
                if self._closed:
                    self.dropped_spans += 1
                    return
                try:
                    self._queue.put_nowait(span)
                except queue.Full:
                    self.dropped_spans += 1
                    return
                self._pending += 1
            if self._queue.qsize() >= self._batch_size:
                self._wake.set()
        except Exception:
            # Instrumentation must not fail a user's operation, even during interpreter exit.
            pass

    def _send(self, spans: list[Any]) -> None:
        try:
            body = payload(spans)
            for attempt in range(4):
                try:
                    response = self._client.post(self.url, json=body)
                    if response.status_code == 200:
                        rejected = int(
                            response.json().get("partialSuccess", {}).get("rejectedSpans", 0)
                        )
                        self.rejected_spans += max(0, min(rejected, len(spans)))
                        self.exported_spans += len(spans) - max(0, min(rejected, len(spans)))
                        return
                    retry = response.status_code == 429 or response.status_code >= 500
                    retry_after = response.headers.get("retry-after", "0")
                except httpx.TransportError:
                    retry, retry_after = True, "0"
                if not retry or attempt == 3 or self._stop.is_set():
                    break
                try:
                    delay = min(max(float(retry_after), 0.2 * 2**attempt), 2.0)
                except ValueError:
                    delay = 0.2 * 2**attempt
                self.retries += 1
                if self._stop.wait(delay):
                    break
        except Exception:
            pass
        self.dropped_spans += len(spans)

    def _work(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self._interval)
            self._wake.clear()
            while not self._stop.is_set():
                batch = []
                for _ in range(self._batch_size):
                    try:
                        batch.append(self._queue.get_nowait())
                    except queue.Empty:
                        break
                if not batch:
                    break
                try:
                    self._send(batch)
                finally:
                    with self._condition:
                        self._pending -= len(batch)
                        self._condition.notify_all()
        self._client.close()

    def force_flush(self, timeout_millis: int = 5000) -> bool:
        deadline = time.monotonic() + max(0, timeout_millis) / 1000
        self._wake.set()
        with self._condition:
            while self._pending:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._condition.wait(left)
        return True

    def shutdown(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
        self.force_flush()
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=0.1)
        atexit.unregister(self.shutdown)
