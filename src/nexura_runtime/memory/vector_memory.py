# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Agent long-term memory on top of a platform vector store.

``VectorMemoryStore`` implements the ``nexura_agents.MemoryStore`` protocol, so
``Agent(memory=Memory(VectorMemoryStore(vectors, embedder, workspace_id="acme")))``
keeps memories in any ``nexura_runtime.knowledge.vector`` store (in-memory, pgvector,
Qdrant, SQLite) instead of process memory.

Isolation: the vector store scopes every call by ``workspace_id`` (your tenant) and
``collection_id``. Each memory namespace gets its own collection, named from a hash of the
namespace, so namespaces cannot collide or reach each other's records whatever characters
they contain.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Sequence
from typing import Any

from nexura_agents.errors import SDKValidationError
from nexura_agents.memory import (
    Embedder,
    MemoryRecord,
    _as_embedder,
    _check_limit,
    _check_metadata,
    _check_namespace,
    _check_text,
)

from nexura_runtime.knowledge.chunks.models import IndexLineage
from nexura_runtime.knowledge.vector.errors import CollectionNotFoundError
from nexura_runtime.knowledge.vector.models import (
    VectorRecord,
    VectorSearchQuery,
    VectorSearchResult,
)
from nexura_runtime.knowledge.vector.store import VectorStore

__all__ = ["VectorMemoryStore"]

# Aliases: inside the class ``list`` names the ``list()`` method, not the builtin.
_Records = list[MemoryRecord]
_Hits = list[VectorSearchResult]
_Vector = list[float]
_Vectors = list[list[float]]

_LIST_FETCH = 1000  # most records inspected when listing; see ``VectorMemoryStore.list``
_USER_META = "user"  # memory metadata is kept under this key, away from internal fields


class VectorMemoryStore:
    """A ``MemoryStore`` backed by a ``VectorStore`` and an embedder.

    ``workspace_id`` is the tenant key passed to every vector store call; namespaces map to
    collections inside it. ``embedder`` is a ``nexura_agents`` ``Embedder`` or any provider
    object with ``embed()``. Use the same embedder for a collection's whole life: a
    different vector size is rejected by the vector store.
    """

    def __init__(
        self,
        vector_store: VectorStore,
        embedder: Embedder | Any,
        *,
        workspace_id: str = "agent-memory",
        model_name: str | None = None,
    ) -> None:
        for method in ("create_collection", "upsert", "search", "delete", "get_active_generation"):
            if not callable(getattr(vector_store, method, None)):
                raise SDKValidationError(f"vector_store must provide {method}().")
        _check_namespace(workspace_id)
        self.vector_store = vector_store
        self.embedder: Embedder = _as_embedder(embedder)
        self.workspace_id = workspace_id
        self.model_name = model_name or type(embedder).__name__
        self._ready: set[str] = set()

    @staticmethod
    def collection_id(namespace: str) -> str:
        """The vector collection that holds ``namespace`` (a hash; never the raw text)."""
        return "mem_" + hashlib.sha256(namespace.encode("utf-8")).hexdigest()[:32]

    async def add(
        self, namespace: str, text: str, metadata: dict[str, Any] | None = None
    ) -> MemoryRecord:
        """Embed and store ``text`` in the namespace's collection."""
        _check_namespace(namespace)
        _check_text(text)
        meta = _check_metadata(metadata)
        (vector,) = await self._embed([text])
        collection = self.collection_id(namespace)
        if collection not in self._ready:
            lineage = IndexLineage(
                chunker_name="agent-memory",
                chunker_config_hash="none",
                model_name=self.model_name,
                model_revision="v1",
                dimensions=len(vector),
            )
            await self.vector_store.create_collection(
                self.workspace_id, collection, len(vector), lineage
            )
            self._ready.add(collection)
        created_at = time.time()
        record = VectorRecord(
            id=str(uuid.uuid4()),
            workspace_id=self.workspace_id,
            collection_id=collection,
            generation_id="",  # the store fills in the active generation
            vector=vector,
            created_at=created_at,
            document_id=namespace,
            text=text,
            metadata={_USER_META: meta, "namespace": namespace, "created_at": created_at},
        )
        await self.vector_store.upsert(self.workspace_id, collection, [record])
        return MemoryRecord(
            id=record.id,
            text=text,
            namespace=namespace,
            metadata=meta,
            created_at=created_at,
        )

    async def search(
        self, namespace: str, query: str, limit: int = 5, min_score: float | None = None
    ) -> _Records:
        """Closest memories first; ``score`` is the vector store's similarity."""
        _check_namespace(namespace)
        _check_limit(limit)
        if not isinstance(query, str) or not query.strip():
            return []
        (vector,) = await self._embed([query])
        hits = await self._query(namespace, vector, limit)
        return [
            self._record(namespace, h) for h in hits if min_score is None or h.score >= min_score
        ]

    async def delete(self, namespace: str, id: str) -> bool:
        """Hard-delete a memory from this namespace; False if it is not there."""
        _check_namespace(namespace)
        removed = await self.vector_store.delete(
            self.workspace_id, self.collection_id(namespace), [id], hard_delete=True
        )
        return bool(removed)

    async def list(self, namespace: str, limit: int = 100) -> _Records:
        """Newest memories first.

        Vector stores cannot enumerate records, so this inspects up to 1000 of them. A
        namespace with more memories than that may not show its newest ones here.
        """
        _check_namespace(namespace)
        _check_limit(limit)
        generation = await self.vector_store.get_active_generation(
            self.workspace_id, self.collection_id(namespace)
        )
        if generation is None:
            return []
        hits = await self._query(namespace, [1.0] * generation.dimensions, max(limit, _LIST_FETCH))
        records = [self._record(namespace, h, with_score=False) for h in hits]
        records.sort(key=lambda r: r.created_at, reverse=True)
        return records[:limit]

    async def _query(self, namespace: str, vector: _Vector, top_k: int) -> _Hits:
        try:
            return await self.vector_store.search(
                self.workspace_id,
                VectorSearchQuery(
                    vector=vector,
                    collection_id=self.collection_id(namespace),
                    workspace_id=self.workspace_id,
                    top_k=top_k,
                ),
            )
        except CollectionNotFoundError:
            return []

    async def _embed(self, texts: Sequence[str]) -> _Vectors:
        vectors = await self.embedder.embed(list(texts))
        if len(vectors) != len(texts):
            raise SDKValidationError("Embedder must return one vector per input text.")
        return vectors

    def _record(
        self, namespace: str, hit: VectorSearchResult, *, with_score: bool = True
    ) -> MemoryRecord:
        user_meta = hit.metadata.get(_USER_META) or {}
        return MemoryRecord(
            id=hit.id,
            text=hit.text,
            namespace=namespace,
            metadata=json.loads(json.dumps(user_meta)),
            created_at=float(hit.metadata.get("created_at", 0.0)),
            score=hit.score if with_score else None,
        )
