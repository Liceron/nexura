# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Disposable and persistent SQLite-backed vector store implementation."""

from __future__ import annotations

import json
import sqlite3
import struct
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

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


class SqliteVectorStore:
    """Persistent and disposable SQLite-backed vector store.

    Guarantees:
    - Persistent or in-memory transactional database storage.
    - Mandatory workspace and collection scoping on all queries and mutations.
    - Atomic generation switches and active-index pointer updates.
    - Idempotent upserts and tombstone-based soft deletions.
    - Exact cosine, dot-product, and Euclidean similarity scoring.
    """

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS vector_collections (
                    workspace_id TEXT NOT NULL,
                    collection_id TEXT NOT NULL,
                    active_generation_id TEXT,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (workspace_id, collection_id)
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS vector_generations (
                    workspace_id TEXT NOT NULL,
                    collection_id TEXT NOT NULL,
                    generation_id TEXT NOT NULL,
                    dimensions INTEGER NOT NULL,
                    metric TEXT NOT NULL,
                    status TEXT NOT NULL,
                    lineage_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    activated_at REAL,
                    PRIMARY KEY (workspace_id, collection_id, generation_id)
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS vector_records (
                    workspace_id TEXT NOT NULL,
                    collection_id TEXT NOT NULL,
                    generation_id TEXT NOT NULL,
                    record_id TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    vector_blob BLOB NOT NULL,
                    text TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    is_deleted INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (workspace_id, collection_id, generation_id, record_id)
                )
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_vector_records_lookup
                ON vector_records (workspace_id, collection_id, generation_id, is_deleted)
                """
            )

    def _pack_vector(self, vector: Sequence[float]) -> bytes:
        return struct.pack(f"{len(vector)}f", *vector)

    def _unpack_vector(self, blob: bytes, dimensions: int) -> list[float]:
        return [round(v, 6) for v in struct.unpack(f"{dimensions}f", blob)]

    async def create_collection(
        self,
        workspace_id: str,
        collection_id: str,
        dimensions: int,
        lineage: IndexLineage | None = None,
        metric: str = "cosine",
    ) -> IndexGeneration:
        if dimensions <= 0:
            raise VectorDimensionMismatchError(expected=1, actual=dimensions)

        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT active_generation_id FROM vector_collections
            WHERE workspace_id = ? AND collection_id = ?
            """,
            (workspace_id, collection_id),
        )
        row = cur.fetchone()
        if row and row[0]:
            gen = await self.get_active_generation(workspace_id, collection_id)
            if gen:
                return gen

        lin = lineage or IndexLineage(
            chunker_name="default",
            chunker_config_hash="default",
            model_name="default_embedder",
            model_revision="v1",
            dimensions=dimensions,
        )
        gen_id = f"gen_1_{lin.lineage_id[:8]}"
        now = time.time()

        generation = IndexGeneration(
            generation_id=gen_id,
            collection_id=collection_id,
            workspace_id=workspace_id,
            lineage=lin,
            dimensions=dimensions,
            metric=metric,  # type: ignore[arg-type]
            status="active",
            activated_at=now,
        )

        with self._conn:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO vector_collections
                (workspace_id, collection_id, active_generation_id, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (workspace_id, collection_id, gen_id, now),
            )
            self._conn.execute(
                """
                INSERT OR REPLACE INTO vector_generations
                (workspace_id, collection_id, generation_id, dimensions,
                 metric, status, lineage_json, created_at, activated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    workspace_id,
                    collection_id,
                    gen_id,
                    dimensions,
                    metric,
                    "active",
                    lin.model_dump_json(),
                    now,
                    now,
                ),
            )

        return generation

    async def create_generation(
        self,
        workspace_id: str,
        collection_id: str,
        lineage: IndexLineage,
        dimensions: int,
        metric: str = "cosine",
    ) -> IndexGeneration:
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT COUNT(*) FROM vector_collections
            WHERE workspace_id = ? AND collection_id = ?
            """,
            (workspace_id, collection_id),
        )
        if cur.fetchone()[0] == 0:
            raise CollectionNotFoundError(collection_id, workspace_id)

        if dimensions <= 0:
            raise VectorDimensionMismatchError(expected=1, actual=dimensions)

        cur.execute(
            """
            SELECT COUNT(*) FROM vector_generations
            WHERE workspace_id = ? AND collection_id = ?
            """,
            (workspace_id, collection_id),
        )
        gen_count = cur.fetchone()[0] + 1
        gen_id = f"gen_{gen_count}_{lineage.lineage_id[:8]}"
        now = time.time()

        generation = IndexGeneration(
            generation_id=gen_id,
            collection_id=collection_id,
            workspace_id=workspace_id,
            lineage=lineage,
            dimensions=dimensions,
            metric=metric,  # type: ignore[arg-type]
            status="building",
        )

        with self._conn:
            self._conn.execute(
                """
                INSERT INTO vector_generations
                (workspace_id, collection_id, generation_id, dimensions,
                 metric, status, lineage_json, created_at, activated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    workspace_id,
                    collection_id,
                    gen_id,
                    dimensions,
                    metric,
                    "building",
                    lineage.model_dump_json(),
                    now,
                ),
            )

        return generation

    async def activate_generation(
        self,
        workspace_id: str,
        collection_id: str,
        generation_id: str,
    ) -> None:
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT status FROM vector_generations
            WHERE workspace_id = ? AND collection_id = ? AND generation_id = ?
            """,
            (workspace_id, collection_id, generation_id),
        )
        row = cur.fetchone()
        if not row:
            raise GenerationNotFoundError(generation_id, collection_id)

        now = time.time()
        with self._conn:
            # Retire previously active generation
            self._conn.execute(
                """
                UPDATE vector_generations SET status = 'retired'
                WHERE workspace_id = ? AND collection_id = ? AND status = 'active'
                """,
                (workspace_id, collection_id),
            )
            # Activate target generation
            self._conn.execute(
                """
                UPDATE vector_generations SET status = 'active', activated_at = ?
                WHERE workspace_id = ? AND collection_id = ? AND generation_id = ?
                """,
                (now, workspace_id, collection_id, generation_id),
            )
            # Update collection active pointer
            self._conn.execute(
                """
                UPDATE vector_collections SET active_generation_id = ?
                WHERE workspace_id = ? AND collection_id = ?
                """,
                (generation_id, workspace_id, collection_id),
            )

    async def get_active_generation(
        self,
        workspace_id: str,
        collection_id: str,
    ) -> IndexGeneration | None:
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT g.generation_id, g.dimensions, g.metric, g.status,
                   g.lineage_json, g.created_at, g.activated_at
            FROM vector_collections c
            JOIN vector_generations g
              ON c.workspace_id = g.workspace_id
             AND c.collection_id = g.collection_id
             AND c.active_generation_id = g.generation_id
            WHERE c.workspace_id = ? AND c.collection_id = ?
            """,
            (workspace_id, collection_id),
        )
        row = cur.fetchone()
        if not row:
            return None

        lin = IndexLineage.model_validate_json(row[4])
        cur.execute(
            """
            SELECT COUNT(*) FROM vector_records
            WHERE workspace_id = ? AND collection_id = ?
              AND generation_id = ? AND is_deleted = 0
            """,
            (workspace_id, collection_id, row[0]),
        )
        rec_count = cur.fetchone()[0]

        return IndexGeneration(
            generation_id=row[0],
            collection_id=collection_id,
            workspace_id=workspace_id,
            lineage=lin,
            dimensions=row[1],
            metric=row[2],
            status=row[3],
            record_count=rec_count,
            created_at=row[5],
            activated_at=row[6],
        )

    async def upsert(
        self,
        workspace_id: str,
        collection_id: str,
        records: Sequence[VectorRecord],
        generation_id: str | None = None,
    ) -> int:
        cur = self._conn.cursor()
        target_gen_id = generation_id
        if not target_gen_id:
            cur.execute(
                """
                SELECT active_generation_id FROM vector_collections
                WHERE workspace_id = ? AND collection_id = ?
                """,
                (workspace_id, collection_id),
            )
            row = cur.fetchone()
            if not row:
                raise CollectionNotFoundError(collection_id, workspace_id)
            target_gen_id = row[0]

        cur.execute(
            """
            SELECT dimensions, status FROM vector_generations
            WHERE workspace_id = ? AND collection_id = ? AND generation_id = ?
            """,
            (workspace_id, collection_id, target_gen_id),
        )
        gen_row = cur.fetchone()
        if not gen_row:
            raise GenerationNotFoundError(target_gen_id or "unknown", collection_id)

        dimensions, status = gen_row
        if status in ("retired", "dropped"):
            raise ImmutableGenerationError(target_gen_id, status)

        # Dimension validation
        for r in records:
            if len(r.vector) != dimensions:
                raise VectorDimensionMismatchError(
                    expected=dimensions,
                    actual=len(r.vector),
                )

        now = time.time()
        rows = [
            (
                workspace_id,
                collection_id,
                target_gen_id,
                r.id,
                r.document_id,
                self._pack_vector(r.vector),
                r.text,
                json.dumps(r.metadata),
                0,  # is_deleted
                now,
                now,
            )
            for r in records
        ]

        with self._conn:
            self._conn.executemany(
                """
                INSERT OR REPLACE INTO vector_records
                (workspace_id, collection_id, generation_id, record_id,
                 document_id, vector_blob, text, metadata_json,
                 is_deleted, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

        return len(rows)

    async def search(
        self,
        workspace_id: str,
        query: VectorSearchQuery,
    ) -> list[VectorSearchResult]:
        cur = self._conn.cursor()
        target_gen_id = query.generation_id
        if not target_gen_id:
            cur.execute(
                """
                SELECT active_generation_id FROM vector_collections
                WHERE workspace_id = ? AND collection_id = ?
                """,
                (workspace_id, query.collection_id),
            )
            col_row = cur.fetchone()
            if not col_row or not col_row[0]:
                return []
            target_gen_id = col_row[0]

        cur.execute(
            """
            SELECT dimensions, metric FROM vector_generations
            WHERE workspace_id = ? AND collection_id = ? AND generation_id = ?
            """,
            (workspace_id, query.collection_id, target_gen_id),
        )
        gen_row = cur.fetchone()
        if not gen_row:
            return []

        dimensions, metric = gen_row
        if len(query.vector) != dimensions:
            raise VectorDimensionMismatchError(
                expected=dimensions,
                actual=len(query.vector),
            )

        # Mandatory tenant isolation & filter sanitization
        sanitized_filter = dict(query.filter) if query.filter else {}
        sanitized_filter.pop("workspace_id", None)

        cur.execute(
            """
            SELECT record_id, document_id, vector_blob, text, metadata_json
            FROM vector_records
            WHERE workspace_id = ? AND collection_id = ?
              AND generation_id = ? AND is_deleted = 0
            """,
            (workspace_id, query.collection_id, target_gen_id),
        )
        rows = cur.fetchall()

        candidates: list[tuple[float, str, str, str, dict[str, Any], list[float]]] = []
        for r_id, doc_id, blob, text, meta_json in rows:
            meta = json.loads(meta_json)
            if not matches_filter(meta, sanitized_filter):
                continue
            cand_vec = self._unpack_vector(blob, dimensions)
            score = compute_score(metric, query.vector, cand_vec)
            candidates.append((score, r_id, doc_id, text, meta, cand_vec))

        candidates.sort(key=lambda item: item[0], reverse=True)
        top = candidates[: query.top_k]

        return [
            VectorSearchResult(
                id=r_id,
                score=round(score, 6),
                document_id=doc_id,
                text=text,
                metadata=meta if query.include_metadata else {},
                generation_id=target_gen_id,
                vector=cand_vec if query.include_vector else None,
            )
            for score, r_id, doc_id, text, meta, cand_vec in top
        ]

    async def delete(
        self,
        workspace_id: str,
        collection_id: str,
        ids: Sequence[str],
        hard_delete: bool = False,
        generation_id: str | None = None,
    ) -> int:
        if not ids:
            return 0

        target_gen_id = generation_id
        if not target_gen_id:
            cur = self._conn.cursor()
            cur.execute(
                """
                SELECT active_generation_id FROM vector_collections
                WHERE workspace_id = ? AND collection_id = ?
                """,
                (workspace_id, collection_id),
            )
            row = cur.fetchone()
            if not row or not row[0]:
                return 0
            target_gen_id = row[0]

        placeholders = ",".join("?" for _ in ids)
        now = time.time()
        with self._conn:
            if hard_delete:
                cur = self._conn.execute(
                    f"""
                    DELETE FROM vector_records
                    WHERE workspace_id = ? AND collection_id = ?
                      AND generation_id = ? AND record_id IN ({placeholders})
                    """,
                    (workspace_id, collection_id, target_gen_id, *ids),
                )
            else:
                cur = self._conn.execute(
                    f"""
                    UPDATE vector_records SET is_deleted = 1, updated_at = ?
                    WHERE workspace_id = ? AND collection_id = ?
                      AND generation_id = ? AND record_id IN ({placeholders})
                      AND is_deleted = 0
                    """,
                    (now, workspace_id, collection_id, target_gen_id, *ids),
                )
            return cur.rowcount

    async def get(
        self,
        workspace_id: str,
        collection_id: str,
        record_id: str,
        generation_id: str | None = None,
    ) -> VectorRecord | None:
        target_gen_id = generation_id
        cur = self._conn.cursor()
        if not target_gen_id:
            cur.execute(
                """
                SELECT active_generation_id FROM vector_collections
                WHERE workspace_id = ? AND collection_id = ?
                """,
                (workspace_id, collection_id),
            )
            row = cur.fetchone()
            if not row or not row[0]:
                return None
            target_gen_id = row[0]

        cur.execute(
            """
            SELECT g.dimensions, r.document_id, r.vector_blob, r.text,
                   r.metadata_json, r.is_deleted, r.created_at, r.updated_at
            FROM vector_records r
            JOIN vector_generations g
              ON r.workspace_id = g.workspace_id
             AND r.collection_id = g.collection_id
             AND r.generation_id = g.generation_id
            WHERE r.workspace_id = ? AND r.collection_id = ?
              AND r.generation_id = ? AND r.record_id = ?
            """,
            (workspace_id, collection_id, target_gen_id, record_id),
        )
        row = cur.fetchone()
        if not row or row[5]:  # Not found or is_deleted
            return None

        dims = row[0]
        vec = self._unpack_vector(row[2], dims)
        return VectorRecord(
            id=record_id,
            workspace_id=workspace_id,
            collection_id=collection_id,
            generation_id=target_gen_id,
            vector=vec,
            document_id=row[1],
            text=row[3],
            metadata=json.loads(row[4]),
            is_deleted=bool(row[5]),
            created_at=row[6],
            updated_at=row[7],
        )

    async def count(
        self,
        workspace_id: str,
        collection_id: str,
        generation_id: str | None = None,
    ) -> int:
        cur = self._conn.cursor()
        target_gen_id = generation_id
        if not target_gen_id:
            cur.execute(
                """
                SELECT active_generation_id FROM vector_collections
                WHERE workspace_id = ? AND collection_id = ?
                """,
                (workspace_id, collection_id),
            )
            row = cur.fetchone()
            if not row or not row[0]:
                return 0
            target_gen_id = row[0]

        cur.execute(
            """
            SELECT COUNT(*) FROM vector_records
            WHERE workspace_id = ? AND collection_id = ?
              AND generation_id = ? AND is_deleted = 0
            """,
            (workspace_id, collection_id, target_gen_id),
        )
        return int(cur.fetchone()[0])

    def close(self) -> None:
        self._conn.close()
