# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Milvus REST v2 application envelopes, custom schema and dynamic fields."""

from __future__ import annotations

from typing import Any

import httpx

from .common import FakeAPI, milvus_match


class MilvusAPI(FakeAPI):
    def handle(self, request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        assert request.method == "POST"
        assert body["dbName"] == "default"
        path = request.url.path.removeprefix("/v2/vectordb/")
        name = body.get("collectionName")
        data: Any = {}
        if path == "collections/list":
            data = list(self.schemas)
        elif path == "collections/create":
            assert body["schema"]["autoId"] is False
            assert body["schema"]["enableDynamicField"] is True
            # Real Milvus REST queries use the collection's default consistency level.
            assert body["params"]["consistencyLevel"] == "Strong"
            for field in body["schema"]["fields"]:
                dim = field.get("elementTypeParams", {}).get("dim")
                if dim is not None and not 2 <= int(dim) <= 32768:
                    # Real Milvus 2.4 answers HTTP 200 with an application error here.
                    message = f"invalid dimension: {dim}. should be in range 2 ~ 32768"
                    return httpx.Response(200, json={"code": 65535, "message": message})
            self.schemas[name] = body
            self.rows[name] = {}
        elif path == "collections/describe":
            # Milvus 2.4 describes each field with "name" and "params" as key/value pairs.
            fields = [
                {
                    "name": f["fieldName"],
                    "type": f["dataType"],
                    "params": [
                        {"key": k, "value": v} for k, v in f.get("elementTypeParams", {}).items()
                    ],
                }
                for f in self.schemas[name]["schema"]["fields"]
            ]
            data = {"collectionName": name, "fields": fields}
        elif path == "entities/upsert":
            for row in body["data"]:
                self.rows[name][row["id"]] = row.copy()
            data = {"upsertCount": len(body["data"])}
        elif path == "entities/query":
            assert body["consistencyLevel"] == "Strong"
            rows = [r for r in self.rows[name].values() if milvus_match(r, body["filter"])]
            data = (
                [{"count(*)": len(rows)}]
                if body["outputFields"] == ["count(*)"]
                else rows[: body["limit"]]
            )
        elif path == "entities/search":
            assert body["annsField"] == "vector"
            data = self.ranked(
                name, body["data"][0], body["limit"], lambda r: milvus_match(r, body["filter"])
            )
            data = [{**r, "distance": 1.0} for r in data]
        elif path == "entities/delete":
            ids = [r["id"] for r in self.rows[name].values() if milvus_match(r, body["filter"])]
            for i in ids:
                self.rows[name].pop(i)
            data = {"deleteCount": len(ids)}
        else:
            raise AssertionError((path, body))
        return httpx.Response(200, json={"code": 0, "data": data})
