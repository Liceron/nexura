# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""EventSink protocol and concrete sinks: Memory, JSONL, SQLite, and Composite."""

from __future__ import annotations

import dataclasses
import io
import json
import sqlite3
import threading
from typing import Any, Protocol

from nexura_agents.types import Usage

from nexura_runtime.integrations.channels.redaction import redact_text
from nexura_runtime.observability.errors import ExporterFailedError, SinkClosedError
from nexura_runtime.observability.models import (
    EventRecord,
    ExporterFailurePolicy,
)


class EventSink(Protocol):
    """Storage contract for consuming ordered canonical execution events."""

    def emit(self, event: EventRecord) -> None: ...

    def flush(self) -> None: ...

    def close(self) -> None: ...

    def get_events(self, run_id: str | None = None) -> list[EventRecord]: ...


def _sanitize_payload(payload: dict[str, Any], payload_export: bool) -> dict[str, Any]:
    """Sanitize event payload according to payload export policy and secret redaction."""
    if not payload_export:
        return {}

    sanitized: dict[str, Any] = {}
    for k, v in payload.items():
        if isinstance(v, str):
            sanitized[k] = redact_text(v)
        elif isinstance(v, dict):
            sanitized[k] = _sanitize_payload(v, payload_export=True)
        else:
            sanitized[k] = v
    return sanitized


class MemoryEventSink:
    """Thread-safe in-memory sink storing ordered execution events."""

    def __init__(self, payload_export: bool = False) -> None:
        self.payload_export = payload_export
        self._lock = threading.Lock()
        self._events: list[EventRecord] = []
        self._closed = False
        self._seq_by_run: dict[str, int] = {}

    def next_seq(self, run_id: str) -> int:
        """Generate monotonic sequence number for a given run ID."""
        with self._lock:
            current = self._seq_by_run.get(run_id, 0) + 1
            self._seq_by_run[run_id] = current
            return current

    def emit(self, event: EventRecord) -> None:
        with self._lock:
            if self._closed:
                raise SinkClosedError("Cannot emit event to a closed MemoryEventSink.")

            # Sanitize payload according to export policy
            prepared_event = event.model_copy()
            prepared_event.payload = _sanitize_payload(prepared_event.payload, self.payload_export)
            self._events.append(prepared_event)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        with self._lock:
            self._closed = True

    def get_events(self, run_id: str | None = None) -> list[EventRecord]:
        with self._lock:
            if run_id is None:
                return [e.model_copy() for e in self._events]
            return [e.model_copy() for e in self._events if e.run_id == run_id]

    def clear(self) -> None:
        with self._lock:
            self._events.clear()
            self._seq_by_run.clear()


class JsonlEventSink:
    """Sink streaming events as JSON Lines to an underlying file or IO stream."""

    def __init__(
        self,
        stream_or_path: io.TextIOBase | str,
        payload_export: bool = False,
    ) -> None:
        self.payload_export = payload_export
        self._lock = threading.Lock()
        self._closed = False
        self._stream: io.TextIOBase

        if isinstance(stream_or_path, str):
            self._stream = open(stream_or_path, "a", encoding="utf-8")  # noqa: SIM115
            self._owns_stream = True
        else:
            self._stream = stream_or_path
            self._owns_stream = False

    def emit(self, event: EventRecord) -> None:
        with self._lock:
            if self._closed:
                raise SinkClosedError("Cannot emit event to a closed JsonlEventSink.")

            prepared_event = event.model_copy()
            prepared_event.payload = _sanitize_payload(prepared_event.payload, self.payload_export)
            line = json.dumps(prepared_event.model_dump(mode="json"), ensure_ascii=False) + "\n"
            self._stream.write(line)
            self._stream.flush()

    def flush(self) -> None:
        with self._lock:
            if not self._closed and self._stream:
                self._stream.flush()

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                self._stream.flush()
                if self._owns_stream:
                    self._stream.close()

    def get_events(self, run_id: str | None = None) -> list[EventRecord]:
        # JSONL stream is write-optimized; reading is handled via file or stream inspection
        return []


