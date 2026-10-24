# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Lifecycle events emitted during agent execution."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class RunEventType(StrEnum):
    """Types of execution lifecycle events."""

    RUN_STARTED = "run_started"
    MODEL_STARTED = "model_started"
    MODEL_COMPLETED = "model_completed"
    TOOL_STARTED = "tool_started"
    # data "tool_call_id", "tool_name", "result". When the result may become the final answer
    # (tool_use_behavior) of an agent with output guardrails, "result_withheld": True instead
    # of "result": the checked answer arrives in FINAL_OUTPUT (P39 G4).
    TOOL_COMPLETED = "tool_completed"
    FINAL_OUTPUT = "final_output"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    RUN_CANCELLED = "run_cancelled"
    # Model text as it is generated (only when streaming is requested and supported).
    # data "turn", "text". When the active agent has output guardrails the reply is not
    # streamed unchecked (Agent(stream_guard=...), P39 G4): "hold" sends none (the checked
    # text is in FINAL_OUTPUT); "chunked" sends only checked windows, with "checked": True.
    TEXT_DELTA = "text_delta"
    # A tool error or invalid output was sent back to the model to try again.
    RETRY = "retry"
    # Control moved to another agent: data has "from", "to", "reason", "path".
    HANDOFF = "handoff"
    # The run stopped before tool calls that need human approval.
    RUN_PAUSED = "run_paused"
    # An agent became active (run start, or the target of a handoff): data "agent", "path".
    AGENT_STARTED = "agent_started"
    # With output_type and stream_text: the answer parsed so far (best effort, unvalidated).
    PARTIAL_OUTPUT = "partial_output"
    # With stream_text: a piece of a tool call as the model writes it. data "turn", "index",
    # "id", "name" (None until the model sends them) and "arguments_delta". Raw model output,
    # before validation, guardrails and approval: for display only.
    TOOL_CALL_DELTA = "tool_call_delta"
    # ctx.emit(name, **data) from a tool or hook: data "name", "data", "agent",
    # "tool_call_id" and "tool_name" (None outside a tool call).
    CUSTOM = "custom"
    # A guardrail did more than pass: it tripped (action "fail"), rewrote ("scrub") or
    # rejected ("reject"), or crashed or timed out ("fail" with "error_type"). data
    # "stage" ("input", "output", "tool_input" or "tool_output"), "guardrail", "action",
    # "agent", "latency_ms"; on tool stages "tool_name" and "tool_call_id"; and, only when
    # the guardrail's info holds them as plain values, "categories", "score", "backend",
    # "cached", "rule", "decision" and "error_type". Never the checked content, the
    # rejection message or other info. Precedes RUN_FAILED when the run stops.
    GUARDRAIL_DECISION = "guardrail_decision"
    # An output guardrail stopped the reply (P39 G4). data "stage" ("output"), "guardrail",
    # "agent", "turn", "phase" ("chunk": a streamed window or text written before tool
    # calls, "final": the whole answer),
    # "stream_guard" (the turn's mode: "hold", "chunked", "passthrough", or None when text
    # was not streamed) and "shown" (True when checked text of this reply was already
    # shown, so a client should discard it). Never the checked or blocked text. Follows the
    # GUARDRAIL_DECISION of the trip and precedes RUN_FAILED.
    GUARDRAIL_TRIPPED = "guardrail_tripped"
    # Team streaming (Team.stream_async). data has "team" and "member_path" (list of path
    # segments of the team inside the root team; empty for the root team).
    TEAM_STARTED = "team_started"
    # data["result"] is the TeamResult, equal to what Team.run_async returns.
    TEAM_COMPLETED = "team_completed"
    # Stopped for human approval; data["result"] is the paused TeamResult.
    TEAM_PAUSED = "team_paused"
    # A member raised; data "error" and "error_type". The error is raised after this event.
    TEAM_FAILED = "team_failed"
    # A member (agent or nested team) began / finished: data "team", "member", "member_path".
    # MEMBER_COMPLETED also has "status" and "result" (RunResult or TeamResult).
    MEMBER_STARTED = "member_started"
    MEMBER_COMPLETED = "member_completed"
    # A member raised; data "error", "error_type" and the member tags. TEAM_FAILED follows.
    MEMBER_FAILED = "member_failed"
    # Workflow streaming (WorkflowEngine.stream). data has "workflow" and "workflow_run_id".
    WORKFLOW_STARTED = "workflow_started"
    WORKFLOW_COMPLETED = "workflow_completed"  # data["result"] is the WorkflowRunResult
    WORKFLOW_PAUSED = "workflow_paused"  # data["result"] is the paused WorkflowRunResult
    WORKFLOW_FAILED = "workflow_failed"  # data "error", "error_type"; the error is raised next
    # A workflow node: data "node_id", "step" (1-based). NODE_COMPLETED adds "attempts",
    # "branch" and "updated_keys"; NODE_PAUSED adds "reason" ("approval" or "interrupt");
    # NODE_FAILED adds "error".
    NODE_STARTED = "node_started"
    NODE_COMPLETED = "node_completed"
    NODE_PAUSED = "node_paused"
    NODE_FAILED = "node_failed"


@dataclass(frozen=True)
class RunEvent:
    """An event emitted during agent execution.

    Ordered per-run events allow callers to observe lifecycle progression,
    tool invocations, intermediate model turns, and final outcomes.
    """

    type: RunEventType
    run_id: str
    timestamp: float = field(default_factory=time.time)
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        """True if this event ends the stream it came from.

        In a team stream, the events of members and nested teams carry
        ``data["member_path"]`` and are never terminal; only the root team's last event is.
        """
        if self.data.get("member_path"):
            return False
        return self.type in (
            RunEventType.RUN_COMPLETED,
            RunEventType.RUN_FAILED,
            RunEventType.RUN_CANCELLED,
            RunEventType.RUN_PAUSED,
            RunEventType.TEAM_COMPLETED,
            RunEventType.TEAM_PAUSED,
            RunEventType.TEAM_FAILED,
            RunEventType.WORKFLOW_COMPLETED,
            RunEventType.WORKFLOW_PAUSED,
            RunEventType.WORKFLOW_FAILED,
        )
