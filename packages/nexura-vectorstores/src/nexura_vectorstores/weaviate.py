# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Weaviate REST object/schema and GraphQL nearVector adapter."""

from __future__ import annotations

import json
from typing import Any

from nexura_runtime.knowledge.vector.errors import VectorStoreError

from .base import HTTPVectorStore, Row
from .filters import native_filter


def graphql_input(value: Any) -> str:
    """Only generated property names become GraphQL identifiers; values stay JSON."""
    if isinstance(value, dict):
        # GraphQL enums must be bare identifiers in query text, unlike JSON
        # variables. These operators come solely from our compiler.
        return (
            "{"
            + ",".join(
                k
                + ":"
                + (
                    v
                    if k == "operator"
                    and v
                    in {
                        "And",
                        "Or",
                        "Equal",
                        "GreaterThan",
                        "GreaterThanEqual",
                        "LessThan",
                        "LessThanEqual",
                    }
                    else graphql_input(v)
                )
                for k, v in value.items()
            )
            + "}"
        )
    if isinstance(value, list):
        return "[" + ",".join(graphql_input(v) for v in value) + "]"
    return json.dumps(value)


class WeaviateVectorStore(HTTPVectorStore):
    filter_dialect = "weaviate"
    database = "Weaviate"
    url_env = "NEXURA_WEAVIATE_URL"
    key_env = "NEXURA_WEAVIATE_API_KEY"
    default_url = "http://localhost:8080"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._properties: dict[str, set[str]] = {}

    def _can_limit_candidates(self, filters: dict[str, Any]) -> bool:
        # Missing schema fields can force _query to drop its native expression.
        # Keep the full fallback pool whenever there is a user filter.
        return not filters

    @staticmethod
    def _class(name: str) -> str:
        return name[0].upper() + name[1:]

    async def _create_space(self, name: str, dimensions: int, metric: str) -> None:
        klass = self._class(name)
        schema = await self._request("GET", "/v1/schema/" + klass, missing_ok=True)
        distance = {"cosine": "cosine", "dot_product": "dot", "euclidean": "l2-squared"}[metric]
        if schema is None:
            schema = {
                "class": klass,
                "vectorizer": "none",
                "vectorIndexConfig": {"distance": distance},
                "properties": [
                    {
                        "name": "payload",
                        "dataType": ["text"],
                        "indexFilterable": False,
                        "indexSearchable": False,
                    },
                    {"name": "deleted", "dataType": ["boolean"]},
                ],
            }
            await self._request("POST", "/v1/schema", body=schema)
        if schema.get("vectorIndexConfig", {}).get("distance", "cosine") != distance:
            raise VectorStoreError("Weaviate collection metric mismatch.")
        self._properties[name] = {p["name"] for p in schema["properties"]}

    async def _put(self, name: str, rows: list[Row]) -> None:
        klass = self._class(name)
        if name not in self._properties:
            schema = await self._request("GET", "/v1/schema/" + klass)
            self._properties[name] = {p["name"] for p in schema["properties"]}
        # Explicit fields avoid Weaviate inferring integer/text tokenization wrongly.
        for row in rows:
            for key in row:
                if key.startswith("m") and key not in self._properties[name]:
                    prop: dict[str, Any] = {
                        "name": key,
                        "dataType": ["text" if key.endswith("s") else "number"],
                    }
                    if key.endswith("s"):
                        prop["tokenization"] = "field"
                        prop["indexSearchable"] = False
                    await self._request("POST", "/v1/schema/" + klass + "/properties", body=prop)
                    self._properties[name].add(key)
        for start in range(0, len(rows), 100):
            result = await self._request(
                "POST",
                "/v1/batch/objects",
                body={
                    "objects": [
                        {
                            "class": klass,
                            "id": r["id"],
                            "vector": r["vector"],
                            "properties": {k: v for k, v in r.items() if k not in ("id", "vector")},
                        }
                        for r in rows[start : start + 100]
                    ]
                },
            )
            if any(item.get("result", {}).get("errors") for item in result):
                raise VectorStoreError("Weaviate HTTP 200 (code batch_error).")

    async def _fetch(self, name: str, ids: list[str]) -> list[Row]:
        result: list[Row] = []
        for record_id in ids:
            row = await self._request(
                "GET",
                "/v1/objects/" + self._class(name) + "/" + record_id,
                missing_ok=True,
            )
            if row is not None:
                result.append({"id": row["id"], **row["properties"]})
        return result

    async def _remove(self, name: str, ids: list[str]) -> None:
        for record_id in ids:
            await self._request(
                "DELETE",
                "/v1/objects/" + self._class(name) + "/" + record_id,
                missing_ok=True,
            )

    async def _graphql(self, query: str) -> Any:
        result = await self._request("POST", "/v1/graphql", body={"query": query})
        if result.get("errors"):
            # GraphQL messages can echo query values; never forward them.
            raise VectorStoreError("Weaviate HTTP 200 (code graphql_error).")
        return result["data"]

    async def _count(self, name: str, live: bool) -> int:
        where = '(where:{path:["deleted"],operator:Equal,valueBoolean:false})' if live else ""
        klass = self._class(name)
        data = await self._graphql("{Aggregate{" + klass + where + "{meta{count}}}}")
        return int(data["Aggregate"][klass][0]["meta"]["count"])

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
        where = native_filter(filters, "weaviate")
        live = {"path": ["deleted"], "operator": "Equal", "valueBoolean": False}
        where = {"operator": "And", "operands": [live, where]} if where else live
        # A queried field not yet present in the schema cannot match a positive
        # condition. Avoid GraphQL schema errors by falling back to post-filtering.
        schema = await self._request("GET", "/v1/schema/" + self._class(name))
        known = {p["name"] for p in schema["properties"]}

        def missing(value: Any) -> bool:
            if isinstance(value, dict):
                return ("path" in value and value["path"][0] not in known) or any(
                    missing(v) for v in value.values()
                )
            return isinstance(value, list) and any(missing(v) for v in value)

        if missing(where):
            where = live
        klass = self._class(name)
        args = (
            "nearVector:{vector:"
            + json.dumps(vector)
            + "},limit:"
            + str(limit)
            + ",where:"
            + graphql_input(where)
        )
        data = await self._graphql(
            "{Get{" + klass + "(" + args + "){payload deleted _additional{id distance}}}}"
        )
        return [
            {"id": r["_additional"]["id"], "payload": r["payload"], "deleted": r["deleted"]}
            for r in data["Get"][klass]
        ]
