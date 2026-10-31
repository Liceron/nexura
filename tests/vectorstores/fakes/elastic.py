# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Search-engine wire fakes validate their different vector mapping/query dialects."""

from __future__ import annotations

import json
from typing import Any

import httpx

from .common import FakeAPI, elastic_match


class ElasticsearchAPI(FakeAPI):
    opensearch = False
    bulk_error = False

    def handle(self, request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        parts = request.url.path.strip("/").split("/")
        name = parts[0]
        if request.url.path == "/_bulk":
            assert request.headers["Content-Type"] == "application/x-ndjson"
            assert request.content.endswith(b"\n")
            assert request.url.params["refresh"] == "wait_for"
            lines = [json.loads(line) for line in request.content.splitlines()]
            index = 0
            while index < len(lines):
                action = lines[index]
                index += 1
                if "index" in action:
                    info = action["index"]
                    self.rows[info["_index"]][info["_id"]] = {"id": info["_id"], **lines[index]}
                    index += 1
                else:
                    info = action["delete"]
                    self.rows[info["_index"]].pop(info["_id"], None)
            if self.bulk_error:
                return httpx.Response(
                    200,
                    json={
                        "errors": True,
                        "items": [
                            {
                                "index": {
                                    "status": 400,
                                    "error": {
                                        "type": "mapper_parsing_exception",
                                        "reason": "s3cr3t",
                                    },
                                },
                            }
                        ],
                    },
                )
            return httpx.Response(200, json={"errors": False, "items": []})
        if len(parts) == 1:
            if request.method == "HEAD":
                return httpx.Response(200 if name in self.rows else 404)
            assert request.method == "PUT"
            mapping = body["mappings"]["properties"]["vector"]
            if self.opensearch:
                assert mapping["type"] == "knn_vector"
                assert mapping["method"]["space_type"] in ("l2", "cosinesimil", "innerproduct")
                assert body["settings"]["index.knn"] is True
            else:
                assert mapping["type"] == "dense_vector"
                assert mapping["similarity"] in ("l2_norm", "cosine", "max_inner_product")
            self.schemas[name] = body
            self.rows[name] = {}
            return httpx.Response(200, json={"acknowledged": True})
        if parts[1] == "_doc":
            row = self.rows[name].get(parts[2])
            if row is None:
                return httpx.Response(404, json={"found": False})
            return httpx.Response(
                200,
                json={
                    "_id": parts[2],
                    "_source": {k: v for k, v in row.items() if k != "id"},
                    "found": True,
                },
            )
        if parts[1] == "_count":
            return httpx.Response(
                200,
                json={
                    "count": sum(elastic_match(r, body["query"]) for r in self.rows[name].values())
                },
            )
        if parts[1] == "_delete_by_query":
            assert not self.opensearch
            assert request.url.params["refresh"] == "true"
            removed = 0
            for record_id in body["query"]["ids"]["values"]:
                removed += self.rows[name].pop(record_id, None) is not None
            return httpx.Response(
                200,
                json={
                    "deleted": removed,
                    "failures": [],
                    "timed_out": False,
                },
            )
        if parts[1] == "_search":
            if self.opensearch:
                query = body["query"]
                if "bool" in query:
                    knn = query["bool"]["must"][0]["knn"]["vector"]
                    where = {"bool": {"filter": query["bool"].get("filter", [])}}
                else:
                    knn = query["knn"]["vector"]
                    where = {"match_all": {}}
                vector, limit = knn["vector"], knn["k"]
            else:
                knn = body["knn"]
                vector, limit = knn["query_vector"], knn["k"]
                assert knn["num_candidates"] >= limit
                where = knn["filter"]
            selected = self.ranked(name, vector, limit, lambda r: elastic_match(r, where))
            return httpx.Response(
                200,
                json={
                    "hits": {
                        "hits": [
                            {
                                "_id": r["id"],
                                "_score": 1.0,
                                "_source": {k: v for k, v in r.items() if k != "id"},
                            }
                            for r in selected
                        ]
                    }
                },
            )
        raise AssertionError((request.method, request.url.path, body))


class OpenSearchAPI(ElasticsearchAPI):
    opensearch = True
