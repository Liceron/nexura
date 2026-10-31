# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""One factory drives the memory reference, six wire fakes and opt-in real servers."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from nexura_vectorstores import (
    ChromaVectorStore,
    ElasticsearchVectorStore,
    MilvusVectorStore,
    OpenSearchVectorStore,
    PineconeVectorStore,
    WeaviateVectorStore,
)

from nexura_runtime.knowledge.vector.store import InMemoryVectorStore

from .fakes import FAKES

ADAPTERS = {
    "chroma": ChromaVectorStore,
    "pinecone": PineconeVectorStore,
    "weaviate": WeaviateVectorStore,
    "milvus": MilvusVectorStore,
    "elasticsearch": ElasticsearchVectorStore,
    "opensearch": OpenSearchVectorStore,
}


class StoreCase:
    def __init__(self, kind: str) -> None:
        self.live = kind.startswith("live-")
        self.kind = kind.removeprefix("live-")
        self.fake: Any = None
        self.stores: list[Any] = []

    def make(self, metric: str = "cosine") -> Any:
        if self.kind == "memory":
            store = InMemoryVectorStore()
        else:
            options: dict[str, Any] = {}
            if self.kind == "pinecone":
                options.update(dimensions=2, metric=metric)
            if self.live:
                prefix = "NEXURA_TEST_" + self.kind.upper()
                url = os.getenv(prefix + "_URL")
                if not url:
                    pytest.skip(prefix + "_URL is unset")
                key = os.getenv(prefix + "_API_KEY") or os.getenv(prefix + "_TOKEN")
                if self.kind == "pinecone":
                    if not key:
                        pytest.skip(prefix + "_API_KEY is unset")
                    if metric != os.getenv(prefix + "_METRIC", "cosine"):
                        pytest.skip("Pinecone's provisioned index has a different metric")
                store = ADAPTERS[self.kind](url, key, **options)
            else:
                self.fake = FAKES[self.kind](metric)
                store = ADAPTERS[self.kind](
                    "http://offline.invalid",
                    "s3cr3t",
                    transport=httpx.MockTransport(self.fake),
                    **options,
                )
        self.stores.append(store)
        return store

    def reopen(self) -> Any:
        assert self.fake is not None
        options = {"dimensions": 2, "metric": self.fake.metric} if self.kind == "pinecone" else {}
        store = ADAPTERS[self.kind](
            "http://offline.invalid", "s3cr3t", transport=httpx.MockTransport(self.fake), **options
        )
        self.stores.append(store)
        return store


@pytest.fixture(params=["memory", *ADAPTERS, *("live-" + name for name in ADAPTERS)])
async def case(request: pytest.FixtureRequest) -> AsyncIterator[StoreCase]:
    result = StoreCase(request.param)
    yield result
    for store in result.stores:
        if hasattr(store, "aclose"):
            await store.aclose()


@pytest.fixture
def collection() -> str:
    return "p44v_" + uuid.uuid4().hex
