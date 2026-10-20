# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Structured errors for session management, memory facts, and access policies."""

from __future__ import annotations

from typing import Any

from nexura_agents.contracts.errors import ContractError, ContractErrorCode


class MemoryError(ContractError):
    """Base error for session and memory operations."""

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


class SessionNotFoundError(MemoryError):
    """Raised when a requested session is not found in the workspace."""

    def __init__(self, workspace_id: str, session_id: str) -> None:
        super().__init__(
            f"Session '{session_id}' not found in workspace '{workspace_id}'.",
            code=ContractErrorCode.POLICY_DENIED,
            details={"workspace_id": workspace_id, "session_id": session_id},
        )


class SessionAccessDeniedError(MemoryError):
    """Raised when a principal attempts to read or mutate another principal's session."""

    def __init__(
        self,
        workspace_id: str,
        session_id: str,
        principal_id: str,
        owner_principal_id: str,
    ) -> None:
        super().__init__(
            f"Access denied: Session '{session_id}' is owned by '{owner_principal_id}', "
            f"not '{principal_id}'.",
            code=ContractErrorCode.AUTHENTICATION_FAILED,
            details={
                "workspace_id": workspace_id,
                "session_id": session_id,
                "principal_id": principal_id,
                "owner_principal_id": owner_principal_id,
            },
        )


class FactNotFoundError(MemoryError):
    """Raised when a long-term memory fact does not exist or has been deleted."""

    def __init__(self, workspace_id: str, principal_id: str, fact_id: str) -> None:
        super().__init__(
            f"Fact '{fact_id}' not found for principal '{principal_id}' "
            f"in workspace '{workspace_id}'.",
            code=ContractErrorCode.POLICY_DENIED,
            details={
                "workspace_id": workspace_id,
                "principal_id": principal_id,
                "fact_id": fact_id,
            },
        )


class DuplicateSystemPromptError(MemoryError):
    """Raised when attempting to append duplicate system prompts across turns."""

    def __init__(self, session_id: str) -> None:
        super().__init__(
            f"Duplicate system prompt rejected: "
            f"Session '{session_id}' already has a system prompt.",
            code=ContractErrorCode.INVALID_SCHEMA,
            details={"session_id": session_id},
        )


class SessionRevisionConflictError(MemoryError):
    """Raised when concurrent session updates conflict on revision CAS."""

    def __init__(
        self,
        session_id: str,
        expected_revision: int,
        actual_revision: int,
    ) -> None:
        super().__init__(
            f"Session conflict on '{session_id}': expected revision {expected_revision}, "
            f"found {actual_revision}.",
            code=ContractErrorCode.POLICY_DENIED,
            details={
                "session_id": session_id,
                "expected_revision": expected_revision,
                "actual_revision": actual_revision,
            },
        )