class SqliteEventSink:
    """Transactional SQLite sink persisting audit and execution events."""

    def __init__(self, db_path: str = ":memory:", payload_export: bool = False) -> None:
        self.db_path = db_path
        self.payload_export = payload_export
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._closed = False
        self._init_db()

    def _init_db(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode = WAL;")
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS observability_events (
                    seq_num INTEGER NOT NULL,
                    event_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    parent_run_id TEXT,
                    node_id TEXT,
                    node_name TEXT,
                    attempt_num INTEGER NOT NULL,
                    cycle_index INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    duration_ms REAL,
                    trace_id TEXT,
                    span_id TEXT,
                    parent_span_id TEXT,
                    payload_json TEXT NOT NULL,
                    usage_json TEXT,
                    metadata_json TEXT NOT NULL,
                    error_code TEXT,
                    error_message TEXT
                );
            """)
            self._conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_obs_run_seq 
                ON observability_events(run_id, seq_num);
            """)
            self._conn.commit()

    def emit(self, event: EventRecord) -> None:
        with self._lock:
            if self._closed:
                raise SinkClosedError("Cannot emit event to a closed SqliteEventSink.")

            prepared_event = event.model_copy()
            prepared_event.payload = _sanitize_payload(prepared_event.payload, self.payload_export)

            usage_json = (
                json.dumps(dataclasses.asdict(prepared_event.usage))
                if prepared_event.usage
                else None
            )

            self._conn.execute(
                """
                INSERT OR REPLACE INTO observability_events (
                    seq_num, event_id, run_id, parent_run_id, node_id, node_name,
                    attempt_num, cycle_index, event_type, timestamp, duration_ms,
                    trace_id, span_id, parent_span_id, payload_json, usage_json,
                    metadata_json, error_code, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    prepared_event.seq_num,
                    prepared_event.event_id,
                    prepared_event.run_id,
                    prepared_event.parent_run_id,
                    prepared_event.node_id,
                    prepared_event.node_name,
                    prepared_event.attempt_num,
                    prepared_event.cycle_index,
                    prepared_event.event_type,
                    prepared_event.timestamp,
                    prepared_event.duration_ms,
                    prepared_event.trace_id,
                    prepared_event.span_id,
                    prepared_event.parent_span_id,
                    json.dumps(prepared_event.payload),
                    usage_json,
                    json.dumps(prepared_event.metadata),
                    prepared_event.error_code,
                    prepared_event.error_message,
                ),
            )
            self._conn.commit()

    def flush(self) -> None:
        with self._lock:
            if not self._closed:
                self._conn.commit()

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                self._conn.close()

    def get_events(self, run_id: str | None = None) -> list[EventRecord]:
        with self._lock:
            if self._closed:
                return []
            if run_id is None:
                cur = self._conn.execute(
                    "SELECT * FROM observability_events ORDER BY run_id, seq_num ASC"
                )
            else:
                cur = self._conn.execute(
                    "SELECT * FROM observability_events WHERE run_id = ? ORDER BY seq_num ASC",
                    (run_id,),
                )
            rows = cur.fetchall()
            events: list[EventRecord] = []
            for r in rows:
                usage_val = Usage(**json.loads(r["usage_json"])) if r["usage_json"] else None
                events.append(
                    EventRecord(
                        seq_num=r["seq_num"],
                        event_id=r["event_id"],
                        run_id=r["run_id"],
                        parent_run_id=r["parent_run_id"],
                        node_id=r["node_id"],
                        node_name=r["node_name"],
                        attempt_num=r["attempt_num"],
                        cycle_index=r["cycle_index"],
                        event_type=r["event_type"],
                        timestamp=r["timestamp"],
                        duration_ms=r["duration_ms"],
                        trace_id=r["trace_id"],
                        span_id=r["span_id"],
                        parent_span_id=r["parent_span_id"],
                        payload=json.loads(r["payload_json"]),
                        usage=usage_val,
                        metadata=json.loads(r["metadata_json"]),
                        error_code=r["error_code"],
                        error_message=r["error_message"],
                    )
                )
            return events


class CompositeEventSink:
    """Fanout sink dispatching events across multiple sinks with configured failure policy."""

    def __init__(
        self,
        sinks: list[EventSink] | None = None,
        failure_policy: ExporterFailurePolicy = "best_effort",
    ) -> None:
        self.sinks: list[EventSink] = list(sinks or [])
        self.failure_policy: ExporterFailurePolicy = failure_policy
        self.sink_errors: list[Exception] = []

    def add_sink(self, sink: EventSink) -> None:
        self.sinks.append(sink)

    def emit(self, event: EventRecord) -> None:
        for sink in self.sinks:
            try:
                sink.emit(event)
            except Exception as exc:
                self.sink_errors.append(exc)
                if self.failure_policy == "strict":
                    raise ExporterFailedError(
                        f"Sink {sink.__class__.__name__} failed to emit event '{event.event_id}'."
                    ) from exc

    def flush(self) -> None:
        for sink in self.sinks:
            try:
                sink.flush()
            except Exception as exc:
                self.sink_errors.append(exc)
                if self.failure_policy == "strict":
                    raise ExporterFailedError(
                        f"Sink {sink.__class__.__name__} failed to flush."
                    ) from exc

    def close(self) -> None:
        for sink in self.sinks:
            try:
                sink.close()
            except Exception as exc:
                self.sink_errors.append(exc)
                if self.failure_policy == "strict":
                    raise ExporterFailedError(
                        f"Sink {sink.__class__.__name__} failed to close."
                    ) from exc

    def get_events(self, run_id: str | None = None) -> list[EventRecord]:
        for sink in self.sinks:
            res = sink.get_events(run_id)
            if res:
                return res
        return []
