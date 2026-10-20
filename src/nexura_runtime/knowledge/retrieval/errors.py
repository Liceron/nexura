# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Typed errors for retrieval, reranking, and citation-backed RAG."""

from __future__ import annotations

from typing import Any

from nexura_agents.contracts.errors import ContractError


class RetrievalError(ContractError):
    """Base error for knowledge retrieval and RAG failures."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message,
            code="RETRIEVAL_ERROR",
            details=details or {},
        )


class RerankerFailureError(RetrievalError):
    """Raised when the reranker fails and fallback policy is configured to raise."""

    def __init__(
        self,
        reranker_name: str,
        reason: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        det = details or {}
        det.update({"reranker_name": reranker_name, "reason": reason})
        super().__init__(
            f"Reranker '{reranker_name}' failed: {reason}",
            details=det,
        )


class ContextBudgetExceededError(RetrievalError):
    """Raised when context packing cannot fit even minimum required evidence."""

    def __init__(
        self,
        required_tokens: int,
        max_context_tokens: int,
        details: dict[str, Any] | None = None,
    ) -> None:
        det = details or {}
        det.update(
            {
                "required_tokens": required_tokens,
                "max_context_tokens": max_context_tokens,
            }
        )
        super().__init__(
            f"Context budget exceeded: required {required_tokens} tokens, "
            f"limit is {max_context_tokens}.",
            details=det,
        )


class InsufficientEvidenceError(RetrievalError):
    """Raised when strict evidence verification requires sources but none were found."""

    def __init__(self, query: str, details: dict[str, Any] | None = None) -> None:
        det = details or {}
        det.update({"query": query})
        super().__init__(
            f"Insufficient evidence found to answer query: {query!r}",
            details=det,
        )
