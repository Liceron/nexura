# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Chroma REST v2: collection ids, columnar get and nested query results."""

from __future__ import annotations

import uuid
from typing import Any

import httpx

from .common import FakeAPI, mongo_match


class ChromaAPI(FakeAPI):
    def handle(self, request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        path = request.url.path.split("/")
        assert path[:4] == ["", "api", "v2", "tenants"]
        assert path[5] == "databases" and path[7] == "collections"
        if len(path) == 8:
            assert request.method == "POST"
            name = body["name"]
            assert body["configuration"]["hnsw"]["space"] in ("cosine", "ip", "l2")
            if name not in self.schemas:
                collection_id = str(uuid.uuid4())
                self.schemas[name] = {"id": collection_id, **body}
                self.rows[collection_id] = {}
            return httpx.Response(200, json=self.schemas[name])
        name = path[8]
        if len(path) == 9:
            return httpx.Response(200, json=self.schemas[name])
        op = path[9]
        rows = self.rows[name]
        if op == "upsert":
            for i, vec, meta in zip(
                body["ids"], body["embeddings"], body["metadatas"], strict=True
            ):
                assert all(isinstance(v, (str, int, float, bool)) for v in meta.values())
                rows[i] = {**rows.get(i, {}), "id": i, "vector": vec, **meta}
            return httpx.Response(200, json=True)
        if op == "get":
            selected = [
                r
                for r in rows.values()
                if (
                    ("ids" not in body or r["id"] in body["ids"])
                    and mongo_match(r, body.get("where", {}))
                )
            ]
            offset = body.get("offset", 0)
            selected = selected[offset : offset + body.get("limit", len(rows))]
            return httpx.Response(
                200,
                json={
                    "ids": [r["id"] for r in selected],
                    "metadatas": [
                        {k: v for k, v in r.items() if k not in ("id", "vector")} for r in selected
                    ],
                },
            )
        if op == "query":
            selected = self.ranked(
                name,
                body["query_embeddings"][0],
                body["n_results"],
                lambda r: mongo_match(r, body["where"]),
            )
            return httpx.Response(
                200,
                json={
                    "ids": [[r["id"] for r in selected]],
                    "metadatas": [
                        [
                            {k: v for k, v in r.items() if k not in ("id", "vector")}
                            for r in selected
                        ]
                    ],
                    "distances": [[0.0 for _ in selected]],
                },
            )
        if op == "delete":
            for i in body["ids"]:
                rows.pop(i, None)
            return httpx.Response(200, json=True)
        if op == "count":
            assert request.method == "GET"
            return httpx.Response(200, json=len(rows))
        raise AssertionError((request.method, request.url.path, body))
