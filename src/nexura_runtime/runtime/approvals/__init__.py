# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Approval policies, gates, and human-in-the-loop runtime package."""

from nexura_runtime.modules.registry import default_registry
from nexura_runtime.runtime.approvals.errors import (
    ApprovalAlreadyDecidedError,
    ApprovalClaimConflictError,
    ApprovalError,
    ApprovalExpiredError,
    ApprovalIdentityMismatchError,
    ApprovalIdentityMissingError,
    ApprovalRequiredError,
    MutatedArgumentsError,
    WrongActorError,
    WrongWorkspaceError,
)
from nexura_runtime.runtime.approvals.gate import ApprovalGate
from nexura_runtime.runtime.approvals.manager import ApprovalPolicyManager, ClaimOutcome
from nexura_runtime.runtime.approvals.models import (
    ApprovalDecisionRecord,
    ApprovalRequest,
    ApprovalStatus,
    dispatch_identity,
    dispatch_identity_digest,
)
from nexura_runtime.runtime.approvals.module import ApprovalModule

__all__ = [
    "ApprovalPolicyManager",
    "ApprovalGate",
    "ApprovalModule",
    "ApprovalRequest",
    "ApprovalDecisionRecord",
    "ApprovalStatus",
    "ApprovalError",
    "ApprovalRequiredError",
    "WrongActorError",
    "WrongWorkspaceError",
    "MutatedArgumentsError",
    "ApprovalExpiredError",
    "ApprovalAlreadyDecidedError",
    "ApprovalClaimConflictError",
    "ApprovalIdentityMismatchError",
    "ApprovalIdentityMissingError",
    "ClaimOutcome",
    "dispatch_identity",
    "dispatch_identity_digest",
]

# Auto-register approval module in default registry
if not default_registry.has("nexura.runtime.approvals"):
    default_registry.register(ApprovalModule)
