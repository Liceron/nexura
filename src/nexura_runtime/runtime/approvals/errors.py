# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Structured errors for approval policies and enforcement."""

from __future__ import annotations

from typing import Any

from nexura_agents.contracts.errors import ContractError, ContractErrorCode


class ApprovalError(ContractError):
    """Base error for approval policy failures."""

    def __init__(
        self,
        message: str,
        *,
        code: ContractErrorCode | str = ContractErrorCode.POLICY_DENIED,
        details: dict[str, Any] | None = None,
        original_error: BaseException | None = None,
    ) -> None:
        super().__init__(
            message,
            code=code,
            details=details,
            original_error=original_error,
        )


class ApprovalRequiredError(ApprovalError):
    """Raised when an operation pauses because human or multi-party approval is required."""

    def __init__(
        self,
        message: str,
        *,
        approval_id: str,
        action_name: str,
        required_role: str,
        payload_digest: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        merged_details = {
            "approval_id": approval_id,
            "action_name": action_name,
            "required_role": required_role,
            "payload_digest": payload_digest,
        }
        if details:
            merged_details.update(details)
        super().__init__(
            message,
            code=ContractErrorCode.POLICY_DENIED,
            details=merged_details,
        )
        self.approval_id = approval_id
        self.action_name = action_name
        self.required_role = required_role
        self.payload_digest = payload_digest


class WrongActorError(ApprovalError):
    """Raised when an approval decision is attempted by a principal without the required role."""

    def __init__(
        self,
        message: str,
        *,
        deciding_role: str,
        required_role: str,
        principal_id: str,
    ) -> None:
        super().__init__(
            message,
            code=ContractErrorCode.AUTHENTICATION_FAILED,
            details={
                "deciding_role": deciding_role,
                "required_role": required_role,
                "principal_id": principal_id,
            },
        )
        self.deciding_role = deciding_role
        self.required_role = required_role
        self.principal_id = principal_id


class WrongWorkspaceError(ApprovalError):
    """Raised when an approval decision is submitted to a workspace different from the request."""

    def __init__(
        self,
        message: str,
        *,
        request_workspace: str,
        decision_workspace: str,
        approval_id: str,
    ) -> None:
        super().__init__(
            message,
            code=ContractErrorCode.POLICY_DENIED,
            details={
                "request_workspace": request_workspace,
                "decision_workspace": decision_workspace,
                "approval_id": approval_id,
            },
        )
        self.request_workspace = request_workspace
        self.decision_workspace = decision_workspace
        self.approval_id = approval_id


class MutatedArgumentsError(ApprovalError):
    """Raised when arguments at decision time do not match the original requested payload."""

    def __init__(
        self,
        message: str,
        *,
        expected_digest: str,
        actual_digest: str,
        approval_id: str,
    ) -> None:
        super().__init__(
            message,
            code=ContractErrorCode.INVALID_SCHEMA,
            details={
                "expected_digest": expected_digest,
                "actual_digest": actual_digest,
                "approval_id": approval_id,
            },
        )
        self.expected_digest = expected_digest
        self.actual_digest = actual_digest
        self.approval_id = approval_id


class ApprovalExpiredError(ApprovalError):
    """Raised when an approval request has expired and cannot be approved."""

    def __init__(
        self,
        message: str,
        *,
        approval_id: str,
        expired_at: float,
    ) -> None:
        super().__init__(
            message,
            code=ContractErrorCode.BUDGET_EXHAUSTED,
            details={
                "approval_id": approval_id,
                "expired_at": expired_at,
            },
        )
        self.approval_id = approval_id
        self.expired_at = expired_at


class ApprovalAlreadyDecidedError(ApprovalError):
    """Raised when attempting to decide an approval request that is already approved or rejected."""

    def __init__(
        self,
        message: str,
        *,
        approval_id: str,
        current_status: str,
    ) -> None:
        super().__init__(
            message,
            code=ContractErrorCode.POLICY_DENIED,
            details={
                "approval_id": approval_id,
                "current_status": current_status,
            },
        )


class ApprovalClaimConflictError(ApprovalError):
    """Raised when another worker or process has already claimed this approved request."""

    def __init__(
        self,
        message: str,
        *,
        approval_id: str,
        claimed_by: str,
    ) -> None:
        super().__init__(
            message,
            code=ContractErrorCode.POLICY_DENIED,
            details={
                "approval_id": approval_id,
                "claimed_by": claimed_by,
            },
        )


class ApprovalIdentityMismatchError(ApprovalError):
    """Raised when an approved request is presented for a different action than it authorises.

    An approval is bound to its dispatch identity: workspace, run, node, spec digest, action
    kind, action name and payload digest. Presenting the approval id with any other value is
    refused and nothing is claimed or dispatched.
    """

    def __init__(
        self,
        message: str,
        *,
        approval_id: str,
        mismatched_fields: list[str],
        reason: str = "identity_mismatch",
    ) -> None:
        super().__init__(
            message,
            code=ContractErrorCode.POLICY_DENIED,
            details={
                "approval_id": approval_id,
                "mismatched_fields": list(mismatched_fields),
                "reason": reason,
            },
        )
        self.approval_id = approval_id
        self.mismatched_fields = list(mismatched_fields)
        self.reason = reason


class ApprovalIdentityMissingError(ApprovalIdentityMismatchError):
    """Raised for a stored request with no recorded dispatch identity (stored before binding).

    Such requests cannot prove which action they approved, so they are never dispatched.
    Create a fresh approval request instead.
    """

    def __init__(self, message: str, *, approval_id: str) -> None:
        super().__init__(
            message,
            approval_id=approval_id,
            mismatched_fields=[],
            reason="identity_missing",
        )
