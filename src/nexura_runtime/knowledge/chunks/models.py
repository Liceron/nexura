# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Pydantic data models for TextChunk, ChunkMetadata, and IndexLineage."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from pydantic import BaseModel, Field


class IndexLineage(BaseModel):
    """Lineage metadata binding chunker configuration and embedding model parameters.

    Ensures that any variation in chunker configuration, model identifier,
    revision, or target embedding dimensions produces a distinct index lineage.
    """

    chunker_name: str
    chunker_config_hash: str
    model_name: str
    model_revision: str
    dimensions: int
    lineage_id: str = ""
    created_at: float = Field(default_factory=time.time)
    extra: dict[str, Any] = Field(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        if not self.lineage_id:
            raw = (
                f"{self.chunker_name}:{self.chunker_config_hash}:"
                f"{self.model_name}:{self.model_revision}:{self.dimensions}"
            )
            digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
            object.__setattr__(self, "lineage_id", f"lin_{digest}")

    @classmethod
    def compute_config_hash(cls, config: dict[str, Any]) -> str:
        """Deterministically hash chunker configuration parameters."""
        serialized = json.dumps(config, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]

    def is_compatible_with(self, other: IndexLineage) -> bool:
        """Verify whether another lineage is compatible for index querying."""
        return (
            self.dimensions == other.dimensions
            and self.model_name == other.model_name
            and self.model_revision == other.model_revision
        )


class TextChunk(BaseModel):
    """A bounded segment of text with exact source offsets, token counts, and page citations."""

    chunk_id: str
    document_id: str
    chunk_index: int
    text: str
    start_char: int
    end_char: int
    token_count: int
    page_start: int = 1
    page_end: int = 1
    content_hash: str  # SHA-256 hex digest of normalized chunk text
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        document_id: str,
        chunk_index: int,
        text: str,
        start_char: int,
        end_char: int,
        token_count: int,
        page_start: int = 1,
        page_end: int = 1,
        chunker_config_hash: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> TextChunk:
        """Factory constructor generating a deterministic, content-bound chunk_id."""
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        raw_seed = (
            f"{document_id}:{chunk_index}:{start_char}:{end_char}:"
            f"{content_hash[:16]}:{chunker_config_hash[:8]}"
        )
        chunk_id = f"chk_{hashlib.sha256(raw_seed.encode('utf-8')).hexdigest()[:16]}"
        return cls(
            chunk_id=chunk_id,
            document_id=document_id,
            chunk_index=chunk_index,
            text=text,
            start_char=start_char,
            end_char=end_char,
            token_count=token_count,
            page_start=page_start,
            page_end=page_end,
            content_hash=content_hash,
            metadata=metadata or {},
        )
