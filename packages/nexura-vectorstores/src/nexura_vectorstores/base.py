# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Shared durable lifecycle and HTTP boundary; no vendor clients or local record cache."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import re
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any, Literal, Self, cast
from urllib.parse import urlsplit

import httpx

from nexura_runtime.knowledge.chunks.models import IndexLineage
from nexura_runtime.knowledge.vector.adapters.common import (
    sanitize_and_validate_search_query,
    validate_upsert_records,
)
from nexura_runtime.knowledge.vector.errors import (
    CollectionNotFoundError,
    GenerationNotFoundError,
    ImmutableGenerationError,
    TenantIsolationViolationError,
    VectorDimensionMismatchError,
    VectorStoreError,
)
from nexura_runtime.knowledge.vector.metrics import compute_score
from nexura_runtime.knowledge.vector.models import (
    IndexGeneration,
    VectorRecord,
    VectorSearchQuery,
    VectorSearchResult,
    matches_filter,
)

Row = dict[str, Any]


def space_name(*parts: str) -> str:
    """Length-prefix via JSON to avoid delimiter and punctuation tenant collisions."""
    return "nx" + hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:40]


def point_id(record_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, record_id))


class HTTPVectorStore(ABC):
    """Persist a collection manifest in one atomic database record.

    The manifest contains both the active pointer and every generation's status.
    One record replacement publishes a complete switch, including retirement.
    An instance lock serializes local writers; coordinate writers across processes
    externally (these six APIs have no common compare-and-swap primitive).
    """

    database = "vector database"
    url_env = ""
    key_env = ""
    default_url = ""
    max_candidates = 10000
    filter_dialect = ""

    def __init__(
        self,
        url: str | None = None,
        api_key: str | None = None,
        *,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        endpoint = url or os.getenv(self.url_env) or self.default_url
        parsed = urlsplit(endpoint)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise VectorStoreError("An HTTP(S) database URL is required.")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise VectorStoreError(
                "Database URLs must not contain credentials, queries or fragments."
            )
        if timeout <= 0:
            raise VectorStoreError("HTTP timeout must be positive.")
        if client is not None and transport is not None:
            raise VectorStoreError("Pass either client or transport.")
        self._url = endpoint.rstrip("/")
        self._key = api_key if api_key is not None else os.getenv(self.key_env)
        self._timeout = timeout
        self._client = client or httpx.AsyncClient(
            transport=transport, timeout=timeout, follow_redirects=False, trust_env=False
        )
        self._owns_client = client is None
        self._lock = asyncio.Lock()
        self._spaces: set[str] = set()

    @property
    def is_available(self) -> bool:
        return importlib.util.find_spec("httpx") is not None

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._key}"} if self._key else {}

    def _safe_code(self, code: Any) -> str:
        value = str(code)
        if self._key and self._key in value:
            return "redacted"
        if re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", value):
            return value
        return "unknown"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        params: Any = None,
        missing_ok: bool = False,
        content: str | None = None,
    ) -> Any:
        headers = self._headers()
        if content is not None:
            headers["Content-Type"] = "application/x-ndjson"
        try:
            response = await self._client.request(
                method,
                self._url + path,
                json=body if content is None else None,
                content=content,
                params=params,
                headers=headers,
                timeout=self._timeout,
            )
        except httpx.HTTPError:
            # Never include a URL, request/response body, header, or transport exception.
            raise VectorStoreError(f"{self.database} HTTP transport failed.") from None
        if response.status_code == 404 and missing_ok:
            return None
        try:
            data = response.json() if response.content else {}
        except ValueError:
            data = {}
        error = data.get("error", {}) if isinstance(data, dict) else {}
        code = data.get("code") if isinstance(data, dict) else None
        if code is None and isinstance(error, dict):
            code = error.get("code", error.get("type", "unknown"))
        elif code is None and isinstance(error, str):
            code = error
        if code is None:
            code = "unknown"
        # Milvus uses HTTP 200 with a nonzero application code.
        if not response.is_success or (
            self.database == "Milvus" and code not in (0, "0", "unknown")
        ):
            safe = self._safe_code(code)
            raise VectorStoreError(
                f"{self.database} HTTP {response.status_code} (code {safe}).",
                details={"http_status": response.status_code, "database_code": safe},
            )
        return data

    async def _ensure(self, name: str, dimensions: int, metric: str) -> None:
        if name not in self._spaces:
            await self._create_space(name, dimensions, metric)
            self._spaces.add(name)

    def _meta_dimensions(self) -> int:
        return 1

    def _meta_metric(self) -> str:
        return "cosine"

    async def _manifest(self, workspace: str, collection: str) -> dict[str, Any] | None:
        name = space_name("manifest", workspace, collection)
        await self._ensure(name, self._meta_dimensions(), self._meta_metric())
        rows = await self._fetch(name, [point_id("manifest")])
        if not rows:
            return None
        try:
            manifest: dict[str, Any] = json.loads(rows[0]["payload"])
        except (ValueError, KeyError, TypeError):
            raise VectorStoreError("Invalid database manifest.") from None
        if manifest.get("workspace_id") != workspace or manifest.get("collection_id") != collection:
            raise TenantIsolationViolationError("stored manifest", workspace)
        return manifest

    async def _save_manifest(self, manifest: dict[str, Any]) -> None:
        name = space_name("manifest", manifest["workspace_id"], manifest["collection_id"])
        await self._put(
            name,
            [
                {
                    "id": point_id("manifest"),
                    "vector": [1.0] + [0.0] * (self._meta_dimensions() - 1),
                    "payload": json.dumps(manifest),
                    "deleted": False,
                }
            ],
        )

    @staticmethod
    def _validate_config(dimensions: int, metric: str) -> None:
        if dimensions <= 0:
            raise VectorDimensionMismatchError(expected=1, actual=dimensions)
        if metric not in ("cosine", "dot_product", "euclidean"):
            raise VectorStoreError(
                "Unsupported vector metric; use cosine, dot_product or euclidean."
            )

    async def create_collection(
        self,
        workspace_id: str,
        collection_id: str,
        dimensions: int,
        lineage: IndexLineage | None = None,
        metric: str = "cosine",
    ) -> IndexGeneration:
        self._validate_config(dimensions, metric)
        async with self._lock:
            manifest = await self._manifest(workspace_id, collection_id)
            if manifest:
                active = IndexGeneration.model_validate(manifest["generations"][manifest["active"]])
                active.record_count = await self._live_size(
                    space_name(workspace_id, collection_id, active.generation_id), active.dimensions
                )
                return active
            lin = lineage or IndexLineage(
                chunker_name="default",
                chunker_config_hash="default",
                model_name="default_embedder",
                model_revision="v1",
                dimensions=dimensions,
            )
            generation = IndexGeneration(
                generation_id="gen_1_" + uuid.uuid4().hex,
                collection_id=collection_id,
                workspace_id=workspace_id,
                lineage=lin,
                dimensions=dimensions,
                metric=cast(Literal["cosine", "dot_product", "euclidean"], metric),
                status="active",
                activated_at=time.time(),
            )
            await self._ensure(
                space_name(workspace_id, collection_id, generation.generation_id),
                dimensions,
                metric,
            )
            await self._save_manifest(
                {
                    "workspace_id": workspace_id,
                    "collection_id": collection_id,
                    "active": generation.generation_id,
                    "generations": {generation.generation_id: generation.model_dump(mode="json")},
                }
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
        self._validate_config(dimensions, metric)
        async with self._lock:
            manifest = await self._manifest(workspace_id, collection_id)
            if manifest is None:
                raise CollectionNotFoundError(collection_id, workspace_id)
            generation = IndexGeneration(
                generation_id="gen_" + uuid.uuid4().hex,
                collection_id=collection_id,
                workspace_id=workspace_id,
                lineage=lineage,
                dimensions=dimensions,
                metric=cast(Literal["cosine", "dot_product", "euclidean"], metric),
            )
            await self._ensure(
                space_name(workspace_id, collection_id, generation.generation_id),
                dimensions,
                metric,
            )
            manifest["generations"][generation.generation_id] = generation.model_dump(mode="json")
            await self._save_manifest(manifest)
            return generation

    async def activate_generation(
        self,
        workspace_id: str,
        collection_id: str,
        generation_id: str,
    ) -> None:
        async with self._lock:
            manifest = await self._manifest(workspace_id, collection_id)
            if manifest is None or generation_id not in manifest["generations"]:
                raise GenerationNotFoundError(generation_id, collection_id)
            old = manifest["active"]
            if old != generation_id:
                manifest["generations"][old]["status"] = "retired"
            manifest["generations"][generation_id].update(status="active", activated_at=time.time())
            manifest["active"] = generation_id
            # One document/point replacement is the publication boundary.
            await self._save_manifest(manifest)

    async def _generation(
        self,
        workspace: str,
        collection: str,
        generation: str | None,
        *,
        required: bool = False,
    ) -> IndexGeneration | None:
        manifest = await self._manifest(workspace, collection)
        if manifest is None:
            if required:
                raise CollectionNotFoundError(collection, workspace)
            return None
        target = generation or manifest["active"]
        data = manifest["generations"].get(target)
        if data is None:
            if required:
                raise GenerationNotFoundError(target, collection)
            return None
        return IndexGeneration.model_validate(data)

    async def get_active_generation(
        self,
        workspace_id: str,
        collection_id: str,
    ) -> IndexGeneration | None:
        gen = await self._generation(workspace_id, collection_id, None)
        if gen is not None:
            gen.record_count = await self.count(workspace_id, collection_id, gen.generation_id)
        return gen

    async def upsert(
        self,
        workspace_id: str,
        collection_id: str,
        records: Sequence[VectorRecord],
        generation_id: str | None = None,
    ) -> int:
        if not records:
            return 0
        async with self._lock:
            gen = await self._generation(workspace_id, collection_id, generation_id, required=True)
            assert gen is not None
            # Qdrant permits active writes, including immediately after create_collection.
            if gen.status in ("retired", "dropped"):
                raise ImmutableGenerationError(gen.generation_id, gen.status)
            validate_upsert_records(
                records, workspace_id, collection_id, gen.generation_id, gen.dimensions
            )
            rows = [
                self._encode(
                    r.model_copy(
                        update={
                            "workspace_id": workspace_id,
                            "collection_id": collection_id,
                            "generation_id": gen.generation_id,
                            "is_deleted": False,
                            "updated_at": time.time(),
                        }
                    )
                )
                for r in records
            ]
            await self._put(space_name(workspace_id, collection_id, gen.generation_id), rows)
            return len(records)

    @staticmethod
    def _encode(record: VectorRecord) -> Row:
        from .filters import projections

        return {
            "id": point_id(record.id),
            "vector": record.vector,
            "payload": record.model_dump_json(),
            "deleted": record.is_deleted,
            **projections(record.metadata),
        }

    @staticmethod
    def _decode(row: Row, workspace: str, collection: str, generation: str) -> VectorRecord:
        try:
            record = VectorRecord.model_validate_json(row["payload"])
        except (ValueError, KeyError, TypeError):
            raise VectorStoreError("Invalid database record.") from None
        if record.workspace_id != workspace:
            raise TenantIsolationViolationError("stored record", workspace)
        if record.collection_id != collection or record.generation_id != generation:
            raise VectorStoreError("Stored record scope mismatch.")
        return record

    async def search(
        self,
        workspace_id: str,
        query: VectorSearchQuery,
    ) -> list[VectorSearchResult]:
        filters = sanitize_and_validate_search_query(query, workspace_id)
        gen = await self._generation(workspace_id, query.collection_id, query.generation_id)
        if gen is None or query.top_k <= 0:
            return []
        if len(query.vector) != gen.dimensions:
            raise VectorDimensionMismatchError(expected=gen.dimensions, actual=len(query.vector))
        name = space_name(workspace_id, query.collection_id, gen.generation_id)
        total = await self._size(name)
        if total == 0:
            return []
        limit = min(total, query.top_k) if self._can_limit_candidates(filters) else total
        if limit > self.max_candidates:
            raise VectorStoreError(
                f"{self.database} exact post-filter candidate limit is {self.max_candidates}; "
                "partition the collection before searching."
            )
        # Substring/list $contains, missing fields and JSON values have different
        # vendor semantics. Push down only safe necessary conditions, over-fetch
        # the complete candidate set, then evaluate matches_filter before top_k.
        rows = await self._query(name, query.vector, limit, filters)
        hits: list[VectorSearchResult] = []
        for row in rows:
            record = self._decode(row, workspace_id, query.collection_id, gen.generation_id)
            if record.is_deleted or not matches_filter(record.metadata, filters):
                continue
            # Recompute from stored vectors: normalizes squared L2, cosine distance,
            # IP distance and vendor-specific positive score transforms consistently.
            score = round(compute_score(gen.metric, query.vector, record.vector), 6)
            hits.append(
                VectorSearchResult(
                    id=record.id,
                    score=score,
                    document_id=record.document_id,
                    text=record.text,
                    metadata=record.metadata if query.include_metadata else {},
                    generation_id=gen.generation_id,
                    vector=record.vector if query.include_vector else None,
                )
            )
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[: query.top_k]

    def _can_limit_candidates(self, filters: dict[str, Any]) -> bool:
        from .filters import fully_native

        return fully_native(filters, self.filter_dialect)

    async def get(
        self,
        workspace_id: str,
        collection_id: str,
        record_id: str,
        generation_id: str | None = None,
    ) -> VectorRecord | None:
        gen = await self._generation(workspace_id, collection_id, generation_id)
        if gen is None:
            return None
        rows = await self._fetch(
            space_name(workspace_id, collection_id, gen.generation_id), [point_id(record_id)]
        )
        if not rows:
            return None
        record = self._decode(rows[0], workspace_id, collection_id, gen.generation_id)
        return None if record.is_deleted else record

    async def count(
        self,
        workspace_id: str,
        collection_id: str,
        generation_id: str | None = None,
    ) -> int:
        gen = await self._generation(workspace_id, collection_id, generation_id)
        if gen is None:
            return 0
        return await self._live_size(
            space_name(workspace_id, collection_id, gen.generation_id), gen.dimensions
        )

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
        async with self._lock:
            gen = await self._generation(workspace_id, collection_id, generation_id)
            if gen is None:
                return 0
            name = space_name(workspace_id, collection_id, gen.generation_id)
            unique = list(dict.fromkeys(point_id(i) for i in ids))
            rows: list[Row] = []
            for start in range(0, len(unique), 100):
                rows.extend(await self._fetch(name, unique[start : start + 100]))
            records = [
                self._decode(r, workspace_id, collection_id, gen.generation_id) for r in rows
            ]
            if hard_delete:
                for start in range(0, len(records), 100):
                    await self._remove(name, [point_id(r.id) for r in records[start : start + 100]])
                return len(records)
            live = [r for r in records if not r.is_deleted]
            if live:
                await self._put(
                    name,
                    [
                        self._encode(
                            r.model_copy(
                                update={
                                    "is_deleted": True,
                                    "updated_at": time.time(),
                                }
                            )
                        )
                        for r in live
                    ],
                )
            return len(live)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await self.aclose()

    @abstractmethod
    async def _create_space(self, name: str, dimensions: int, metric: str) -> None: ...

    @abstractmethod
    async def _put(self, name: str, rows: list[Row]) -> None: ...

    @abstractmethod
    async def _fetch(self, name: str, ids: list[str]) -> list[Row]: ...

    @abstractmethod
    async def _remove(self, name: str, ids: list[str]) -> None: ...

    @abstractmethod
    async def _size(self, name: str) -> int: ...

    @abstractmethod
    async def _live_size(self, name: str, dimensions: int) -> int: ...

    @abstractmethod
    async def _query(
        self,
        name: str,
        vector: list[float],
        limit: int,
        filters: dict[str, Any],
    ) -> list[Row]: ...
