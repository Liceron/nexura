# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Pydantic data models for approval requests and decisions."""

from __future__ import annotations

import time
from typing import Any, Literal

from nexura_agents.contracts.canonical import canonical_digest
from pydantic import BaseModel, Field

ApprovalStatus = Literal["pending", "approved", "rejected", "expired"]

#: Version of the dispatch identity recorded with every new approval request. Rows stored
#: before identity binding existed have no identity (empty ``identity_digest``) and are
#: refused at dispatch rather than trusted.
DISPATCH_IDENTITY_VERSION = 1

#: The fields an approved request is bound to. A dispatch claim must present the same values.
DISPATCH_IDENTITY_FIELDS: tuple[str, ...] = (
    "workspace_id",
    "run_id",
    "node_id",
    "spec_digest",
    "action_kind",
    "action_name",
    "payload_digest",
)


def dispatch_identity(
    *,
    workspace_id: str,
    run_id: str,
    node_id: str | None,
    spec_digest: str,
    action_kind: str,
    action_name: str,
    payload_digest: str,
) -> dict[str, Any]:
    """The exact action an approval authorises: where it runs, what it is and its payload."""
    return {
        "workspace_id": workspace_id,
        "run_id": run_id,
        "node_id": node_id,
        "spec_digest": spec_digest,
        "action_kind": action_kind,
        "action_name": action_name,
        "payload_digest": payload_digest,
    }


def dispatch_identity_digest(identity: dict[str, Any]) -> str:
    """Canonical SHA-256 of a dispatch identity (versioned, so the scheme can change later)."""
    return canonical_digest({"v": DISPATCH_IDENTITY_VERSION, **identity})


class ApprovalDecisionRecord(BaseModel):
    """Immutable audit record of an approval decision made by a principal."""

    approval_id: str
    workspace_id: str
    decided_by_principal_id: str
    decided_by_role: str
    decision: Literal["approved", "rejected"]
    reason: str = ""
    expected_payload_digest: str = ""
    decided_at: float = Field(default_factory=time.time)


class ApprovalRequest(BaseModel):
    """Immutable request for human or multi-party approval before executing side effects."""

    approval_id: str
    workspace_id: str
    run_id: str
    node_id: str | None = None
    attempt_id: str = "att_1"
    action_kind: str = "workflow_node"
    required_role: str = "admin"
    action_payload: dict[str, Any] = Field(default_factory=dict)
    payload_digest: str = ""
    spec_digest: str = ""
    #: Name of the tool, node handler or action the approval is for (part of the identity).
    action_name: str = ""
    #: Digest of :meth:`dispatch_identity`, set when the request is stored. Empty for rows
    #: stored before identity binding: those are never dispatched.
    identity_digest: str = ""
    policy_version: str = "1.0.0"
    status: ApprovalStatus = "pending"
    claimed_by: str | None = None
    claimed_at: float | None = None
    expires_at: float
    decision: ApprovalDecisionRecord | None = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    def dispatch_identity(self) -> dict[str, Any]:
        """The identity this request authorises (see :data:`DISPATCH_IDENTITY_FIELDS`)."""
        return dispatch_identity(
            workspace_id=self.workspace_id,
            run_id=self.run_id,
            node_id=self.node_id,
            spec_digest=self.spec_digest,
            action_kind=self.action_kind,
            action_name=self.action_name,
            payload_digest=self.payload_digest,
        )
