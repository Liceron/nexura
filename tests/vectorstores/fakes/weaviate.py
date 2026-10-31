# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Weaviate schema/object batches and parsed GraphQL inputs (no query shortcuts)."""

from __future__ import annotations

import re
from typing import Any

import httpx

from .common import FakeAPI, graphql_value, weaviate_match


class WeaviateAPI(FakeAPI):
    def handle(self, request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        path = request.url.path.split("/")
        if path[1:3] == ["v1", "schema"]:
            if len(path) == 3:
                assert request.method == "POST"
                self.schemas[body["class"]] = body
                self.rows[body["class"]] = {}
                return httpx.Response(200, json=body)
            klass = path[3]
            if len(path) == 5:
                assert path[4] == "properties"
                assert not any(p["name"] == body["name"] for p in self.schemas[klass]["properties"])
                self.schemas[klass]["properties"].append(body)
                return httpx.Response(200, json=body)
            return (
                httpx.Response(200, json=self.schemas[klass])
                if klass in self.schemas
                else (httpx.Response(404, json={"error": [{"message": "missing class"}]}))
            )
        if path[1:4] == ["v1", "batch", "objects"]:
            for obj in body["objects"]:
                schema = self.schemas[obj["class"]]
                known = {p["name"] for p in schema["properties"]}
                assert obj["properties"].keys() <= known
                self.rows[obj["class"]][obj["id"]] = {
                    "id": obj["id"],
                    "vector": obj["vector"],
                    **obj["properties"],
                }
            return httpx.Response(
                200,
                json=[{"id": o["id"], "result": {"status": "SUCCESS"}} for o in body["objects"]],
            )
        if path[1:3] == ["v1", "objects"]:
            klass, record_id = path[3:5]
            if request.method == "DELETE":
                self.rows[klass].pop(record_id, None)
                return httpx.Response(204)
            row = self.rows[klass].get(record_id)
            if row is None:
                return httpx.Response(404, json={})
            return httpx.Response(
                200,
                json={
                    "id": row["id"],
                    "class": klass,
                    "vector": row["vector"],
                    "properties": {k: v for k, v in row.items() if k not in ("id", "vector")},
                },
            )
        if path[1:3] == ["v1", "graphql"]:
            query = body["query"]
            assert 'operator:"' not in query, "GraphQL operators are enum literals"
            match = re.match(r"{(Aggregate|Get){(\w+)", query)
            assert match
            operation, klass = match.groups()
            where = None
            if "where:" in query:
                where, _ = graphql_value(query, query.index("where:") + 6)
            if operation == "Aggregate":
                count = sum(
                    where is None or weaviate_match(r, where) for r in self.rows[klass].values()
                )
                return httpx.Response(
                    200,
                    json={
                        "data": {"Aggregate": {klass: [{"meta": {"count": count}}]}},
                    },
                )
            near, _ = graphql_value(query, query.index("nearVector:") + 11)
            limit = re.search(r"limit:(\d+)", query)
            assert limit
            selected = self.ranked(
                klass,
                near["vector"],
                int(limit.group(1)),
                lambda r: where is None or weaviate_match(r, where),
            )
            return httpx.Response(
                200,
                json={
                    "data": {
                        "Get": {
                            klass: [
                                {
                                    "payload": r["payload"],
                                    "deleted": r["deleted"],
                                    "_additional": {"id": r["id"], "distance": 0.0},
                                }
                                for r in selected
                            ]
                        }
                    }
                },
            )
        raise AssertionError((request.method, request.url.path, body))
