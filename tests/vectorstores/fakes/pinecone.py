# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Pinecone namespaces, repeated fetch ids, and data-plane camelCase responses."""

from __future__ import annotations

from typing import Any

import httpx

from .common import FakeAPI, mongo_match


class PineconeAPI(FakeAPI):
    def handle(self, request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        path = request.url.path
        assert request.headers["Api-Key"] == "s3cr3t"
        if path == "/describe_index_stats":
            return httpx.Response(
                200,
                json={
                    "dimension": 2,
                    "namespaces": {
                        name: {"vectorCount": len(rows)} for name, rows in self.rows.items()
                    },
                },
            )
        namespace = body.get("namespace", request.url.params.get("namespace"))
        rows = self.rows.setdefault(namespace, {})
        if path == "/vectors/upsert":
            for row in body["vectors"]:
                assert len(row["values"]) == 2
                rows[row["id"]] = {
                    "id": row["id"],
                    "vector": row["values"],
                    **row["metadata"],
                }
            return httpx.Response(200, json={"upsertedCount": len(body["vectors"])})
        if path == "/vectors/fetch":
            assert request.method == "GET"
            return httpx.Response(
                200,
                json={
                    "vectors": {
                        i: {
                            "id": i,
                            "values": rows[i]["vector"],
                            "metadata": {
                                k: v for k, v in rows[i].items() if k not in ("id", "vector")
                            },
                        }
                        for i in request.url.params.get_list("ids")
                        if i in rows
                    },
                    "namespace": namespace,
                },
            )
        if path == "/vectors/delete":
            for i in body["ids"]:
                rows.pop(i, None)
            return httpx.Response(200, json={})
        if path == "/query":
            assert 1 <= body["topK"] <= 10000
            selected = self.ranked(
                namespace,
                body["vector"],
                body["topK"],
                lambda r: mongo_match(r, body.get("filter", {})),
            )
            return httpx.Response(
                200,
                json={
                    "matches": [
                        {
                            "id": r["id"],
                            "score": 1.0,
                            "metadata": {k: v for k, v in r.items() if k not in ("id", "vector")},
                        }
                        for r in selected
                    ],
                    "namespace": namespace,
                },
            )
        raise AssertionError((request.method, path, body))
