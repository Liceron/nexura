# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Chroma 1.x REST v2 adapter (server-side embeddings are never invoked)."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from nexura_runtime.knowledge.vector.errors import VectorStoreError

from .base import HTTPVectorStore, Row
from .filters import native_filter


class ChromaVectorStore(HTTPVectorStore):
    filter_dialect = "chroma"
    database = "Chroma"
    url_env = "NEXURA_CHROMA_URL"
    key_env = "NEXURA_CHROMA_API_KEY"
    default_url = "http://localhost:8000"

    def __init__(
        self,
        url: str | None = None,
        api_key: str | None = None,
        *,
        tenant: str = "default_tenant",
        database: str = "default_database",
        **kwargs: Any,
    ) -> None:
        super().__init__(url, api_key, **kwargs)
        self._prefix = (
            "/api/v2/tenants/"
            + quote(tenant, safe="")
            + "/databases/"
            + quote(database, safe="")
            + "/collections"
        )
        self._collections: dict[str, str] = {}

    def _can_limit_candidates(self, filters: dict[str, Any]) -> bool:
        # Chroma may retain old scalar metadata keys during an upsert merge.
        # Canonical JSON replaces the record; stale projections are only safe
        # necessary conditions, so filtered searches need the complete pool.
        return not filters

    async def _create_space(self, name: str, dimensions: int, metric: str) -> None:
        response = await self._request(
            "POST",
            self._prefix,
            body={
                "name": name,
                "get_or_create": True,
                "metadata": {"nexura_dimensions": dimensions, "nexura_metric": metric},
                "configuration": {
                    "hnsw": {
                        "space": {
                            "cosine": "cosine",
                            "dot_product": "ip",
                            "euclidean": "l2",
                        }[metric]
                    }
                },
            },
        )
        metadata = response.get("metadata") or {}
        if (
            metadata.get("nexura_dimensions") != dimensions
            or metadata.get("nexura_metric") != metric
        ):
            raise VectorStoreError("Chroma collection configuration mismatch.")
        self._collections[name] = response["id"]

    async def _path(self, name: str, operation: str) -> str:
        if name not in self._collections:
            data = await self._request("GET", self._prefix + "/" + quote(name, safe=""))
            self._collections[name] = data["id"]
        return self._prefix + "/" + quote(self._collections[name], safe="") + "/" + operation

    @staticmethod
    def _rows(data: Any) -> list[Row]:
        return [{"id": i, **m} for i, m in zip(data["ids"], data["metadatas"], strict=True)]

    async def _put(self, name: str, rows: list[Row]) -> None:
        for start in range(0, len(rows), 100):
            batch = rows[start : start + 100]
            await self._request(
                "POST",
                await self._path(name, "upsert"),
                body={
                    "ids": [r["id"] for r in batch],
                    "embeddings": [r["vector"] for r in batch],
                    "metadatas": [
                        {k: v for k, v in r.items() if k not in ("id", "vector")} for r in batch
                    ],
                },
            )

    async def _fetch(self, name: str, ids: list[str]) -> list[Row]:
        if not ids:
            return []
        data = await self._request(
            "POST",
            await self._path(name, "get"),
            body={
                "ids": ids,
                "include": ["metadatas"],
            },
        )
        return self._rows(data)

    async def _remove(self, name: str, ids: list[str]) -> None:
        if ids:
            await self._request("POST", await self._path(name, "delete"), body={"ids": ids})

    async def _size(self, name: str) -> int:
        return int(await self._request("GET", await self._path(name, "count")))

    async def _live_size(self, name: str, dimensions: int) -> int:
        count = 0
        while True:
            data = await self._request(
                "POST",
                await self._path(name, "get"),
                body={
                    "where": {"deleted": False},
                    "limit": 1000,
                    "offset": count,
                    "include": [],
                },
            )
            count += len(data["ids"])
            if len(data["ids"]) < 1000:
                return count

    async def _query(
        self,
        name: str,
        vector: list[float],
        limit: int,
        filters: dict[str, Any],
    ) -> list[Row]:
        where = native_filter(filters, "chroma")
        where = {"$and": [{"deleted": False}, where]} if where else {"deleted": False}
        data = await self._request(
            "POST",
            await self._path(name, "query"),
            body={
                "query_embeddings": [vector],
                "n_results": limit,
                "where": where,
                "include": ["metadatas", "distances"],
            },
        )
        return self._rows({"ids": data["ids"][0], "metadatas": data["metadatas"][0]})
