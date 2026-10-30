# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Pydantic data models for Document records, pages, and ingestion jobs."""

from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, Field


class DocumentPage(BaseModel):
    """A single page or logical segment within an ingested document."""

    page_number: int  # 1-indexed
    text: str
    span_start: int
    span_end: int
    metadata: dict[str, Any] = Field(default_factory=dict)


class Document(BaseModel):
    """Immutable ingested document record with provenance, hash, text, and page spans."""

    document_id: str
    collection_id: str = "default"
    origin: str  # Filename, URI, or source identifier
    content_hash: str  # SHA-256 hex digest of raw binary bytes
    mime_type: str
    text: str  # Normalized text content
    pages: list[DocumentPage] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: float = Field(default_factory=time.time)
    lineage: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_deleted(self) -> bool:
        return bool(self.lineage.get("deleted", False))


class IngestionJob(BaseModel):
    """Audit and tracking record for an ingestion operation."""

    job_id: str
    workspace_id: str = "default"
    collection_id: str = "default"
    status: Literal["pending", "completed", "failed"] = "pending"
    document_ids: list[str] = Field(default_factory=list)
    error: str | None = None
    created_at: float = Field(default_factory=time.time)
    completed_at: float | None = None
