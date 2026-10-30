# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Errors for vector embeddings, caching, and token budget operations."""

from __future__ import annotations

from typing import Any

from nexura_agents.contracts.errors import ContractError


class EmbeddingError(ContractError):
    """Base error for embedding generation failures."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message,
            code="EMBEDDING_FAILED",
            details=details or {},
        )


class DimensionMismatchError(EmbeddingError):
    """Raised when embedding dimension does not match index lineage or expected dimension."""

    def __init__(
        self,
        expected: int,
        actual: int,
        details: dict[str, Any] | None = None,
    ) -> None:
        det = details or {}
        det.update({"expected_dimension": expected, "actual_dimension": actual})
        super().__init__(
            f"Embedding dimension mismatch: expected {expected}, got {actual}.",
            details=det,
        )


class EmbeddingBudgetExhaustedError(EmbeddingError):
    """Raised when embedding token or request budget is exhausted."""

    def __init__(
        self,
        requested: int,
        remaining: int,
        limit: int,
        details: dict[str, Any] | None = None,
    ) -> None:
        det = details or {}
        det.update({"requested": requested, "remaining": remaining, "limit": limit})
        super().__init__(
            f"Embedding budget exhausted: requested {requested} tokens, "
            f"only {remaining} of {limit} remaining.",
            details=det,
        )


class LineageMismatchError(EmbeddingError):
    """Raised when attempting to mix chunks or embeddings across incompatible index lineages."""

    def __init__(
        self,
        expected_lineage: str,
        actual_lineage: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        det = details or {}
        det.update(
            {
                "expected_lineage": expected_lineage,
                "actual_lineage": actual_lineage,
            }
        )
        super().__init__(
            f"Lineage mismatch: expected '{expected_lineage}', got '{actual_lineage}'.",
            details=det,
        )


class EmbeddingProviderError(EmbeddingError):
    """Raised when the underlying embedding provider fails."""

    def __init__(
        self,
        provider_name: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        det = details or {}
        det.update({"provider_name": provider_name})
        super().__init__(f"Provider '{provider_name}' failed: {message}", details=det)
