# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Milvus/Zilliz RESTful v2 adapter with VARCHAR primary keys and dynamic metadata."""

from __future__ import annotations

import json
from typing import Any

from nexura_runtime.knowledge.vector.errors import VectorStoreError

from .base import HTTPVectorStore, Row
from .filters import native_filter


class MilvusVectorStore(HTTPVectorStore):
    filter_dialect = "milvus"
    database = "Milvus"
    url_env = "NEXURA_MILVUS_URL"
    key_env = "NEXURA_MILVUS_TOKEN"
    default_url = "http://localhost:19530"

    def __init__(
        self,
        url: str | None = None,
        api_key: str | None = None,
        *,
        db_name: str = "default",
        **kwargs: Any,
    ) -> None:
        super().__init__(url, api_key, **kwargs)
        self._db_name = db_name

    async def _call(self, operation: str, name: str, **body: Any) -> Any:
        return await self._request(
            "POST",
            "/v2/vectordb/" + operation,
            body={
                "dbName": self._db_name,
                "collectionName": name,
                **body,
            },
        )

    def _meta_dimensions(self) -> int:
        # Milvus accepts vector fields of 2 to 32768 dimensions only.
        return 2

    async def _create_space(self, name: str, dimensions: int, metric: str) -> None:
        if not 2 <= dimensions <= 32768:
            raise VectorStoreError("Milvus supports 2 to 32768 vector dimensions.")
        # List avoids treating arbitrary application errors as "missing".
        listed = await self._request(
            "POST",
            "/v2/vectordb/collections/list",
            body={
                "dbName": self._db_name,
            },
        )
        metric_name = {"cosine": "COSINE", "dot_product": "IP", "euclidean": "L2"}[metric]
        if name not in listed["data"]:
            await self._call(
                "collections/create",
                name,
                schema={
                    "autoId": False,
                    "enableDynamicField": True,
                    "fields": [
                        {
                            "fieldName": "id",
                            "dataType": "VarChar",
                            "isPrimary": True,
                            "elementTypeParams": {"max_length": "64"},
                        },
                        {
                            "fieldName": "vector",
                            "dataType": "FloatVector",
                            "elementTypeParams": {"dim": str(dimensions)},
                        },
                        {
                            "fieldName": "payload",
                            "dataType": "VarChar",
                            "elementTypeParams": {"max_length": "65535"},
                        },
                        {"fieldName": "deleted", "dataType": "Bool"},
                    ],
                },
                indexParams=[
                    {
                        "fieldName": "vector",
                        "indexName": "vector_idx",
                        "metricType": metric_name,
                        "params": {"index_type": "AUTOINDEX"},
                    }
                ],
                # Milvus 2.4's REST query ignores a per-request consistency level, so the
                # collection default must be Strong for a read to see the write just before it.
                params={"consistencyLevel": "Strong"},
            )
        described = await self._call("collections/describe", name)
        fields = described["data"].get("fields", [])
        for item in fields:
            if item.get("name", item.get("fieldName")) == "vector":
                params = item.get("params", item.get("elementTypeParams", {}))
                if isinstance(params, list):  # Milvus 2.4 describes params as key/value pairs.
                    params = {p.get("key"): p.get("value") for p in params if isinstance(p, dict)}
                dim = params.get("dim")
                if dim is not None and int(dim) != dimensions:
                    raise VectorStoreError("Milvus collection dimension mismatch.")
        # Strong consistency makes acknowledged upserts visible to subsequent queries.

    async def _put(self, name: str, rows: list[Row]) -> None:
        for start in range(0, len(rows), 100):
            await self._call("entities/upsert", name, data=rows[start : start + 100])

    async def _fetch(self, name: str, ids: list[str]) -> list[Row]:
        if not ids:
            return []
        result = await self._call(
            "entities/query",
            name,
            filter="id in " + json.dumps(ids),
            outputFields=["*"],
            limit=len(ids),
            consistencyLevel="Strong",
        )
        return list(result["data"])

    async def _remove(self, name: str, ids: list[str]) -> None:
        if ids:
            await self._call("entities/delete", name, filter="id in " + json.dumps(ids))

    async def _count(self, name: str, expression: str) -> int:
        result = await self._call(
            "entities/query",
            name,
            filter=expression,
            outputFields=["count(*)"],
            consistencyLevel="Strong",
        )
        return int(result["data"][0]["count(*)"])

    async def _size(self, name: str) -> int:
        return await self._count(name, "")

    async def _live_size(self, name: str, dimensions: int) -> int:
        return await self._count(name, "deleted == false")

    async def _query(
        self,
        name: str,
        vector: list[float],
        limit: int,
        filters: dict[str, Any],
    ) -> list[Row]:
        expression = native_filter(filters, "milvus")
        result = await self._call(
            "entities/search",
            name,
            data=[vector],
            annsField="vector",
            limit=limit,
            filter="deleted == false" + (" and (" + expression + ")" if expression else ""),
            outputFields=["*"],
            consistencyLevel="Strong",
        )
        return list(result["data"])
