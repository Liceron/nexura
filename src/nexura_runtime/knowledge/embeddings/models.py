# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Pydantic data models for EmbeddedChunk, BatchEmbeddingResult, and EmbeddingBudget."""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, Field

from nexura_runtime.knowledge.chunks.models import IndexLineage, TextChunk
from nexura_runtime.knowledge.embeddings.errors import EmbeddingBudgetExhaustedError


class EmbeddedChunk(BaseModel):
    """A TextChunk paired with its dense vector embedding and index lineage."""

    chunk_id: str
    document_id: str
    chunk_index: int
    text: str
    embedding: list[float]
    content_hash: str
    lineage_id: str
    dimensions: int
    start_char: int
    end_char: int
    token_count: int
    page_start: int = 1
    page_end: int = 1
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: float = Field(default_factory=time.time)

    @classmethod
    def from_chunk(
        cls,
        chunk: TextChunk,
        embedding: list[float],
        lineage: IndexLineage,
    ) -> EmbeddedChunk:
        """Construct an EmbeddedChunk from a TextChunk and computed embedding vector."""
        return cls(
            chunk_id=chunk.chunk_id,
            document_id=chunk.document_id,
            chunk_index=chunk.chunk_index,
            text=chunk.text,
            embedding=embedding,
            content_hash=chunk.content_hash,
            lineage_id=lineage.lineage_id,
            dimensions=len(embedding),
            start_char=chunk.start_char,
            end_char=chunk.end_char,
            token_count=chunk.token_count,
            page_start=chunk.page_start,
            page_end=chunk.page_end,
            metadata=dict(chunk.metadata),
        )


class BatchEmbeddingResult(BaseModel):
    """Outcome of a batch embedding generation operation."""

    embedded_chunks: list[EmbeddedChunk]
    lineage: IndexLineage
    cached_count: int
    computed_count: int
    total_tokens: int
    duration_ms: float = 0.0


class EmbeddingBudget(BaseModel):
    """Budget tracker enforcing hard upper bounds on token consumption."""

    max_tokens: int | None = None
    used_tokens: int = 0

    @property
    def remaining_tokens(self) -> int | None:
        """Return remaining tokens before budget exhaustion, or None if unlimited."""
        if self.max_tokens is None:
            return None
        return max(0, self.max_tokens - self.used_tokens)

    def can_consume(self, tokens: int) -> bool:
        """Check whether consuming tokens would stay within budget."""
        if self.max_tokens is None:
            return True
        return self.used_tokens + tokens <= self.max_tokens

    def consume(self, tokens: int) -> None:
        """Consume tokens, raising EmbeddingBudgetExhaustedError if exceeded."""
        if self.max_tokens is not None and self.used_tokens + tokens > self.max_tokens:
            remaining = max(0, self.max_tokens - self.used_tokens)
            raise EmbeddingBudgetExhaustedError(
                requested=tokens,
                remaining=remaining,
                limit=self.max_tokens,
            )
        self.used_tokens += tokens
