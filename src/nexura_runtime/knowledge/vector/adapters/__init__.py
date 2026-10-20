# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Shared validation for vector store adapters.

The adapters themselves are separate packages that depend on this runtime: PostgreSQL
pgvector is ``nexura_postgres.PgVectorStoreAdapter`` (``pip install nexura-postgres``) and
Qdrant is ``nexura_qdrant.QdrantVectorStoreAdapter`` (``pip install nexura-qdrant``).
"""

from __future__ import annotations

from nexura_runtime.knowledge.vector.adapters.common import (
    sanitize_and_validate_search_query,
    validate_upsert_records,
)

__all__ = [
    "sanitize_and_validate_search_query",
    "validate_upsert_records",
]
