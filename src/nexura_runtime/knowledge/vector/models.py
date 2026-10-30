# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Pydantic data models for vector records, index generations, queries, and filters."""

from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, Field

from nexura_runtime.knowledge.chunks.models import IndexLineage


class VectorRecord(BaseModel):
    """A dense vector record with document provenance and arbitrary metadata."""

    id: str  # Typically chunk_id
    workspace_id: str
    collection_id: str
    generation_id: str
    vector: list[float]
    document_id: str = ""
    text: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    is_deleted: bool = False
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


class IndexGeneration(BaseModel):
    """Immutable index generation binding a collection to an embedding lineage."""

    generation_id: str
    collection_id: str
    workspace_id: str
    lineage: IndexLineage
    dimensions: int
    metric: Literal["cosine", "dot_product", "euclidean"] = "cosine"
    status: Literal["building", "active", "retired", "dropped"] = "building"
    record_count: int = 0
    created_at: float = Field(default_factory=time.time)
    activated_at: float | None = None


class VectorSearchQuery(BaseModel):
    """Search request with mandatory workspace/collection scoping and optional filters."""

    vector: list[float]
    collection_id: str
    workspace_id: str
    top_k: int = 10
    filter: dict[str, Any] | None = None
    include_metadata: bool = True
    include_vector: bool = False
    generation_id: str | None = None


class VectorSearchResult(BaseModel):
    """Single scored hit from a vector search query."""

    id: str
    score: float
    document_id: str
    text: str
    metadata: dict[str, Any]
    generation_id: str
    vector: list[float] | None = None


def matches_filter(metadata: dict[str, Any], filter_expr: dict[str, Any] | None) -> bool:
    """Evaluate whether record metadata satisfies a structured filter expression.

    Supports:
    - Direct equality: {"domain": "agentic_ai"}
    - Comparisons: {"$gt": 5, "$gte": 5, "$lt": 10, "$lte": 10, "$eq": ..., "$ne": ...}
    - Set operations: {"$in": ["a", "b"], "$nin": ["c"]}
    - Containment: {"$contains": "sub"}
    - Logical combinations: {"$and": [...], "$or": [...]}
    """
    if not filter_expr:
        return True

    for key, condition in filter_expr.items():
        if key == "$and":
            if not isinstance(condition, list):
                return False
            if not all(matches_filter(metadata, sub) for sub in condition):
                return False
            continue

        if key == "$or":
            if not isinstance(condition, list):
                return False
            if not any(matches_filter(metadata, sub) for sub in condition):
                return False
            continue

        val = metadata.get(key)

        if isinstance(condition, dict):
            for op, target in condition.items():
                if op == "$eq" and val != target:
                    return False
                if op == "$ne" and val == target:
                    return False
                if op == "$in" and val not in target:
                    return False
                if op == "$nin" and val in target:
                    return False
                if op == "$gt" and (val is None or val <= target):
                    return False
                if op == "$gte" and (val is None or val < target):
                    return False
                if op == "$lt" and (val is None or val >= target):
                    return False
                if op == "$lte" and (val is None or val > target):
                    return False
                if op == "$contains":
                    if isinstance(val, (list, set, tuple)):
                        if target not in val:
                            return False
                    elif isinstance(val, str):
                        if str(target) not in val:
                            return False
                    else:
                        return False
        else:
            # Direct equality check
            if val != condition:
                return False

    return True
