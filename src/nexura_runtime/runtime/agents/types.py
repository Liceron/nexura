# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Data structures and types for agent definitions, handoffs, and execution traces."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from nexura_agents.types import Message, Usage


@dataclass(frozen=True)
class Handoff:
    """Represents a handoff invocation request from one agent to another."""

    target_agent: str
    reason: str
    context: dict[str, Any] = field(default_factory=dict)
    transfer_history: bool = False


@dataclass
class AgentRunSummary:
    """Tracing record for an individual agent execution within a multi-agent run."""

    agent_name: str
    run_id: str
    parent_run_id: str | None
    turns: int
    tool_calls_count: int
    handoff_to: str | None = None
    handoff_reason: str | None = None
    usage: Usage = field(default_factory=Usage)


@dataclass
class CoordinatorRunResult:
    """Comprehensive result of a coordinated multi-agent execution."""

    root_run_id: str
    final_agent_name: str
    final_output: str
    structured_output: Any | None = None
    messages: list[Message] = field(default_factory=list)
    traces: list[AgentRunSummary] = field(default_factory=list)
    total_fuel_consumed: int = 0
    handoffs_count: int = 0
    total_usage: Usage = field(default_factory=Usage)
    status: Literal["completed", "failed", "cancelled"] = "completed"
