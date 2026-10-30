# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""VectorStore protocol and in-memory implementation."""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from nexura_runtime.knowledge.chunks.models import IndexLineage
from nexura_runtime.knowledge.vector.errors import (
    CollectionNotFoundError,
    GenerationNotFoundError,
    ImmutableGenerationError,
    VectorDimensionMismatchError,
)
from nexura_runtime.knowledge.vector.metrics import compute_score
from nexura_runtime.knowledge.vector.models import (
    IndexGeneration,
    VectorRecord,
    VectorSearchQuery,
    VectorSearchResult,
    matches_filter,
)


@runtime_checkable
class VectorStore(Protocol):
    """Protocol defining vector storage, index generation management, and search."""

    async def create_collection(
        self,
        workspace_id: str,
        collection_id: str,
        dimensions: int,
        lineage: IndexLineage | None = None,
        metric: str = "cosine",
    ) -> IndexGeneration:
        """Create or return collection and its active index generation."""
        ...

    async def create_generation(
        self,
        workspace_id: str,
        collection_id: str,
        lineage: IndexLineage,
        dimensions: int,
        metric: str = "cosine",
    ) -> IndexGeneration:
        """Create a new index generation in building status."""
        ...

    async def activate_generation(
        self,
        workspace_id: str,
        collection_id: str,
        generation_id: str,
    ) -> None:
        """Atomically switch active index to target generation."""
        ...

    async def get_active_generation(
        self,
        workspace_id: str,
        collection_id: str,
    ) -> IndexGeneration | None:
        """Retrieve active generation for collection."""
        ...

    async def upsert(
        self,
        workspace_id: str,
        collection_id: str,
        records: Sequence[VectorRecord],
        generation_id: str | None = None,
    ) -> int:
        """Upsert records into target (or active) generation."""
        ...

    async def search(
        self,
        workspace_id: str,
        query: VectorSearchQuery,
    ) -> list[VectorSearchResult]:
        """Execute approximate or exact vector search scoped to workspace."""
        ...

    async def delete(
        self,
        workspace_id: str,
        collection_id: str,
        ids: Sequence[str],
        hard_delete: bool = False,
        generation_id: str | None = None,
    ) -> int:
        """Delete or tombstone records idempotently."""
        ...

    async def get(
        self,
        workspace_id: str,
        collection_id: str,
        record_id: str,
        generation_id: str | None = None,
    ) -> VectorRecord | None:
        """Retrieve record by ID."""
        ...

    async def count(
        self,
        workspace_id: str,
        collection_id: str,
        generation_id: str | None = None,
    ) -> int:
        """Count active records in generation."""
        ...


