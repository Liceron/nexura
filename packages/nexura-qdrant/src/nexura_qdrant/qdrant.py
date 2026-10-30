# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Production Qdrant vector store adapter for VectorStore protocol.

Moved from ``nexura_runtime.knowledge.vector.adapters.qdrant`` into ``nexura-qdrant``.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import time
import uuid
from collections.abc import Sequence
from typing import Any

from nexura_agents.contracts.errors import MissingDependencyError

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
    VectorStoreError,
)
from nexura_runtime.knowledge.vector.models import (
    IndexGeneration,
    VectorRecord,
    VectorSearchQuery,
    VectorSearchResult,
    matches_filter,
)


class QdrantVectorStoreAdapter:
    """Production vector store adapter for Qdrant.

    Part of the ``nexura-qdrant`` package, which depends on qdrant-client.
    Satisfies the VectorStore protocol.
    """

    def __init__(
        self,
        url: str = "http://localhost:6333",
        api_key: str | None = None,
        **kwargs: Any,
    ) -> None:
        if importlib.util.find_spec("qdrant_client") is None:
            raise MissingDependencyError(
                "qdrant-client",
                message=(
                    "qdrant-client, a dependency of nexura-qdrant, is not installed. "
                    "Install it with: pip install nexura-qdrant"
                ),
            )
        self.url = url
        self.api_key = api_key
        self._client: Any = None
        self._init_lock: asyncio.Lock = asyncio.Lock()
        self._meta_collection: str = "_nexura_metadata"
        self._initialized: bool = False

    @property
    def is_available(self) -> bool:
        return importlib.util.find_spec("qdrant_client") is not None

    async def _ensure_client(self) -> Any:
        if self._client is None:
            async with self._init_lock:
                if self._client is None:
                    from qdrant_client import AsyncQdrantClient

                    self._client = AsyncQdrantClient(url=self.url, api_key=self.api_key)
        if not self._initialized:
            async with self._init_lock:
                if not self._initialized:
                    if not await self._client.collection_exists(self._meta_collection):
                        await self._client.create_collection(
                            self._meta_collection,
                            vectors_config={},
                        )
                    self._initialized = True
        return self._client

    def _col_point_id(self, workspace_id: str, collection_id: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"col:{workspace_id}:{collection_id}"))

    def _gen_point_id(self, workspace_id: str, collection_id: str, generation_id: str) -> str:
        return str(
            uuid.uuid5(uuid.NAMESPACE_DNS, f"gen:{workspace_id}:{collection_id}:{generation_id}")
        )

    def _record_point_id(
        self, workspace_id: str, collection_id: str, generation_id: str, record_id: str
    ) -> str:
        return str(
            uuid.uuid5(
                uuid.NAMESPACE_DNS,
                f"{workspace_id}:{collection_id}:{generation_id}:{record_id}",
            )
        )

    def _gen_qdrant_collection(
        self,
        workspace_id: str,
        collection_id: str,
        generation_id: str,
    ) -> str:
        clean_ws = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in workspace_id)
        clean_col = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in collection_id)
        clean_gen = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in generation_id)
        name = f"nx_{clean_ws}_{clean_col}_{clean_gen}"
        if len(name) > 120:
            h = hashlib.sha256(
                f"{workspace_id}:{collection_id}:{generation_id}".encode()
            ).hexdigest()[:16]
            name = f"nx_{clean_ws[:32]}_{clean_col[:32]}_{h}"
        return name

    def _to_qdrant_distance(self, metric: str) -> Any:
        from qdrant_client.models import Distance

        mapping = {
            "cosine": Distance.COSINE,
            "euclidean": Distance.EUCLID,
            "dot_product": Distance.DOT,
        }
        if metric in mapping:
            return mapping[metric]
        raise VectorStoreError(f"Unsupported distance metric: '{metric}'.")

    def _to_qdrant_filter(self, filter_dict: dict[str, Any]) -> Any:
        from qdrant_client.models import (
            FieldCondition,
            Filter,
            MatchAny,
            MatchText,
            MatchValue,
            Range,
        )

        must_conditions: list[Any] = [
            FieldCondition(key="_is_deleted", match=MatchValue(value=False))
        ]
        must_not_conditions: list[Any] = []
        should_conditions: list[Any] = []

        def parse_subfilter(sub_dict: dict[str, Any]) -> Filter:
            sub_must: list[Any] = []
            sub_must_not: list[Any] = []
            sub_should: list[Any] = []

            for k, v in sub_dict.items():
                if k == "$and" and isinstance(v, list):
                    for item in v:
                        if isinstance(item, dict):
                            sub_must.append(parse_subfilter(item))
                elif k == "$or" and isinstance(v, list):
                    for item in v:
                        if isinstance(item, dict):
                            sub_should.append(parse_subfilter(item))
                elif isinstance(v, dict):
                    for op, target in v.items():
                        key_path = f"metadata.{k}"
                        if op == "$eq":
                            sub_must.append(
                                FieldCondition(key=key_path, match=MatchValue(value=target))
                            )
                        elif op == "$ne":
                            sub_must_not.append(
                                FieldCondition(key=key_path, match=MatchValue(value=target))
                            )
                        elif op == "$in":
                            sub_must.append(
                                FieldCondition(key=key_path, match=MatchAny(any=list(target)))
                            )
                        elif op == "$nin":
                            sub_must_not.append(
                                FieldCondition(key=key_path, match=MatchAny(any=list(target)))
                            )
                        elif op == "$gt":
                            sub_must.append(
                                FieldCondition(key=key_path, range=Range(gt=float(target)))
                            )
                        elif op == "$gte":
                            sub_must.append(
                                FieldCondition(key=key_path, range=Range(gte=float(target)))
                            )
                        elif op == "$lt":
                            sub_must.append(
                                FieldCondition(key=key_path, range=Range(lt=float(target)))
                            )
                        elif op == "$lte":
                            sub_must.append(
                                FieldCondition(key=key_path, range=Range(lte=float(target)))
                            )
                        elif op == "$contains":
                            if isinstance(target, str):
                                sub_must.append(
                                    FieldCondition(key=key_path, match=MatchText(text=target))
                                )
                            else:
                                sub_must.append(
                                    FieldCondition(key=key_path, match=MatchValue(value=target))
                                )
                else:
                    key_path = f"metadata.{k}"
                    sub_must.append(FieldCondition(key=key_path, match=MatchValue(value=v)))

            return Filter(
                must=sub_must if sub_must else None,
                must_not=sub_must_not if sub_must_not else None,
                should=sub_should if sub_should else None,
            )

        for k, v in filter_dict.items():
            if k == "$and" and isinstance(v, list):
                for item in v:
                    if isinstance(item, dict):
                        must_conditions.append(parse_subfilter(item))
            elif k == "$or" and isinstance(v, list):
                for item in v:
                    if isinstance(item, dict):
                        should_conditions.append(parse_subfilter(item))
            elif isinstance(v, dict):
                for op, target in v.items():
                    key_path = f"metadata.{k}"
                    if op == "$eq":
                        must_conditions.append(
                            FieldCondition(key=key_path, match=MatchValue(value=target))
                        )
                    elif op == "$ne":
                        must_not_conditions.append(
                            FieldCondition(key=key_path, match=MatchValue(value=target))
                        )
                    elif op == "$in":
                        must_conditions.append(
                            FieldCondition(key=key_path, match=MatchAny(any=list(target)))
                        )
                    elif op == "$nin":
                        must_not_conditions.append(
                            FieldCondition(key=key_path, match=MatchAny(any=list(target)))
                        )
                    elif op == "$gt":
                        must_conditions.append(
                            FieldCondition(key=key_path, range=Range(gt=float(target)))
                        )
                    elif op == "$gte":
                        must_conditions.append(
                            FieldCondition(key=key_path, range=Range(gte=float(target)))
                        )
                    elif op == "$lt":
                        must_conditions.append(
                            FieldCondition(key=key_path, range=Range(lt=float(target)))
                        )
                    elif op == "$lte":
                        must_conditions.append(
                            FieldCondition(key=key_path, range=Range(lte=float(target)))
                        )
                    elif op == "$contains":
                        if isinstance(target, str):
                            must_conditions.append(
                                FieldCondition(key=key_path, match=MatchText(text=target))
                            )
                        else:
                            must_conditions.append(
                                FieldCondition(key=key_path, match=MatchValue(value=target))
                            )
            else:
                key_path = f"metadata.{k}"
                must_conditions.append(FieldCondition(key=key_path, match=MatchValue(value=v)))

        return Filter(
            must=must_conditions,
            must_not=must_not_conditions if must_not_conditions else None,
            should=should_conditions if should_conditions else None,
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

        client = await self._ensure_client()
        col_point_id = self._col_point_id(workspace_id, collection_id)
        existing_cols = await client.retrieve(self._meta_collection, ids=[col_point_id])
        if existing_cols:
            active_gen_id = existing_cols[0].payload.get("active_generation_id")
            if active_gen_id:
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

        from qdrant_client.models import PointStruct, VectorParams

        qdrant_col = self._gen_qdrant_collection(workspace_id, collection_id, gen_id)
        dist = self._to_qdrant_distance(metric)
        if not await client.collection_exists(qdrant_col):
            await client.create_collection(
                qdrant_col,
                vectors_config=VectorParams(size=dimensions, distance=dist),
            )

        gen_point_id = self._gen_point_id(workspace_id, collection_id, gen_id)
        await client.upsert(
            self._meta_collection,
            points=[
                PointStruct(
                    id=gen_point_id,
                    vector={},
                    payload={
                        "type": "generation",
                        "workspace_id": workspace_id,
                        "collection_id": collection_id,
                        "generation_id": gen_id,
                        "dimensions": dimensions,
                        "metric": metric,
                        "status": "active",
                        "lineage_json": lin.model_dump_json(),
                        "created_at": now,
                        "activated_at": now,
                    },
                ),
                PointStruct(
                    id=col_point_id,
                    vector={},
                    payload={
                        "type": "collection",
                        "workspace_id": workspace_id,
                        "collection_id": collection_id,
                        "active_generation_id": gen_id,
                        "created_at": now,
                    },
                ),
            ],
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

        client = await self._ensure_client()
        col_point_id = self._col_point_id(workspace_id, collection_id)
        cols = await client.retrieve(self._meta_collection, ids=[col_point_id])
        if not cols:
            raise CollectionNotFoundError(collection_id, workspace_id)

        from qdrant_client.models import (
            FieldCondition,
            Filter,
            MatchValue,
            PointStruct,
            VectorParams,
        )

        count_filt = Filter(
            must=[
                FieldCondition(key="type", match=MatchValue(value="generation")),
                FieldCondition(key="workspace_id", match=MatchValue(value=workspace_id)),
                FieldCondition(key="collection_id", match=MatchValue(value=collection_id)),
            ]
        )
        gen_count_res = await client.count(
            self._meta_collection, count_filter=count_filt, exact=True
        )
        gen_id = f"gen_{gen_count_res.count + 1}_{lineage.lineage_id[:8]}"
        now = time.time()

        qdrant_col = self._gen_qdrant_collection(workspace_id, collection_id, gen_id)
        dist = self._to_qdrant_distance(metric)
        if not await client.collection_exists(qdrant_col):
            await client.create_collection(
                qdrant_col,
                vectors_config=VectorParams(size=dimensions, distance=dist),
            )

        gen_point_id = self._gen_point_id(workspace_id, collection_id, gen_id)
        await client.upsert(
            self._meta_collection,
            points=[
                PointStruct(
                    id=gen_point_id,
                    vector={},
                    payload={
                        "type": "generation",
                        "workspace_id": workspace_id,
                        "collection_id": collection_id,
                        "generation_id": gen_id,
                        "dimensions": dimensions,
                        "metric": metric,
                        "status": "building",
                        "lineage_json": lineage.model_dump_json(),
                        "created_at": now,
                        "activated_at": None,
                    },
                )
            ],
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
        client = await self._ensure_client()
        gen_point_id = self._gen_point_id(workspace_id, collection_id, generation_id)
        gens = await client.retrieve(self._meta_collection, ids=[gen_point_id])
        if not gens:
            raise GenerationNotFoundError(generation_id, collection_id)

        col_point_id = self._col_point_id(workspace_id, collection_id)
        cols = await client.retrieve(self._meta_collection, ids=[col_point_id])
        old_gen_id = cols[0].payload.get("active_generation_id") if cols else None

        now = time.time()
        if old_gen_id and old_gen_id != generation_id:
            old_gen_point_id = self._gen_point_id(workspace_id, collection_id, old_gen_id)
            await client.set_payload(
                self._meta_collection,
                payload={"status": "retired"},
                points=[old_gen_point_id],
            )

        await client.set_payload(
            self._meta_collection,
            payload={"status": "active", "activated_at": now},
            points=[gen_point_id],
        )
        await client.set_payload(
            self._meta_collection,
            payload={"active_generation_id": generation_id},
            points=[col_point_id],
        )

    async def get_active_generation(
        self,
        workspace_id: str,
        collection_id: str,
    ) -> IndexGeneration | None:
        client = await self._ensure_client()
        col_point_id = self._col_point_id(workspace_id, collection_id)
        cols = await client.retrieve(self._meta_collection, ids=[col_point_id])
        if not cols:
            return None
        active_gen_id = cols[0].payload.get("active_generation_id")
        if not active_gen_id:
            return None

        gen_point_id = self._gen_point_id(workspace_id, collection_id, active_gen_id)
        gens = await client.retrieve(self._meta_collection, ids=[gen_point_id])
        if not gens:
            return None

        gen_payload = gens[0].payload
        qdrant_col = self._gen_qdrant_collection(workspace_id, collection_id, active_gen_id)
        record_count = 0
        if await client.collection_exists(qdrant_col):
            from qdrant_client.models import FieldCondition, Filter, MatchValue

            del_filt = Filter(
                must=[FieldCondition(key="_is_deleted", match=MatchValue(value=False))]
            )
            cnt = await client.count(qdrant_col, count_filter=del_filt, exact=True)
            record_count = cnt.count

        lin = IndexLineage.model_validate_json(gen_payload["lineage_json"])
        return IndexGeneration(
            generation_id=active_gen_id,
            collection_id=collection_id,
            workspace_id=workspace_id,
            lineage=lin,
            dimensions=gen_payload["dimensions"],
            metric=gen_payload["metric"],
            status=gen_payload["status"],
            record_count=record_count,
            created_at=gen_payload["created_at"],
            activated_at=gen_payload.get("activated_at"),
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

        client = await self._ensure_client()
        target_gen_id = generation_id
        if not target_gen_id:
            col_point_id = self._col_point_id(workspace_id, collection_id)
            cols = await client.retrieve(self._meta_collection, ids=[col_point_id])
            if not cols:
                raise CollectionNotFoundError(collection_id, workspace_id)
            target_gen_id = cols[0].payload.get("active_generation_id")
            if not target_gen_id:
                raise GenerationNotFoundError("unknown", collection_id)

        gen_point_id = self._gen_point_id(workspace_id, collection_id, target_gen_id)
        gens = await client.retrieve(self._meta_collection, ids=[gen_point_id])
        if not gens:
            raise GenerationNotFoundError(target_gen_id, collection_id)

        gen_payload = gens[0].payload
        dimensions = gen_payload["dimensions"]
        status = gen_payload["status"]
        if status in ("retired", "dropped"):
            raise ImmutableGenerationError(target_gen_id, status)

        validate_upsert_records(records, workspace_id, collection_id, target_gen_id, dimensions)

        qdrant_col = self._gen_qdrant_collection(workspace_id, collection_id, target_gen_id)
        from qdrant_client.models import PointStruct

        now = time.time()
        points: list[PointStruct] = []
        for r in records:
            pt_id = self._record_point_id(workspace_id, collection_id, target_gen_id, r.id)
            points.append(
                PointStruct(
                    id=pt_id,
                    vector=list(r.vector),
                    payload={
                        "_record_id": r.id,
                        "_workspace_id": workspace_id,
                        "_collection_id": collection_id,
                        "_generation_id": target_gen_id,
                        "_document_id": r.document_id,
                        "_text": r.text,
                        "_is_deleted": False,
                        "_created_at": r.created_at,
                        "_updated_at": now,
                        "metadata": dict(r.metadata),
                    },
                )
            )

        await client.upsert(qdrant_col, points=points)
        return len(records)

    async def search(
        self,
        workspace_id: str,
        query: VectorSearchQuery,
    ) -> list[VectorSearchResult]:
        sanitized_filter = sanitize_and_validate_search_query(query, workspace_id)

        client = await self._ensure_client()
        target_gen_id = query.generation_id
        if not target_gen_id:
            col_point_id = self._col_point_id(workspace_id, query.collection_id)
            cols = await client.retrieve(self._meta_collection, ids=[col_point_id])
            if not cols:
                return []
            target_gen_id = cols[0].payload.get("active_generation_id")
            if not target_gen_id:
                return []

        gen_point_id = self._gen_point_id(workspace_id, query.collection_id, target_gen_id)
        gens = await client.retrieve(self._meta_collection, ids=[gen_point_id])
        if not gens:
            return []

        gen_payload = gens[0].payload
        dimensions = gen_payload["dimensions"]
        metric = gen_payload["metric"]

        if len(query.vector) != dimensions:
            raise VectorDimensionMismatchError(
                expected=dimensions,
                actual=len(query.vector),
            )

        qdrant_col = self._gen_qdrant_collection(workspace_id, query.collection_id, target_gen_id)
        if not await client.collection_exists(qdrant_col):
            return []

        q_filter = self._to_qdrant_filter(sanitized_filter)
        res = await client.query_points(
            qdrant_col,
            query=list(query.vector),
            query_filter=q_filter,
            limit=query.top_k,
            with_payload=True,
            with_vectors=query.include_vector,
        )

        results: list[VectorSearchResult] = []
        for hit in res.points:
            payload = hit.payload or {}
            if payload.get("_is_deleted", False):
                continue
            meta = payload.get("metadata", {})
            if not matches_filter(meta, sanitized_filter):
                continue

            raw_score = hit.score
            if metric == "euclidean":
                score = round(1.0 / (1.0 + raw_score), 6)
            else:
                score = round(raw_score, 6)

            vec = list(hit.vector) if (query.include_vector and hit.vector is not None) else None
            results.append(
                VectorSearchResult(
                    id=payload.get("_record_id", str(hit.id)),
                    score=score,
                    document_id=payload.get("_document_id", ""),
                    text=payload.get("_text", ""),
                    metadata=meta if query.include_metadata else {},
                    generation_id=target_gen_id,
                    vector=vec,
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

        client = await self._ensure_client()
        target_gen_id = generation_id
        if not target_gen_id:
            col_point_id = self._col_point_id(workspace_id, collection_id)
            cols = await client.retrieve(self._meta_collection, ids=[col_point_id])
            if not cols:
                return 0
            target_gen_id = cols[0].payload.get("active_generation_id")
            if not target_gen_id:
                return 0

        qdrant_col = self._gen_qdrant_collection(workspace_id, collection_id, target_gen_id)
        if not await client.collection_exists(qdrant_col):
            return 0

        point_ids = [
            self._record_point_id(workspace_id, collection_id, target_gen_id, r_id) for r_id in ids
        ]

        existing = await client.retrieve(qdrant_col, ids=point_ids, with_payload=True)
        if not existing:
            return 0

        if hard_delete:
            delete_ids = [p.id for p in existing]
            await client.delete(qdrant_col, points_selector=delete_ids)
            return len(delete_ids)
        else:
            to_soft_del = [
                p.id for p in existing if not (p.payload or {}).get("_is_deleted", False)
            ]
            if not to_soft_del:
                return 0
            now = time.time()
            await client.set_payload(
                qdrant_col,
                payload={"_is_deleted": True, "_updated_at": now},
                points=to_soft_del,
            )
            return len(to_soft_del)

    async def get(
        self,
        workspace_id: str,
        collection_id: str,
        record_id: str,
        generation_id: str | None = None,
    ) -> VectorRecord | None:
        client = await self._ensure_client()
        target_gen_id = generation_id
        if not target_gen_id:
            col_point_id = self._col_point_id(workspace_id, collection_id)
            cols = await client.retrieve(self._meta_collection, ids=[col_point_id])
            if not cols:
                return None
            target_gen_id = cols[0].payload.get("active_generation_id")
            if not target_gen_id:
                return None

        qdrant_col = self._gen_qdrant_collection(workspace_id, collection_id, target_gen_id)
        if not await client.collection_exists(qdrant_col):
            return None

        point_id = self._record_point_id(workspace_id, collection_id, target_gen_id, record_id)
        pts = await client.retrieve(
            qdrant_col, ids=[point_id], with_payload=True, with_vectors=True
        )
        if not pts:
            return None

        payload = pts[0].payload or {}
        if payload.get("_is_deleted", False):
            return None

        return VectorRecord(
            id=payload.get("_record_id", record_id),
            workspace_id=workspace_id,
            collection_id=collection_id,
            generation_id=target_gen_id,
            vector=[float(x) for x in pts[0].vector] if pts[0].vector is not None else [],
            document_id=payload.get("_document_id", ""),
            text=payload.get("_text", ""),
            metadata=payload.get("metadata", {}),
            is_deleted=bool(payload.get("_is_deleted", False)),
            created_at=payload.get("created_at", time.time()),
            updated_at=payload.get("updated_at", time.time()),
        )

    async def count(
        self,
        workspace_id: str,
        collection_id: str,
        generation_id: str | None = None,
    ) -> int:
        client = await self._ensure_client()
        target_gen_id = generation_id
        if not target_gen_id:
            col_point_id = self._col_point_id(workspace_id, collection_id)
            cols = await client.retrieve(self._meta_collection, ids=[col_point_id])
            if not cols:
                return 0
            target_gen_id = cols[0].payload.get("active_generation_id")
            if not target_gen_id:
                return 0

        qdrant_col = self._gen_qdrant_collection(workspace_id, collection_id, target_gen_id)
        if not await client.collection_exists(qdrant_col):
            return 0

        from qdrant_client.models import FieldCondition, Filter, MatchValue

        del_filt = Filter(must=[FieldCondition(key="_is_deleted", match=MatchValue(value=False))])
        cnt = await client.count(qdrant_col, count_filter=del_filt, exact=True)
        return int(cnt.count)

    async def drop_collection(self, workspace_id: str, collection_id: str) -> None:
        """Drop collection, delete its vector collections in Qdrant, and remove metadata."""
        client = await self._ensure_client()
        col_point_id = self._col_point_id(workspace_id, collection_id)

        from qdrant_client.models import FieldCondition, Filter, MatchValue

        filt = Filter(
            must=[
                FieldCondition(key="type", match=MatchValue(value="generation")),
                FieldCondition(key="workspace_id", match=MatchValue(value=workspace_id)),
                FieldCondition(key="collection_id", match=MatchValue(value=collection_id)),
            ]
        )
        gen_points, _ = await client.scroll(self._meta_collection, scroll_filter=filt, limit=100)
        for gp in gen_points:
            gid = gp.payload.get("generation_id")
            if gid:
                qcol = self._gen_qdrant_collection(workspace_id, collection_id, gid)
                if await client.collection_exists(qcol):
                    await client.delete_collection(qcol)

        gen_ids_to_del = [gp.id for gp in gen_points]
        all_del = [col_point_id, *gen_ids_to_del]
        await client.delete(self._meta_collection, points_selector=all_del)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
            self._initialized = False

    async def close(self) -> None:
        await self.aclose()

    async def __aenter__(self) -> QdrantVectorStoreAdapter:
        await self._ensure_client()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.aclose()
