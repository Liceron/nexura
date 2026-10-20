# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Artifact storage error classes and stable codes."""

from __future__ import annotations

from typing import Any

from nexura_agents.contracts.errors import ContractError, ContractErrorCode


class ArtifactError(ContractError):
    """Base exception for all artifact storage failures."""


class ArtifactNotFoundError(ArtifactError):
    """Raised when an artifact cannot be found in the specified workspace."""

    def __init__(self, workspace_id: str, artifact_id: str) -> None:
        super().__init__(
            f"Artifact '{artifact_id}' not found in workspace '{workspace_id}'.",
            code=ContractErrorCode.ARTIFACT_NOT_FOUND,
            details={"workspace_id": workspace_id, "artifact_id": artifact_id},
        )


class ArtifactSecurityError(ArtifactError):
    """Raised on path traversal, symlink escapes, or cross-workspace access attempts."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message,
            code=ContractErrorCode.POLICY_DENIED,
            details=details,
        )


class ArtifactLimitExceededError(ArtifactError):
    """Raised when an uploaded artifact exceeds configured size limits."""

    def __init__(self, size_bytes: int, max_bytes: int) -> None:
        super().__init__(
            f"Artifact size {size_bytes} bytes exceeds maximum limit of {max_bytes} bytes.",
            code=ContractErrorCode.POLICY_DENIED,
            details={"size_bytes": size_bytes, "max_bytes": max_bytes},
        )


class ArtifactHashMismatchError(ArtifactError):
    """Raised when an artifact's content digest does not match its expected checksum."""

    def __init__(self, expected_digest: str, actual_digest: str) -> None:
        super().__init__(
            f"Artifact checksum verification failed: "
            f"expected {expected_digest}, got {actual_digest}.",
            code=ContractErrorCode.INDETERMINATE_EFFECT,
            details={"expected_digest": expected_digest, "actual_digest": actual_digest},
        )
