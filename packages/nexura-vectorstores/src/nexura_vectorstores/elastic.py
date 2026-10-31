# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Shared Elasticsearch/OpenSearch document, bulk and vector operations."""

from __future__ import annotations

import json
from typing import Any

from nexura_runtime.knowledge.vector.errors import VectorStoreError

from .base import HTTPVectorStore, Row
from .filters import native_filter


class ElasticVectorStoreBase(HTTPVectorStore):
    """The two search engines share their document protocol, but differ in kNN DSL."""

    opensearch = False
    filter_dialect = "elastic"

    def _can_limit_candidates(self, filters: dict[str, Any]) -> bool:
        # OpenSearch nmslib applies outer filters after kNN selection. It needs
        # all candidates to avoid losing hits to tombstones or user predicates.
        return not self.opensearch and super()._can_limit_candidates(filters)

    async def _create_space(self, name: str, dimensions: int, metric: str) -> None:
        exists = await self._request("HEAD", "/" + name, missing_ok=True)
        if exists is not None:
            return
        properties: dict[str, Any] = {
            "payload": {"type": "text", "index": False},
            "deleted": {"type": "boolean"},
        }
        settings: dict[str, Any] = {}
        if self.opensearch:
            settings["index.knn"] = True
            properties["vector"] = {
                "type": "knn_vector",
                "dimension": dimensions,
                "method": {
                    "name": "hnsw",
                    "engine": "nmslib",
                    "space_type": {
                        "cosine": "cosinesimil",
                        "dot_product": "innerproduct",
                        "euclidean": "l2",
                    }[metric],
                },
            }
        else:
            properties["vector"] = {
                "type": "dense_vector",
                "dims": dimensions,
                "index": True,
                "index_options": {"type": "hnsw"},
                # max_inner_product preserves arbitrary vector norms; dot_product
                # requires unit vectors. This setting needs Elasticsearch >=8.11.
                "similarity": {
                    "cosine": "cosine",
                    "dot_product": "max_inner_product",
                    "euclidean": "l2_norm",
                }[metric],
            }
        await self._request(
            "PUT",
            "/" + name,
            body={
                "settings": settings,
                "mappings": {
                    "properties": properties,
                    "dynamic_templates": [
                        {"strings": {"match": "m*s", "mapping": {"type": "keyword"}}},
                        {"numbers": {"match": "m*n", "mapping": {"type": "double"}}},
                    ],
                },
            },
        )

    async def _bulk(self, lines: list[dict[str, Any]]) -> None:
        if not lines:
            return
        result = await self._request(
            "POST",
            "/_bulk",
            params={"refresh": "wait_for"},
            content="".join(json.dumps(line) + "\n" for line in lines),
        )
        if result.get("errors"):
            for item in result.get("items", []):
                action = next(iter(item.values()))
                if action.get("error"):
                    code = self._safe_code(action["error"].get("type", "bulk_error"))
                    status = action.get("status", 500)
                    raise VectorStoreError(
                        f"{self.database} HTTP {status} (code {code}).",
                        details={"http_status": status, "database_code": code},
                    )
            raise VectorStoreError(f"{self.database} HTTP 200 (code bulk_error).")

    async def _put(self, name: str, rows: list[Row]) -> None:
        for start in range(0, len(rows), 100):
            lines: list[dict[str, Any]] = []
            for row in rows[start : start + 100]:
                lines.extend(
                    [
                        {"index": {"_index": name, "_id": row["id"]}},
                        {k: v for k, v in row.items() if k != "id"},
                    ]
                )
            await self._bulk(lines)

    async def _fetch(self, name: str, ids: list[str]) -> list[Row]:
        result: list[Row] = []
        for record_id in ids:
            row = await self._request(
                "GET",
                "/" + name + "/_doc/" + record_id,
                missing_ok=True,
            )
            if row is not None and row.get("found", True):
                result.append({"id": row["_id"], **row["_source"]})
        return result

    async def _remove(self, name: str, ids: list[str]) -> None:
        if not ids:
            return
        if self.opensearch:
            await self._bulk([{"delete": {"_index": name, "_id": record_id}} for record_id in ids])
            return
        result = await self._request(
            "POST",
            "/" + name + "/_delete_by_query",
            params={"refresh": "true"},
            body={"query": {"ids": {"values": ids}}},
        )
        if result.get("failures") or result.get("timed_out"):
            raise VectorStoreError("Elasticsearch HTTP 200 (code delete_by_query_failed).")

    async def _count(self, name: str, live: bool) -> int:
        result = await self._request(
            "POST",
            "/" + name + "/_count",
            body={
                "query": {"term": {"deleted": False}} if live else {"match_all": {}},
            },
        )
        return int(result["count"])

    async def _size(self, name: str) -> int:
        return await self._count(name, False)

    async def _live_size(self, name: str, dimensions: int) -> int:
        return await self._count(name, True)

    async def _query(
        self,
        name: str,
        vector: list[float],
        limit: int,
        filters: dict[str, Any],
    ) -> list[Row]:
        compiled = native_filter(filters, "elastic")
        clauses = [{"term": {"deleted": False}}] + ([compiled] if compiled else [])
        where = {"bool": {"filter": clauses}}
        if self.opensearch:
            # nmslib does not support an efficient inner kNN filter in all 2.x
            # versions. Retrieve the entire bounded candidate set then post-filter.
            body = {
                "size": limit,
                "query": {
                    "bool": {
                        "must": [{"knn": {"vector": {"vector": vector, "k": limit}}}],
                        "filter": clauses,
                    }
                },
            }
        else:
            body = {
                "size": limit,
                "knn": {
                    "field": "vector",
                    "query_vector": vector,
                    "k": limit,
                    "num_candidates": limit,
                    "filter": where,
                },
            }
        result = await self._request("POST", "/" + name + "/_search", body=body)
        return [{"id": h["_id"], **h["_source"]} for h in result["hits"]["hits"]]


class ElasticsearchVectorStore(ElasticVectorStoreBase):
    database = "Elasticsearch"
    url_env = "NEXURA_ELASTICSEARCH_URL"
    key_env = "NEXURA_ELASTICSEARCH_API_KEY"
    default_url = "http://localhost:9200"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"ApiKey {self._key}"} if self._key else {}


class OpenSearchVectorStore(ElasticVectorStoreBase):
    opensearch = True
    database = "OpenSearch"
    url_env = "NEXURA_OPENSEARCH_URL"
    key_env = "NEXURA_OPENSEARCH_TOKEN"
    default_url = "http://localhost:9200"
