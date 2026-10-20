# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Production PostgreSQL pgvector adapter for VectorStore protocol.

Moved from ``nexura_runtime.knowledge.vector.adapters.pgvector`` into ``nexura-postgres``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import time
from collections.abc import Sequence
from typing import Any

from nexura_agents.contracts.errors import MissingDependencyError

from nexura_postgres.repository import ASYNCPG_MISSING
from nexura_runtime.knowledge.chunks.models import IndexLineage
from nexura_runtime.knowledge.vector.adapters.common import (
    sanitize_and_validate_search_query,
    validate_upsert_records,
)
from nexura_runtime.knowledge.vector.errors import (
    CollectionNotFoundError,
    GenerationNotFoundError,
    ImmutableGenerationError,
    VectorDimensionMismatchError,
)
from nexura_runtime.knowledge.vector.models import (
    IndexGeneration,
    VectorRecord,
    VectorSearchQuery,
    VectorSearchResult,
    matches_filter,
)


class PgVectorStoreAdapter:
    """Production vector store adapter for PostgreSQL with pgvector.

    Part of the ``nexura-postgres`` package, which depends on asyncpg.
    Satisfies the VectorStore protocol.
    """

    def __init__(self, dsn: str = "postgresql://localhost:5432/nexura", **kwargs: Any) -> None:
        if importlib.util.find_spec("asyncpg") is None:
            raise MissingDependencyError("asyncpg", message=ASYNCPG_MISSING)
        self.dsn = dsn
        self._pool: Any = None
        self._init_lock: asyncio.Lock = asyncio.Lock()
        self._initialized: bool = False

    @property
    def is_available(self) -> bool:
        return importlib.util.find_spec("asyncpg") is not None

    async def _ensure_pool(self) -> Any:
        if self._pool is None:
            async with self._init_lock:
                if self._pool is None:
                    import asyncpg  # type: ignore[import-untyped]

                    self._pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=10)
        if not self._initialized:
            async with self._init_lock:
                if not self._initialized:
                    await self._init_db()
                    self._initialized = True
        return self._pool

    async def _init_db(self) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS vector_collections (
                    workspace_id TEXT NOT NULL,
                    collection_id TEXT NOT NULL,
                    active_generation_id TEXT,
                    created_at DOUBLE PRECISION NOT NULL,
                    PRIMARY KEY (workspace_id, collection_id)
                );
                CREATE TABLE IF NOT EXISTS vector_generations (
                    workspace_id TEXT NOT NULL,
                    collection_id TEXT NOT NULL,
                    generation_id TEXT NOT NULL,
                    dimensions INTEGER NOT NULL,
                    metric TEXT NOT NULL,
                    status TEXT NOT NULL,
                    lineage_json TEXT NOT NULL,
                    created_at DOUBLE PRECISION NOT NULL,
                    activated_at DOUBLE PRECISION,
                    PRIMARY KEY (workspace_id, collection_id, generation_id)
                );
                CREATE TABLE IF NOT EXISTS vector_records (
                    workspace_id TEXT NOT NULL,
                    collection_id TEXT NOT NULL,
                    generation_id TEXT NOT NULL,
                    record_id TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    vector_data vector NOT NULL,
                    text TEXT NOT NULL,
                    metadata_json JSONB NOT NULL,
                    is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at DOUBLE PRECISION NOT NULL,
                    updated_at DOUBLE PRECISION NOT NULL,
                    PRIMARY KEY (workspace_id, collection_id, generation_id, record_id)
                );
                CREATE INDEX IF NOT EXISTS idx_vector_records_lookup
                ON vector_records (workspace_id, collection_id, generation_id, is_deleted);
                """
            )

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

        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            existing = await conn.fetchrow(
                """
                SELECT active_generation_id FROM vector_collections
                WHERE workspace_id = $1 AND collection_id = $2
                """,
                workspace_id,
                collection_id,
            )
            if existing and existing["active_generation_id"]:
                active = await self.get_active_generation(workspace_id, collection_id)
                if active:
                    return active

            lin = lineage or IndexLineage(
                chunker_name="default",
                chunker_config_hash="default",
                model_name="default_embedder",
                model_revision="v1",
                dimensions=dimensions,
            )
            gen_id = f"gen_1_{lin.lineage_id[:8]}"
            now = time.time()

            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO vector_collections
                    (workspace_id, collection_id, active_generation_id, created_at)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (workspace_id, collection_id)
                    DO UPDATE SET active_generation_id = EXCLUDED.active_generation_id
                    """,
                    workspace_id,
                    collection_id,
                    gen_id,
                    now,
                )
                await conn.execute(
                    """
                    INSERT INTO vector_generations
                    (workspace_id, collection_id, generation_id, dimensions,
                     metric, status, lineage_json, created_at, activated_at)
                    VALUES ($1, $2, $3, $4, $5, 'active', $6, $7, $8)
                    ON CONFLICT (workspace_id, collection_id, generation_id)
                    DO UPDATE SET status = 'active', activated_at = EXCLUDED.activated_at
                    """,
                    workspace_id,
                    collection_id,
                    gen_id,
                    dimensions,
                    metric,
                    lin.model_dump_json(),
                    now,
                    now,
                )

        return IndexGeneration(
            generation_id=gen_id,
            collection_id=collection_id,
            workspace_id=workspace_id,
            lineage=lin,
            dimensions=dimensions,
            metric=metric,  # type: ignore[arg-type]
            status="active",
            record_count=0,
            created_at=now,
            activated_at=now,
        )

    async def create_generation(
        self,
        workspace_id: str,
        collection_id: str,
        lineage: IndexLineage,
        dimensions: int,
        metric: str = "cosine",
    ) -> IndexGeneration:
        if dimensions <= 0:
            raise VectorDimensionMismatchError(expected=1, actual=dimensions)

        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            col = await conn.fetchrow(
                """
                SELECT 1 FROM vector_collections
                WHERE workspace_id = $1 AND collection_id = $2
                """,
                workspace_id,
                collection_id,
            )
            if not col:
                raise CollectionNotFoundError(collection_id, workspace_id)

            gen_count = await conn.fetchval(
                """
                SELECT COUNT(*) FROM vector_generations
                WHERE workspace_id = $1 AND collection_id = $2
                """,
                workspace_id,
                collection_id,
            )
            gen_id = f"gen_{int(gen_count) + 1}_{lineage.lineage_id[:8]}"
            now = time.time()

            await conn.execute(
                """
                INSERT INTO vector_generations
                (workspace_id, collection_id, generation_id, dimensions,
                 metric, status, lineage_json, created_at, activated_at)
                VALUES ($1, $2, $3, $4, $5, 'building', $6, $7, NULL)
                """,
                workspace_id,
                collection_id,
                gen_id,
                dimensions,
                metric,
                lineage.model_dump_json(),
                now,
            )

        return IndexGeneration(
            generation_id=gen_id,
            collection_id=collection_id,
            workspace_id=workspace_id,
            lineage=lineage,
            dimensions=dimensions,
            metric=metric,  # type: ignore[arg-type]
            status="building",
            record_count=0,
            created_at=now,
            activated_at=None,
        )

    async def activate_generation(
        self,
        workspace_id: str,
        collection_id: str,
        generation_id: str,
    ) -> None:
        pool = await self._ensure_pool()
        now = time.time()
        async with pool.acquire() as conn, conn.transaction():
            gen = await conn.fetchrow(
                """
                    SELECT status FROM vector_generations
                    WHERE workspace_id = $1 AND collection_id = $2 AND generation_id = $3
                    """,
                workspace_id,
                collection_id,
                generation_id,
            )
            if not gen:
                raise GenerationNotFoundError(generation_id, collection_id)

            await conn.execute(
                """
                    UPDATE vector_generations
                    SET status = 'retired'
                    WHERE workspace_id = $1 AND collection_id = $2 AND status = 'active'
                    """,
                workspace_id,
                collection_id,
            )
            await conn.execute(
                """
                    UPDATE vector_generations
                    SET status = 'active', activated_at = $4
                    WHERE workspace_id = $1 AND collection_id = $2 AND generation_id = $3
                    """,
                workspace_id,
                collection_id,
                generation_id,
                now,
            )
            await conn.execute(
                """
                    UPDATE vector_collections
                    SET active_generation_id = $3
                    WHERE workspace_id = $1 AND collection_id = $2
                    """,
                workspace_id,
                collection_id,
                generation_id,
            )

    async def get_active_generation(
        self,
        workspace_id: str,
        collection_id: str,
    ) -> IndexGeneration | None:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT g.generation_id, g.dimensions, g.metric, g.status,
                       g.lineage_json, g.created_at, g.activated_at
                FROM vector_collections c
                JOIN vector_generations g
                  ON c.workspace_id = g.workspace_id
                 AND c.collection_id = g.collection_id
                 AND c.active_generation_id = g.generation_id
                WHERE c.workspace_id = $1 AND c.collection_id = $2
                """,
                workspace_id,
                collection_id,
            )
            if not row:
                return None

            count = await conn.fetchval(
                """
                SELECT COUNT(*) FROM vector_records
                WHERE workspace_id = $1 AND collection_id = $2
                  AND generation_id = $3 AND is_deleted = FALSE
                """,
                workspace_id,
                collection_id,
                row["generation_id"],
            )

        lin = IndexLineage.model_validate_json(row["lineage_json"])
        return IndexGeneration(
            generation_id=row["generation_id"],
            collection_id=collection_id,
            workspace_id=workspace_id,
            lineage=lin,
            dimensions=row["dimensions"],
            metric=row["metric"],
            status=row["status"],
            record_count=int(count),
            created_at=row["created_at"],
            activated_at=row["activated_at"],
        )

    async def upsert(
        self,
        workspace_id: str,
        collection_id: str,
        records: Sequence[VectorRecord],
        generation_id: str | None = None,
    ) -> int:
        if not records:
            return 0

        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            target_gen_id = generation_id
            if not target_gen_id:
                col = await conn.fetchrow(
                    """
                    SELECT active_generation_id FROM vector_collections
                    WHERE workspace_id = $1 AND collection_id = $2
                    """,
                    workspace_id,
                    collection_id,
                )
                if not col:
                    raise CollectionNotFoundError(collection_id, workspace_id)
                target_gen_id = col["active_generation_id"]
                if not target_gen_id:
                    raise GenerationNotFoundError("unknown", collection_id)

            gen = await conn.fetchrow(
                """
                SELECT dimensions, status FROM vector_generations
                WHERE workspace_id = $1 AND collection_id = $2 AND generation_id = $3
                """,
                workspace_id,
                collection_id,
                target_gen_id,
            )
            if not gen:
                raise GenerationNotFoundError(target_gen_id, collection_id)

            dimensions = gen["dimensions"]
            status = gen["status"]
            if status in ("retired", "dropped"):
                raise ImmutableGenerationError(target_gen_id, status)

            validate_upsert_records(records, workspace_id, collection_id, target_gen_id, dimensions)

            now = time.time()
            rows = [
                (
                    workspace_id,
                    collection_id,
                    target_gen_id,
                    r.id,
                    r.document_id,
                    "[" + ",".join(str(x) for x in r.vector) + "]",
                    r.text,
                    json.dumps(r.metadata),
                    now,
                )
                for r in records
            ]

            await conn.executemany(
                """
                INSERT INTO vector_records (
                    workspace_id, collection_id, generation_id, record_id,
                    document_id, vector_data, text, metadata_json,
                    is_deleted, created_at, updated_at
                ) VALUES (
                    $1, $2, $3, $4, $5, $6::vector, $7, $8::jsonb, FALSE, $9, $9
                )
                ON CONFLICT (workspace_id, collection_id, generation_id, record_id)
                DO UPDATE SET
                    document_id = EXCLUDED.document_id,
                    vector_data = EXCLUDED.vector_data,
                    text = EXCLUDED.text,
                    metadata_json = EXCLUDED.metadata_json,
                    is_deleted = FALSE,
                    updated_at = EXCLUDED.updated_at
                """,
                rows,
            )

        return len(records)

    def _compile_filter_sql(self, filter_dict: dict[str, Any], params: list[Any]) -> str:
        clauses: list[str] = []
        for k, v in filter_dict.items():
            if k == "$and":
                sub_clauses = [self._compile_filter_sql(sub, params) for sub in v]
                sub_clauses = [c for c in sub_clauses if c]
                if sub_clauses:
                    clauses.append(f"({' AND '.join(sub_clauses)})")
            elif k == "$or":
                sub_clauses = [self._compile_filter_sql(sub, params) for sub in v]
                sub_clauses = [c for c in sub_clauses if c]
                if sub_clauses:
                    clauses.append(f"({' OR '.join(sub_clauses)})")
            elif isinstance(v, dict):
                for op, target in v.items():
                    if op == "$eq":
                        params.append(k)
                        idx_k = len(params)
                        params.append(
                            str(target).lower() if isinstance(target, bool) else str(target)
                        )
                        idx_v = len(params)
                        clauses.append(f"(metadata_json ->> ${idx_k} = ${idx_v})")
                    elif op == "$ne":
                        params.append(k)
                        idx_k = len(params)
                        params.append(
                            str(target).lower() if isinstance(target, bool) else str(target)
                        )
                        idx_v = len(params)
                        clauses.append(f"(metadata_json ->> ${idx_k} IS DISTINCT FROM ${idx_v})")
                    elif op in ("$gt", "$gte", "$lt", "$lte"):
                        sql_op = {"$gt": ">", "$gte": ">=", "$lt": "<", "$lte": "<="}[op]
                        params.append(k)
                        idx_k = len(params)
                        params.append(float(target))
                        idx_v = len(params)
                        clauses.append(
                            f"(jsonb_typeof(metadata_json -> ${idx_k}) = 'number' "
                            f"AND (metadata_json ->> ${idx_k})::numeric {sql_op} ${idx_v})"
                        )
                    elif op == "$in":
                        params.append(k)
                        idx_k = len(params)
                        params.append(
                            [str(x).lower() if isinstance(x, bool) else str(x) for x in target]
                        )
                        idx_v = len(params)
                        clauses.append(f"(metadata_json ->> ${idx_k} = ANY(${idx_v}::text[]))")
                    elif op == "$nin":
                        params.append(k)
                        idx_k = len(params)
                        params.append(
                            [str(x).lower() if isinstance(x, bool) else str(x) for x in target]
                        )
                        idx_v = len(params)
                        clauses.append(f"(metadata_json ->> ${idx_k} != ALL(${idx_v}::text[]))")
                    elif op == "$contains":
                        params.append(k)
                        idx_k = len(params)
                        params.append(str(target))
                        idx_v = len(params)
                        clauses.append(
                            f"((metadata_json -> ${idx_k} ? ${idx_v}) "
                            f"OR (position(${idx_v} in (metadata_json ->> ${idx_k})) > 0))"
                        )
            else:
                params.append(k)
                idx_k = len(params)
                params.append(str(v).lower() if isinstance(v, bool) else str(v))
                idx_v = len(params)
                clauses.append(f"(metadata_json ->> ${idx_k} = ${idx_v})")

        return " AND ".join(clauses) if clauses else ""

    async def search(
        self,
        workspace_id: str,
        query: VectorSearchQuery,
    ) -> list[VectorSearchResult]:
        sanitized_filter = sanitize_and_validate_search_query(query, workspace_id)

        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            target_gen_id = query.generation_id
            if not target_gen_id:
                col = await conn.fetchrow(
                    """
                    SELECT active_generation_id FROM vector_collections
                    WHERE workspace_id = $1 AND collection_id = $2
                    """,
                    workspace_id,
                    query.collection_id,
                )
                if not col or not col["active_generation_id"]:
                    return []
                target_gen_id = col["active_generation_id"]

            gen = await conn.fetchrow(
                """
                SELECT dimensions, metric FROM vector_generations
                WHERE workspace_id = $1 AND collection_id = $2 AND generation_id = $3
                """,
                workspace_id,
                query.collection_id,
                target_gen_id,
            )
            if not gen:
                return []

            dimensions = gen["dimensions"]
            metric = gen["metric"]

            if len(query.vector) != dimensions:
                raise VectorDimensionMismatchError(
                    expected=dimensions,
                    actual=len(query.vector),
                )

            params: list[Any] = [workspace_id, query.collection_id, target_gen_id]
            filter_clauses = self._compile_filter_sql(sanitized_filter, params)

            vec_str = "[" + ",".join(str(x) for x in query.vector) + "]"
            params.append(vec_str)
            vec_idx = len(params)

            if metric == "cosine":
                score_expr = f"1.0 - (vector_data <=> ${vec_idx}::vector)"
            elif metric == "euclidean":
                score_expr = f"1.0 / (1.0 + (vector_data <-> ${vec_idx}::vector))"
            else:  # dot_product
                score_expr = f"- (vector_data <#> ${vec_idx}::vector)"

            params.append(query.top_k)
            limit_idx = len(params)

            where_sql = (
                "workspace_id = $1 AND collection_id = $2 AND generation_id = $3 "
                "AND is_deleted = FALSE"
            )
            if filter_clauses:
                where_sql += f" AND ({filter_clauses})"

            sql = f"""
                SELECT record_id, document_id, vector_data::text AS vec_txt,
                       text, metadata_json::text AS meta_txt,
                       {score_expr} AS sim_score
                FROM vector_records
                WHERE {where_sql}
                ORDER BY sim_score DESC
                LIMIT ${limit_idx}
            """

            rows = await conn.fetch(sql, *params)

        results: list[VectorSearchResult] = []
        for r in rows:
            meta = json.loads(r["meta_txt"])
            if not matches_filter(meta, sanitized_filter):
                continue
            cand_vec = (
                [float(x) for x in r["vec_txt"].strip("[]").split(",")]
                if query.include_vector
                else None
            )
            score = round(float(r["sim_score"]), 6)
            results.append(
                VectorSearchResult(
                    id=r["record_id"],
                    score=score,
                    document_id=r["document_id"],
                    text=r["text"],
                    metadata=meta if query.include_metadata else {},
                    generation_id=target_gen_id,
                    vector=cand_vec,
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
        if not ids:
            return 0

        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            target_gen_id = generation_id
            if not target_gen_id:
                col = await conn.fetchrow(
                    """
                    SELECT active_generation_id FROM vector_collections
                    WHERE workspace_id = $1 AND collection_id = $2
                    """,
                    workspace_id,
                    collection_id,
                )
                if not col or not col["active_generation_id"]:
                    return 0
                target_gen_id = col["active_generation_id"]

            now = time.time()
            if hard_delete:
                count = await conn.fetchval(
                    """
                    WITH deleted AS (
                        DELETE FROM vector_records
                        WHERE workspace_id = $1 AND collection_id = $2 AND generation_id = $3
                          AND record_id = ANY($4::text[])
                        RETURNING record_id
                    )
                    SELECT COUNT(*) FROM deleted
                    """,
                    workspace_id,
                    collection_id,
                    target_gen_id,
                    list(ids),
                )
            else:
                count = await conn.fetchval(
                    """
                    WITH updated AS (
                        UPDATE vector_records
                        SET is_deleted = TRUE, updated_at = $5
                        WHERE workspace_id = $1 AND collection_id = $2 AND generation_id = $3
                          AND record_id = ANY($4::text[]) AND is_deleted = FALSE
                        RETURNING record_id
                    )
                    SELECT COUNT(*) FROM updated
                    """,
                    workspace_id,
                    collection_id,
                    target_gen_id,
                    list(ids),
                    now,
                )

        return int(count)

    async def get(
        self,
        workspace_id: str,
        collection_id: str,
        record_id: str,
        generation_id: str | None = None,
    ) -> VectorRecord | None:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            target_gen_id = generation_id
            if not target_gen_id:
                col = await conn.fetchrow(
                    """
                    SELECT active_generation_id FROM vector_collections
                    WHERE workspace_id = $1 AND collection_id = $2
                    """,
                    workspace_id,
                    collection_id,
                )
                if not col or not col["active_generation_id"]:
                    return None
                target_gen_id = col["active_generation_id"]

            row = await conn.fetchrow(
                """
                SELECT record_id, document_id, vector_data::text AS vec_txt,
                       text, metadata_json::text AS meta_txt,
                       is_deleted, created_at, updated_at
                FROM vector_records
                WHERE workspace_id = $1 AND collection_id = $2
                  AND generation_id = $3 AND record_id = $4 AND is_deleted = FALSE
                """,
                workspace_id,
                collection_id,
                target_gen_id,
                record_id,
            )
            if not row:
                return None

        vec = [float(x) for x in row["vec_txt"].strip("[]").split(",")]
        return VectorRecord(
            id=row["record_id"],
            workspace_id=workspace_id,
            collection_id=collection_id,
            generation_id=target_gen_id,
            vector=vec,
            document_id=row["document_id"],
            text=row["text"],
            metadata=json.loads(row["meta_txt"]),
            is_deleted=bool(row["is_deleted"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def count(
        self,
        workspace_id: str,
        collection_id: str,
        generation_id: str | None = None,
    ) -> int:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            target_gen_id = generation_id
            if not target_gen_id:
                col = await conn.fetchrow(
                    """
                    SELECT active_generation_id FROM vector_collections
                    WHERE workspace_id = $1 AND collection_id = $2
                    """,
                    workspace_id,
                    collection_id,
                )
                if not col or not col["active_generation_id"]:
                    return 0
                target_gen_id = col["active_generation_id"]

            count = await conn.fetchval(
                """
                SELECT COUNT(*) FROM vector_records
                WHERE workspace_id = $1 AND collection_id = $2
                  AND generation_id = $3 AND is_deleted = FALSE
                """,
                workspace_id,
                collection_id,
                target_gen_id,
            )

        return int(count)

    async def drop_collection(self, workspace_id: str, collection_id: str) -> None:
        """Drop collection and all its generations/records (cleanup helper)."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """
                    DELETE FROM vector_records
                    WHERE workspace_id = $1 AND collection_id = $2
                    """,
                workspace_id,
                collection_id,
            )
            await conn.execute(
                """
                    DELETE FROM vector_generations
                    WHERE workspace_id = $1 AND collection_id = $2
                    """,
                workspace_id,
                collection_id,
            )
            await conn.execute(
                """
                    DELETE FROM vector_collections
                    WHERE workspace_id = $1 AND collection_id = $2
                    """,
                workspace_id,
                collection_id,
            )

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            self._initialized = False

    async def close(self) -> None:
        await self.aclose()

    async def __aenter__(self) -> PgVectorStoreAdapter:
        await self._ensure_pool()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.aclose()