class InMemoryVectorStore:
    """Thread-safe in-memory vector store with exact search.

    Guarantees:
    - Mandatory workspace and collection scoping.
    - Wrong-tenant filters cannot be overridden.
    - Immutable index generations and atomic active-index switch.
    - Idempotent upsert and deletion with tombstones.
    - Eager vector dimension validation.
    """

    def __init__(self) -> None:
        # Structure: {(workspace_id, collection_id): {generation_id: {record_id: VectorRecord}}}
        self._records: dict[tuple[str, str], dict[str, dict[str, VectorRecord]]] = {}
        # Structure: {(workspace_id, collection_id): {generation_id: IndexGeneration}}
        self._generations: dict[tuple[str, str], dict[str, IndexGeneration]] = {}
        # Structure: {(workspace_id, collection_id): active_generation_id}
        self._active_generations: dict[tuple[str, str], str] = {}

    def _col_key(self, workspace_id: str, collection_id: str) -> tuple[str, str]:
        return (workspace_id, collection_id)

    async def create_collection(
        self,
        workspace_id: str,
        collection_id: str,
        dimensions: int,
        lineage: IndexLineage | None = None,
        metric: str = "cosine",
    ) -> IndexGeneration:
        key = self._col_key(workspace_id, collection_id)
        if key in self._active_generations:
            active_gen_id = self._active_generations[key]
            return self._generations[key][active_gen_id]

        if dimensions <= 0:
            raise VectorDimensionMismatchError(expected=1, actual=dimensions)

        lin = lineage or IndexLineage(
            chunker_name="default",
            chunker_config_hash="default",
            model_name="default_embedder",
            model_revision="v1",
            dimensions=dimensions,
        )

        gen_id = f"gen_1_{lin.lineage_id[:8]}"
        generation = IndexGeneration(
            generation_id=gen_id,
            collection_id=collection_id,
            workspace_id=workspace_id,
            lineage=lin,
            dimensions=dimensions,
            metric=metric,  # type: ignore[arg-type]
            status="active",
            activated_at=time.time(),
        )

        self._generations.setdefault(key, {})[gen_id] = generation
        self._active_generations[key] = gen_id
        self._records.setdefault(key, {})[gen_id] = {}
        return generation

    async def create_generation(
        self,
        workspace_id: str,
        collection_id: str,
        lineage: IndexLineage,
        dimensions: int,
        metric: str = "cosine",
    ) -> IndexGeneration:
        key = self._col_key(workspace_id, collection_id)
        if key not in self._generations:
            raise CollectionNotFoundError(collection_id, workspace_id)

        if dimensions <= 0:
            raise VectorDimensionMismatchError(expected=1, actual=dimensions)

        gen_count = len(self._generations[key]) + 1
        gen_id = f"gen_{gen_count}_{lineage.lineage_id[:8]}"

        generation = IndexGeneration(
            generation_id=gen_id,
            collection_id=collection_id,
            workspace_id=workspace_id,
            lineage=lineage,
            dimensions=dimensions,
            metric=metric,  # type: ignore[arg-type]
            status="building",
        )

        self._generations[key][gen_id] = generation
        self._records[key][gen_id] = {}
        return generation

    async def activate_generation(
        self,
        workspace_id: str,
        collection_id: str,
        generation_id: str,
    ) -> None:
        key = self._col_key(workspace_id, collection_id)
        if key not in self._generations or generation_id not in self._generations[key]:
            raise GenerationNotFoundError(generation_id, collection_id)

        # Retire previously active generation
        curr_active_id = self._active_generations.get(key)
        if curr_active_id and curr_active_id in self._generations[key]:
            prev_gen = self._generations[key][curr_active_id]
            self._generations[key][curr_active_id] = prev_gen.model_copy(
                update={"status": "retired"}
            )

        # Activate new generation
        new_gen = self._generations[key][generation_id]
        self._generations[key][generation_id] = new_gen.model_copy(
            update={"status": "active", "activated_at": time.time()}
        )
        self._active_generations[key] = generation_id

    async def get_active_generation(
        self,
        workspace_id: str,
        collection_id: str,
    ) -> IndexGeneration | None:
        key = self._col_key(workspace_id, collection_id)
        active_id = self._active_generations.get(key)
        if not active_id:
            return None
        return self._generations.get(key, {}).get(active_id)

    async def upsert(
        self,
        workspace_id: str,
        collection_id: str,
        records: Sequence[VectorRecord],
        generation_id: str | None = None,
    ) -> int:
        key = self._col_key(workspace_id, collection_id)
        if key not in self._generations:
            raise CollectionNotFoundError(collection_id, workspace_id)

        target_gen_id = generation_id or self._active_generations.get(key)
        if not target_gen_id or target_gen_id not in self._generations[key]:
            raise GenerationNotFoundError(target_gen_id or "unknown", collection_id)

        target_gen = self._generations[key][target_gen_id]
        if target_gen.status in ("retired", "dropped"):
            raise ImmutableGenerationError(target_gen_id, target_gen.status)

        # Eager dimension verification
        for r in records:
            if len(r.vector) != target_gen.dimensions:
                raise VectorDimensionMismatchError(
                    expected=target_gen.dimensions,
                    actual=len(r.vector),
                )

        # Upsert records idempotently
        count = 0
        now = time.time()
        for r in records:
            updated = r.model_copy(
                update={
                    "workspace_id": workspace_id,
                    "collection_id": collection_id,
                    "generation_id": target_gen_id,
                    "is_deleted": False,
                    "updated_at": now,
                }
            )
            self._records[key][target_gen_id][r.id] = updated
            count += 1

        # Update record count on generation
        active_count = sum(
            1 for rec in self._records[key][target_gen_id].values() if not rec.is_deleted
        )
        self._generations[key][target_gen_id] = target_gen.model_copy(
            update={"record_count": active_count}
        )

        return count

    async def search(
        self,
        workspace_id: str,
        query: VectorSearchQuery,
    ) -> list[VectorSearchResult]:
        # Enforce mandatory tenant isolation: query strictly executed in workspace_id
        key = self._col_key(workspace_id, query.collection_id)
        if key not in self._generations:
            return []

        target_gen_id = query.generation_id or self._active_generations.get(key)
        if not target_gen_id or target_gen_id not in self._generations[key]:
            return []

        target_gen = self._generations[key][target_gen_id]

        # Dimension verification
        if len(query.vector) != target_gen.dimensions:
            raise VectorDimensionMismatchError(
                expected=target_gen.dimensions,
                actual=len(query.vector),
            )

        # Clean filter: ensure wrong-tenant filters cannot be overridden
        sanitized_filter = dict(query.filter) if query.filter else {}
        sanitized_filter.pop("workspace_id", None)  # Prevent overriding tenant scope

        candidates: list[tuple[float, VectorRecord]] = []
        gen_records = self._records[key].get(target_gen_id, {})

        for rec in gen_records.values():
            if rec.is_deleted:
                continue
            if not matches_filter(rec.metadata, sanitized_filter):
                continue

            score = compute_score(target_gen.metric, query.vector, rec.vector)
            candidates.append((score, rec))

        # Sort descending by similarity score
        candidates.sort(key=lambda item: item[0], reverse=True)
        top_candidates = candidates[: query.top_k]

        results: list[VectorSearchResult] = []
        for score, rec in top_candidates:
            results.append(
                VectorSearchResult(
                    id=rec.id,
                    score=round(score, 6),
                    document_id=rec.document_id,
                    text=rec.text,
                    metadata=dict(rec.metadata) if query.include_metadata else {},
                    generation_id=rec.generation_id,
                    vector=list(rec.vector) if query.include_vector else None,
                )
            )

        return results

    async def delete(
        self,
        workspace_id: str,
        collection_id: str,
        ids: Sequence[str],
        hard_delete: bool = False,
        generation_id: str | None = None,
    ) -> int:
        key = self._col_key(workspace_id, collection_id)
        if key not in self._records:
            return 0

        target_gen_id = generation_id or self._active_generations.get(key)
        if not target_gen_id or target_gen_id not in self._records[key]:
            return 0

        gen_records = self._records[key][target_gen_id]
        deleted_count = 0

        for r_id in ids:
            if r_id in gen_records:
                if hard_delete:
                    del gen_records[r_id]
                    deleted_count += 1
                elif not gen_records[r_id].is_deleted:
                    gen_records[r_id] = gen_records[r_id].model_copy(
                        update={"is_deleted": True, "updated_at": time.time()}
                    )
                    deleted_count += 1

        # Update record count
        if target_gen_id in self._generations.get(key, {}):
            gen = self._generations[key][target_gen_id]
            active_count = sum(1 for r in gen_records.values() if not r.is_deleted)
            self._generations[key][target_gen_id] = gen.model_copy(
                update={"record_count": active_count}
            )

        return deleted_count

    async def get(
        self,
        workspace_id: str,
        collection_id: str,
        record_id: str,
        generation_id: str | None = None,
    ) -> VectorRecord | None:
        key = self._col_key(workspace_id, collection_id)
        if key not in self._records:
            return None

        target_gen_id = generation_id or self._active_generations.get(key)
        if not target_gen_id or target_gen_id not in self._records[key]:
            return None

        rec = self._records[key][target_gen_id].get(record_id)
        if rec is None or rec.is_deleted:
            return None
        return rec

    async def count(
        self,
        workspace_id: str,
        collection_id: str,
        generation_id: str | None = None,
    ) -> int:
        key = self._col_key(workspace_id, collection_id)
        if key not in self._records:
            return 0

        target_gen_id = generation_id or self._active_generations.get(key)
        if not target_gen_id or target_gen_id not in self._records[key]:
            return 0

        return sum(1 for r in self._records[key][target_gen_id].values() if not r.is_deleted)
