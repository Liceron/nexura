# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Credential error types and stable codes."""

from __future__ import annotations

from typing import Any

from nexura_agents.contracts.errors import ContractError, ContractErrorCode


class CredentialError(ContractError):
    """Base exception for all credential-related errors."""


class CredentialNotFoundError(CredentialError):
    """Raised when a requested credential cannot be found."""

    def __init__(self, credential_id: str, workspace_id: str) -> None:
        super().__init__(
            f"Credential '{credential_id}' not found in workspace '{workspace_id}'.",
            code=ContractErrorCode.POLICY_DENIED,
            details={"credential_id": credential_id, "workspace_id": workspace_id},
        )


class CredentialAlreadyExistsError(CredentialError):
    """Raised by a create-only store when the workspace already has the credential."""

    def __init__(self, credential_id: str, workspace_id: str) -> None:
        super().__init__(
            f"Credential '{credential_id}' already exists in workspace '{workspace_id}'.",
            code=ContractErrorCode.POLICY_DENIED,
            details={"credential_id": credential_id, "workspace_id": workspace_id},
        )


class CredentialAccessDeniedError(CredentialError):
    """Raised on cross-workspace access or unauthorized credential retrieval."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message,
            code=ContractErrorCode.POLICY_DENIED,
            details=details,
        )
