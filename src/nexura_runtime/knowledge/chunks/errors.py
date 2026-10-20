# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Errors for chunking and token counting operations."""

from __future__ import annotations

from typing import Any

from nexura_agents.contracts.errors import ContractError


class ChunkingError(ContractError):
    """Base error for chunking and text segmentation failures."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message,
            code="CHUNKING_FAILED",
            details=details or {},
        )


class TokenLimitExceededError(ChunkingError):
    """Raised when a text segment exceeds the configured token limit and cannot be split."""

    def __init__(
        self,
        token_count: int,
        max_tokens: int,
        details: dict[str, Any] | None = None,
    ) -> None:
        det = details or {}
        det.update({"token_count": token_count, "max_tokens": max_tokens})
        super().__init__(
            f"Token count ({token_count}) exceeds limit of {max_tokens} tokens.",
            details=det,
        )


class InvalidChunkerConfigError(ChunkingError):
    """Raised when chunker parameters are invalid (e.g. chunk_overlap >= chunk_size)."""

    def __init__(self, reason: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            f"Invalid chunker configuration: {reason}",
            details=details or {},
        )
