# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Qdrant vector store for nexura-runtime.

Distributed as ``nexura-qdrant`` (it depends on ``nexura-runtime`` and qdrant-client).
``QdrantVectorStoreAdapter`` implements the runtime's ``VectorStore`` protocol
(``nexura_runtime.knowledge.vector.store``) with workspace-scoped collections and immutable
index generations.
"""

from __future__ import annotations

from nexura_qdrant.qdrant import QdrantVectorStoreAdapter

__all__ = ["QdrantVectorStoreAdapter"]
