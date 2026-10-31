# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Pinecone data plane adapter; each generation occupies a namespace."""

from __future__ import annotations

from typing import Any

from nexura_runtime.knowledge.vector.errors import VectorDimensionMismatchError, VectorStoreError

from .base import HTTPVectorStore, Row
from .filters import native_filter


class PineconeVectorStore(HTTPVectorStore):
    filter_dialect = "pinecone"
    database = "Pinecone"
    url_env = "NEXURA_PINECONE_URL"
    key_env = "NEXURA_PINECONE_API_KEY"

    def __init__(
        self,
        url: str | None = None,
        api_key: str | None = None,
        *,
        dimensions: int = 1536,
        metric: str = "cosine",
        **kwargs: Any,
    ) -> None:
        self._validate_config(dimensions, metric)
        self._dimensions = dimensions
        self._metric = metric
        super().__init__(url, api_key, **kwargs)

    def _headers(self) -> dict[str, str]:
        return {"Api-Key": self._key} if self._key else {}

    def _meta_dimensions(self) -> int:
        return self._dimensions

    def _meta_metric(self) -> str:
        return self._metric

    async def _create_space(self, name: str, dimensions: int, metric: str) -> None:
        if dimensions != self._dimensions:
            raise VectorDimensionMismatchError(self._dimensions, dimensions)
        if metric != self._metric:
            raise VectorStoreError(
                "Pinecone namespaces share the pre-provisioned index metric; "
                "configure a separate index for a different metric."
            )
        stats = await self._request("POST", "/describe_index_stats", body={})
        actual = stats.get("dimension")
        if actual is not None and actual != dimensions:
            raise VectorDimensionMismatchError(dimensions, actual)
        # Namespace creation is implicit in upsert. The data plane does not expose
        # the index metric; the constructor must match the provisioned index.

    async def _put(self, name: str, rows: list[Row]) -> None:
        for start in range(0, len(rows), 100):
            await self._request(
                "POST",
                "/vectors/upsert",
                body={
                    "namespace": name,
                    "vectors": [
                        {
                            "id": r["id"],
                            "values": r["vector"],
                            "metadata": {k: v for k, v in r.items() if k not in ("id", "vector")},
                        }
                        for r in rows[start : start + 100]
                    ],
                },
            )

    async def _fetch(self, name: str, ids: list[str]) -> list[Row]:
        if not ids:
            return []
        data = await self._request(
            "GET",
            "/vectors/fetch",
            params=[("namespace", name)] + [("ids", i) for i in ids],
        )
        return [{"id": i, **r["metadata"]} for i, r in data.get("vectors", {}).items()]

    async def _remove(self, name: str, ids: list[str]) -> None:
        if ids:
            await self._request("POST", "/vectors/delete", body={"namespace": name, "ids": ids})

    async def _size(self, name: str) -> int:
        data = await self._request("POST", "/describe_index_stats", body={})
        return int(data.get("namespaces", {}).get(name, {}).get("vectorCount", 0))

    async def _live_size(self, name: str, dimensions: int) -> int:
        size = await self._size(name)
        if size > self.max_candidates:
            raise VectorStoreError("Pinecone exact count candidate limit is 10000.")
        if not size:
            return 0
        return len(await self._query(name, [1.0] + [0.0] * (dimensions - 1), size, {}))

    async def _query(
        self,
        name: str,
        vector: list[float],
        limit: int,
        filters: dict[str, Any],
    ) -> list[Row]:
        where = native_filter(filters, "pinecone")
        where = (
            {"$and": [{"deleted": {"$eq": False}}, where]} if where else {"deleted": {"$eq": False}}
        )
        data = await self._request(
            "POST",
            "/query",
            body={
                "namespace": name,
                "vector": vector,
                "topK": limit,
                "includeMetadata": True,
                "includeValues": False,
                "filter": where,
            },
        )
        return [{"id": r["id"], **r["metadata"]} for r in data.get("matches", [])]
