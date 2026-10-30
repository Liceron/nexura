# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Typed errors for vector storage, search, and index generation operations."""

from __future__ import annotations

from typing import Any

from nexura_agents.contracts.errors import ContractError


class VectorStoreError(ContractError):
    """Base error for vector store failures."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message,
            code="VECTOR_STORE_ERROR",
            details=details or {},
        )


class VectorDimensionMismatchError(VectorStoreError):
    """Raised when an inserted vector or search query does not match index dimensions."""

    def __init__(
        self,
        expected: int,
        actual: int,
        details: dict[str, Any] | None = None,
    ) -> None:
        det = details or {}
        det.update({"expected_dimension": expected, "actual_dimension": actual})
        super().__init__(
            f"Vector dimension mismatch: index expects {expected}, got vector of size {actual}.",
            details=det,
        )


class CollectionNotFoundError(VectorStoreError):
    """Raised when a specified collection does not exist in the workspace."""

    def __init__(
        self,
        collection_id: str,
        workspace_id: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        det = details or {}
        det.update({"collection_id": collection_id, "workspace_id": workspace_id})
        super().__init__(
            f"Collection '{collection_id}' not found in workspace '{workspace_id}'.",
            details=det,
        )


class GenerationNotFoundError(VectorStoreError):
    """Raised when a targeted index generation does not exist."""

    def __init__(
        self,
        generation_id: str,
        collection_id: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        det = details or {}
        det.update({"generation_id": generation_id, "collection_id": collection_id})
        super().__init__(
            f"Index generation '{generation_id}' not found in collection '{collection_id}'.",
            details=det,
        )


class TenantIsolationViolationError(VectorStoreError):
    """Raised when a caller attempts cross-tenant access or filter overriding."""

    def __init__(
        self,
        attempted_workspace: str,
        actual_workspace: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        det = details or {}
        det.update(
            {
                "attempted_workspace": attempted_workspace,
                "actual_workspace": actual_workspace,
            }
        )
        super().__init__(
            f"Tenant isolation violation: cannot access workspace '{attempted_workspace}' "
            f"from context of '{actual_workspace}'.",
            details=det,
        )


class ImmutableGenerationError(VectorStoreError):
    """Raised when mutating a retired or dropped index generation."""

    def __init__(
        self,
        generation_id: str,
        status: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        det = details or {}
        det.update({"generation_id": generation_id, "status": status})
        super().__init__(
            f"Cannot modify index generation '{generation_id}' with status '{status}'.",
            details=det,
        )
