# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Keyless Chroma REST v2 example with an explicitly labelled local HTTP fake."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from nexura_vectorstores import ChromaVectorStore

from nexura_runtime.knowledge.vector.models import VectorRecord, VectorSearchQuery


async def main() -> None:
    collections: dict[str, Any] = {}
    rows: dict[str, dict[str, Any]] = {}

    def local_fake(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        parts = request.url.path.split("/")
        if request.url.path.endswith("/collections"):
            name = body["name"]
            collections.setdefault(name, {"id": name, **body})
            rows.setdefault(name, {})
            return httpx.Response(200, json=collections[name])
        name, operation = parts[-2:]
        if operation == "upsert":
            for key, metadata in zip(body["ids"], body["metadatas"], strict=True):
                rows[name][key] = metadata
            return httpx.Response(200, json=True)
        if operation == "get":
            selected = [i for i in body["ids"] if i in rows[name]]
            return httpx.Response(
                200,
                json={
                    "ids": selected,
                    "metadatas": [rows[name][i] for i in selected],
                },
            )
        if operation == "count":
            return httpx.Response(200, json=len(rows[name]))
        if operation == "query":
            return httpx.Response(
                200,
                json={
                    "ids": [list(rows[name])],
                    "metadatas": [list(rows[name].values())],
                },
            )
        raise AssertionError("Unexpected local fake operation")

    async with ChromaVectorStore(
        "http://local-fake.invalid", transport=httpx.MockTransport(local_fake)
    ) as store:
        await store.create_collection("demo", "documents", 2)
        await store.upsert(
            "demo",
            "documents",
            [
                VectorRecord(
                    id="one",
                    workspace_id="demo",
                    collection_id="documents",
                    generation_id="",
                    vector=[1.0, 0.0],
                    text="Local fake HTTP; no model called.",
                )
            ],
        )
        hits = await store.search(
            "demo",
            VectorSearchQuery(
                workspace_id="demo",
                collection_id="documents",
                vector=[1.0, 0.0],
                top_k=1,
            ),
        )
        print(hits[0].text, hits[0].score)


if __name__ == "__main__":
    asyncio.run(main())
