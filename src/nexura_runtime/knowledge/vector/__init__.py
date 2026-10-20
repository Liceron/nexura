# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Vector storage, similarity search, and index generations package."""

from __future__ import annotations

from nexura_runtime.knowledge.vector.errors import (
    CollectionNotFoundError,
    GenerationNotFoundError,
    ImmutableGenerationError,
    TenantIsolationViolationError,
    VectorDimensionMismatchError,
    VectorStoreError,
)
from nexura_runtime.knowledge.vector.metrics import (
    compute_score,
    cosine_similarity,
    dot_product,
    euclidean_distance,
)
from nexura_runtime.knowledge.vector.models import (
    IndexGeneration,
    VectorRecord,
    VectorSearchQuery,
    VectorSearchResult,
    matches_filter,
)
from nexura_runtime.knowledge.vector.module import VectorStoreModule
from nexura_runtime.knowledge.vector.sqlite import SqliteVectorStore
from nexura_runtime.knowledge.vector.store import (
    InMemoryVectorStore,
    VectorStore,
)
from nexura_runtime.modules.registry import default_registry

_default_vector_module = VectorStoreModule()
if not default_registry.has(_default_vector_module.manifest.module_id):
    default_registry.register(_default_vector_module)

__all__ = [
    "VectorStore",
    "InMemoryVectorStore",
    "SqliteVectorStore",
    "VectorRecord",
    "IndexGeneration",
    "VectorSearchQuery",
    "VectorSearchResult",
    "matches_filter",
    "VectorStoreError",
    "VectorDimensionMismatchError",
    "CollectionNotFoundError",
    "GenerationNotFoundError",
    "TenantIsolationViolationError",
    "ImmutableGenerationError",
    "cosine_similarity",
    "dot_product",
    "euclidean_distance",
    "compute_score",
    "VectorStoreModule",
]
