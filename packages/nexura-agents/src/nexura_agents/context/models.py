# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Data models and policy definitions for conservative context selection."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class ContextPolicy:
    """Configuration for inspectable conservative context selection.

    Defaults to opt-in (enabled=False). Short contexts below threshold bypass selection.
    """

    policy_id: str = "conservative_context_selection"
    version: str = "2026.10"
    enabled: bool = False
    max_context_tokens: int = 4096
    short_context_threshold_tokens: int = 1024
    overflow_strategy: Literal["fail", "compact_tables", "truncate_unprotected"] = "fail"
    protect_instructions: bool = True
    protect_corrections: bool = True
    protect_negations: bool = True
    protect_identifiers: bool = True
    protect_financial_amounts: bool = True
    protect_approval_arguments: bool = True
    protect_tool_associations: bool = True
    protect_required_evidence: bool = True
    json_projections: dict[str, list[str]] = field(default_factory=dict)
    max_table_rows: int = 50
    max_log_lines: int = 100
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ContextItem:
    """An individual candidate unit evaluated for context assembly."""

    item_id: str
    item_type: Literal[
        "message",
        "system_prompt",
        "tool_definition",
        "retrieval_chunk",
        "structured_json",
        "table",
        "log",
        "approval",
    ] = "message"
    content: Any = ""
    role: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    source_provenance: str | None = None
    workspace_id: str | None = None
    is_deleted: bool = False
    is_revoked: bool = False
    is_required_evidence: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ContextItemRecord:
    """Persisted decision record for an individual context item."""

    item_id: str
    item_type: str
    role: str | None = None
    tool_call_id: str | None = None
    source_provenance: str | None = None
    decision: Literal["included", "omitted", "projected", "compacted"] = "included"
    reason: str = ""
    is_protected: bool = False
    protected_reasons: list[str] = field(default_factory=list)
    original_tokens: int = 0
    projected_tokens: int = 0
    content_preview: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "item_type": self.item_type,
            "role": self.role,
            "tool_call_id": self.tool_call_id,
            "source_provenance": self.source_provenance,
            "decision": self.decision,
            "reason": self.reason,
            "is_protected": self.is_protected,
            "protected_reasons": list(self.protected_reasons),
            "original_tokens": self.original_tokens,
            "projected_tokens": self.projected_tokens,
            "content_preview": self.content_preview,
        }


@dataclass
class ContextDecisionRecord:
    """Audit record capturing all context selection decisions for a model invocation."""

    record_id: str
    run_id: str | None = None
    node_id: str | None = None
    policy_version: str = "2026.10"
    status: Literal[
        "applied",
        "bypassed_short_context",
        "bypassed_disabled",
        "overflow_failed",
    ] = "applied"
    initial_tokens: int = 0
    projected_tokens: int = 0
    reduction_ratio: float = 0.0
    items: list[ContextItemRecord] = field(default_factory=list)
    overflow_error: str | None = None
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "run_id": self.run_id,
            "node_id": self.node_id,
            "policy_version": self.policy_version,
            "status": self.status,
            "initial_tokens": self.initial_tokens,
            "projected_tokens": self.projected_tokens,
            "reduction_ratio": self.reduction_ratio,
            "items": [it.to_dict() for it in self.items],
            "overflow_error": self.overflow_error,
            "timestamp": self.timestamp,
            "metadata": dict(self.metadata),
        }


@dataclass
class ContextBundle:
    """Final assembled context payload ready for provider dispatch."""

    messages: list[Any]
    tools: list[dict[str, Any]] | None
    decision_record: ContextDecisionRecord
    total_tokens: int = 0
    is_reduced: bool = False
    capacity_limit: int = 4096
    remaining_tokens: int = 4096
