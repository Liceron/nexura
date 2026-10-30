# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Vector embeddings, batching, caching, and budget enforcement package."""

from __future__ import annotations

from nexura_runtime.knowledge.embeddings.cache import (
    EmbeddingCache,
    InMemoryEmbeddingCache,
    SqliteEmbeddingCache,
)
from nexura_runtime.knowledge.embeddings.embedder import EmbeddingOrchestrator
from nexura_runtime.knowledge.embeddings.errors import (
    DimensionMismatchError,
    EmbeddingBudgetExhaustedError,
    EmbeddingError,
    EmbeddingProviderError,
    LineageMismatchError,
)
from nexura_runtime.knowledge.embeddings.models import (
    BatchEmbeddingResult,
    EmbeddedChunk,
    EmbeddingBudget,
)
from nexura_runtime.knowledge.embeddings.module import EmbeddingModule
from nexura_runtime.knowledge.embeddings.provider import DeterministicEmbeddingProvider
from nexura_runtime.modules.registry import default_registry

_default_embedding_module = EmbeddingModule()
if not default_registry.has(_default_embedding_module.manifest.module_id):
    default_registry.register(_default_embedding_module)

__all__ = [
    "EmbeddingOrchestrator",
    "DeterministicEmbeddingProvider",
    "EmbeddingCache",
    "InMemoryEmbeddingCache",
    "SqliteEmbeddingCache",
    "EmbeddedChunk",
    "BatchEmbeddingResult",
    "EmbeddingBudget",
    "EmbeddingError",
    "DimensionMismatchError",
    "EmbeddingBudgetExhaustedError",
    "LineageMismatchError",
    "EmbeddingProviderError",
    "EmbeddingModule",
]
