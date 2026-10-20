# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Operational metrics collection and usage accounting."""

from __future__ import annotations

import threading

from nexura_runtime.observability.models import EventRecord, MetricsSummary, SpanRecord


class MetricsCollector:
    """Collector aggregating run counts, tool invocations, durations, and token usage."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs_total = 0
        self._runs_succeeded = 0
        self._runs_failed = 0
        self._nodes_total = 0
        self._tools_total = 0
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._total_tokens = 0
        self._unknown_usage_count = 0
        self._durations: list[float] = []

    def record_event(self, event: EventRecord) -> None:
        """Process an event record to update counters, latencies, and usage metrics."""
        with self._lock:
            et = event.event_type
            if et == "run_started":
                self._runs_total += 1
            elif et == "run_completed":
                self._runs_succeeded += 1
                if event.duration_ms is not None:
                    self._durations.append(event.duration_ms)
            elif et == "run_failed":
                self._runs_failed += 1
                if event.duration_ms is not None:
                    self._durations.append(event.duration_ms)
            elif et in ("node_completed", "invoke_completed"):
                self._nodes_total += 1
            elif et == "tool_completed":
                self._tools_total += 1

            # Token usage accounting
            if event.usage is not None:
                if event.usage.prompt_tokens is not None:
                    self._prompt_tokens += event.usage.prompt_tokens
                if event.usage.completion_tokens is not None:
                    self._completion_tokens += event.usage.completion_tokens
                if event.usage.total_tokens is not None:
                    self._total_tokens += event.usage.total_tokens
                else:
                    self._unknown_usage_count += 1
            elif et in ("model_completed", "run_completed"):
                self._unknown_usage_count += 1

    def record_span(self, span: SpanRecord) -> None:
        """Record completed span duration."""
        with self._lock:
            if span.duration_ms is not None:
                self._durations.append(span.duration_ms)

    def get_summary(self) -> MetricsSummary:
        """Return snapshot summary of aggregated metrics."""
        with self._lock:
            avg_dur = sum(self._durations) / len(self._durations) if self._durations else 0.0
            return MetricsSummary(
                runs_total=self._runs_total,
                runs_succeeded=self._runs_succeeded,
                runs_failed=self._runs_failed,
                nodes_total=self._nodes_total,
                tools_total=self._tools_total,
                total_prompt_tokens=self._prompt_tokens,
                total_completion_tokens=self._completion_tokens,
                total_tokens=self._total_tokens,
                unknown_usage_count=self._unknown_usage_count,
                avg_duration_ms=round(avg_dur, 2),
            )

    def clear(self) -> None:
        """Reset all metrics counters."""
        with self._lock:
            self._runs_total = 0
            self._runs_succeeded = 0
            self._runs_failed = 0
            self._nodes_total = 0
            self._tools_total = 0
            self._prompt_tokens = 0
            self._completion_tokens = 0
            self._total_tokens = 0
            self._unknown_usage_count = 0
            self._durations.clear()
