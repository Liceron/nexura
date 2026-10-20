# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Data models for events, spans, metrics, and audit records."""

from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from nexura_agents.types import Usage
from pydantic import BaseModel, Field

SpanStatus = Literal["unset", "ok", "error"]
ExporterFailurePolicy = Literal["best_effort", "strict"]


class EventRecord(BaseModel):
    """Canonical ordered event emitted across SDK and service executions."""

    seq_num: int
    event_id: str = Field(default_factory=lambda: f"evt_{uuid.uuid4().hex[:12]}")
    run_id: str
    parent_run_id: str | None = None
    node_id: str | None = None
    node_name: str | None = None
    attempt_num: int = 1
    cycle_index: int = 0
    event_type: str
    timestamp: float = Field(default_factory=time.time)
    duration_ms: float | None = None
    trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    usage: Usage | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None


class SpanRecord(BaseModel):
    """Execution span model compatible with OpenTelemetry trace definitions."""

    trace_id: str
    span_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:16])
    parent_span_id: str | None = None
    name: str
    start_time: float = Field(default_factory=time.time)
    end_time: float | None = None
    duration_ms: float | None = None
    status: SpanStatus = "unset"
    attributes: dict[str, Any] = Field(default_factory=dict)
    events: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None


class MetricsSummary(BaseModel):
    """Aggregated operational metrics for workflow runs, nodes, and token consumption."""

    runs_total: int = 0
    runs_succeeded: int = 0
    runs_failed: int = 0
    nodes_total: int = 0
    tools_total: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_tokens: int = 0
    unknown_usage_count: int = 0
    avg_duration_ms: float = 0.0
