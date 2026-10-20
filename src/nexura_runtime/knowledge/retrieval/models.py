# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Data models for citations, retrieved items, context packing, and RAG outcomes."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class Citation(BaseModel):
    """Verifiable citation referencing source document, page, and chunk character spans."""

    citation_id: str  # e.g. "[1]"
    document_id: str
    origin: str  # Filename or URI
    chunk_id: str
    page_start: int = 1
    page_end: int = 1
    start_char: int = 0
    end_char: int = 0
    snippet: str
    score: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class RetrievedItem(BaseModel):
    """Retrieved chunk candidate with multi-method similarity scoring."""

    chunk_id: str
    document_id: str
    origin: str = ""
    text: str
    page_start: int = 1
    page_end: int = 1
    start_char: int = 0
    end_char: int = 0
    dense_score: float | None = None
    lexical_score: float | None = None
    fused_score: float = 0.0
    rerank_score: float | None = None
    score: float = 0.0
    token_count: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class PackedContext(BaseModel):
    """Assembled prompt context bounded by token budget, with verifiable citations."""

    items: list[RetrievedItem] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    citation_map: dict[str, Citation] = Field(default_factory=dict)
    formatted_context: str = ""
    total_tokens: int = 0
    has_sufficient_evidence: bool = True
    truncated: bool = False


class RagQuery(BaseModel):
    """Query payload for hybrid retrieval, reranking, and generation."""

    query: str
    collection_id: str
    workspace_id: str = "default"
    top_k: int = 5
    max_context_tokens: int = 2000
    min_score_threshold: float = 0.0
    filter: dict[str, Any] | None = None
    allowed_document_ids: list[str] | None = None
    dense_weight: float = 0.7
    lexical_weight: float = 0.3
    rerank: bool = False


class RagResponse(BaseModel):
    """Complete citation-backed RAG generation outcome."""

    query: str
    answer: str
    has_sufficient_evidence: bool
    citations: list[Citation] = Field(default_factory=list)
    packed_context: PackedContext
    retrieval_duration_ms: float = 0.0
    generation_duration_ms: float = 0.0
