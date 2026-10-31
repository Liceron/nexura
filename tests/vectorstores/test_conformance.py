# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Public protocol conformance; the same tests run against opt-in real HTTP APIs."""

from __future__ import annotations

from typing import Any

import pytest

from nexura_runtime.knowledge.chunks.models import IndexLineage
from nexura_runtime.knowledge.vector.errors import (
    CollectionNotFoundError,
    GenerationNotFoundError,
    ImmutableGenerationError,
    TenantIsolationViolationError,
    VectorDimensionMismatchError,
    VectorStoreError,
)
from nexura_runtime.knowledge.vector.metrics import compute_score
from nexura_runtime.knowledge.vector.models import (
    VectorRecord,
    VectorSearchQuery,
    matches_filter,
)
from nexura_runtime.knowledge.vector.store import VectorStore

from .conftest import StoreCase


def lineage() -> IndexLineage:
    return IndexLineage(
        chunker_name="offline",
        chunker_config_hash="v1",
        model_name="local-fake-model",
        model_revision="v1",
        dimensions=2,
    )


def record(
    collection: str,
    record_id: str = "a",
    *,
    workspace: str = "ws",
    vector: list[float] | None = None,
    metadata: dict[str, Any] | None = None,
) -> VectorRecord:
    return VectorRecord(
        id=record_id,
        workspace_id=workspace,
        collection_id=collection,
        generation_id="",
        vector=vector or [1.0, 0.0],
        text="offline test document",
        document_id="doc_" + record_id,
        metadata=metadata or {},
    )


def query(collection: str, **kwargs: Any) -> VectorSearchQuery:
    return VectorSearchQuery(
        workspace_id="ws", collection_id=collection, vector=[1.0, 0.0], **kwargs
    )


async def test_lifecycle_and_persistence(case: StoreCase, collection: str) -> None:
    store = case.make()
    assert isinstance(store, VectorStore)
    if case.kind != "memory":
        assert store.is_available
    first = await store.create_collection("ws", collection, 2)
    assert first.status == "active"
    assert (await store.create_collection("ws", collection, 2)).generation_id == first.generation_id
    building = await store.create_generation("ws", collection, lineage(), 2)
    assert building.status == "building"
    assert await store.upsert("ws", collection, [record(collection)], building.generation_id) == 1
    assert await store.count("ws", collection) == 0
    await store.activate_generation("ws", collection, building.generation_id)
    active = await store.get_active_generation("ws", collection)
    assert active and active.generation_id == building.generation_id and active.record_count == 1
    assert len(await store.search("ws", query(collection))) == 1
    with pytest.raises(ImmutableGenerationError):
        await store.upsert("ws", collection, [record(collection)], first.generation_id)
    # Qdrant permits active writes. Repeated activation must preserve active status.
    await store.activate_generation("ws", collection, building.generation_id)
    assert await store.upsert("ws", collection, [record(collection)], building.generation_id) == 1
    if case.kind != "memory" and not case.live:
        reopened = case.reopen()
        active = await reopened.get_active_generation("ws", collection)
        assert active and active.generation_id == building.generation_id
        assert await reopened.get("ws", collection, "a") == await store.get("ws", collection, "a")


@pytest.mark.parametrize("metric", ["cosine", "dot_product", "euclidean"])
async def test_ranking_scores_and_projection(
    case: StoreCase,
    collection: str,
    metric: str,
) -> None:
    store = case.make(metric)
    await store.create_collection("ws", collection, 2, metric=metric)
    vectors = {"a": [1.0, 0.0], "b": [2.0, 1.0], "c": [-1.0, 0.1], "d": [0.2, 0.8]}
    await store.upsert(
        "ws",
        collection,
        [
            record(collection, key, vector=vec, metadata={"kind": "test"})
            for key, vec in vectors.items()
        ],
    )
    hits = await store.search("ws", query(collection, top_k=4, include_vector=True))
    expected = sorted(
        vectors, key=lambda k: compute_score(metric, [1, 0], vectors[k]), reverse=True
    )
    assert [h.id for h in hits] == expected
    for hit in hits:
        assert hit.score == pytest.approx(compute_score(metric, [1, 0], vectors[hit.id]), abs=1e-6)
        assert hit.vector == vectors[hit.id] and hit.metadata == {"kind": "test"}
        assert hit.text == "offline test document" and hit.document_id == "doc_" + hit.id
    projected = await store.search("ws", query(collection, top_k=1, include_metadata=False))
    assert projected[0].metadata == {} and projected[0].vector is None


FILTERS = [
    {"kind": "red"},
    {"kind": {"$eq": "red"}},
    {"kind": {"$ne": "red"}},
    {"kind": {"$in": ["red", "blue"]}},
    {"kind": {"$nin": ["red", "blue"]}},
    {"n": {"$gt": 2}},
    {"n": {"$gte": 2}},
    {"n": {"$lt": 3}},
    {"n": {"$lte": 3}},
    {"text": {"$contains": "middle"}},
    {"tags": {"$contains": "x"}},
    {"$and": [{"kind": "red"}, {"n": {"$gte": 2}}]},
    {"$or": [{"kind": "blue"}, {"n": {"$gt": 3}}]},
    {"$and": [{"$or": [{"kind": "blue"}, {"tags": {"$contains": "x"}}]}, {"n": {"$gt": 0}}]},
    {"missing": {"$ne": "x"}},
    {"missing": {"$nin": [1, 2]}},
    {"missing": {"$eq": None}},
    {"json": {"$eq": {"nested": 1}}},
    {"kind": {"$in": []}},
    {"kind": {"$nin": []}},
    {"flag": {"$eq": True}},
    {"flag": {"$eq": 1}},
    {"text": {"$contains": 7}},
    {"$or": []},
    {"$and": []},
]


@pytest.mark.parametrize("expression", FILTERS)
async def test_every_filter_operator(
    case: StoreCase,
    collection: str,
    expression: dict[str, Any],
) -> None:
    store = case.make()
    await store.create_collection("ws", collection, 2)
    metadata = [
        {"kind": "red", "n": 1, "text": "prefix middle suffix", "tags": ["x", "y"], "flag": True},
        {"kind": "blue", "n": 2, "text": "7", "tags": ["y"], "json": {"nested": 1}},
        {"kind": "red", "n": 3, "text": "middle", "tags": ["x"]},
        {"n": 4},
    ]
    await store.upsert(
        "ws",
        collection,
        [
            record(collection, str(i), vector=[1, i + 0.1], metadata=m)
            for i, m in enumerate(metadata)
        ],
    )
    expected = {str(i) for i, m in enumerate(metadata) if matches_filter(m, expression)}
    hits = await store.search("ws", query(collection, top_k=10, filter=expression))
    assert {h.id for h in hits} == expected
    # A fallback must fill top_k even when the closest records fail its predicate.
    top = await store.search("ws", query(collection, top_k=1, filter=expression))
    assert len(top) == min(1, len(expected))


async def test_upsert_delete_and_tombstones(case: StoreCase, collection: str) -> None:
    store = case.make()
    await store.create_collection("ws", collection, 2)
    original = record(collection, metadata={"old": "value"})
    await store.upsert("ws", collection, [original])
    await store.upsert("ws", collection, [record(collection, metadata={"new": "value"})])
    assert await store.count("ws", collection) == 1
    if case.kind != "memory":
        assert (await store.create_collection("ws", collection, 2)).record_count == 1
    fetched = await store.get("ws", collection, "a")
    assert fetched and fetched.metadata == {"new": "value"}
    assert fetched.created_at > 0 and fetched.updated_at > 0
    assert await store.delete("ws", collection, ["a", "a", "missing"]) == 1
    assert await store.delete("ws", collection, ["a"]) == 0
    assert await store.get("ws", collection, "a") is None
    assert await store.count("ws", collection) == 0
    assert await store.search("ws", query(collection)) == []
    assert await store.delete("ws", collection, ["a"], hard_delete=True) == 1
    assert await store.delete("ws", collection, ["a"], hard_delete=True) == 0
    await store.upsert("ws", collection, [original])
    assert await store.get("ws", collection, "a") is not None
    assert await store.delete("ws", collection, ["a"], hard_delete=True) == 1
    assert await store.upsert("ws", collection, []) == 0
    assert await store.delete("ws", collection, []) == 0


async def test_workspace_and_generation_isolation(case: StoreCase, collection: str) -> None:
    store = case.make()
    first = await store.create_collection("ws", collection, 2)
    await store.create_collection("other", collection, 2)
    await store.upsert("ws", collection, [record(collection)])
    assert await store.count("other", collection) == 0
    assert await store.get("other", collection, "a") is None
    assert await store.delete("other", collection, ["a"], hard_delete=True) == 0
    assert await store.count("ws", collection) == 1
    assert await store.get("other", collection, "a", first.generation_id) is None
    assert (
        await store.search("other", query(collection).model_copy(update={"workspace_id": "other"}))
        == []
    )
    second = await store.create_generation("ws", collection, lineage(), 2)
    await store.activate_generation("ws", collection, second.generation_id)
    assert await store.get("ws", collection, "a") is None
    assert await store.get("ws", collection, "a", first.generation_id) is not None
    assert await store.count("ws", collection, first.generation_id) == 1
    # The historical in-memory reference does not use the shared tenant/filter
    # validators; preserve its existing behavior and exercise negatives on HTTP stores.
    if case.kind != "memory":
        with pytest.raises(TenantIsolationViolationError):
            await store.upsert("ws", collection, [record(collection, workspace="other")])
        with pytest.raises(TenantIsolationViolationError):
            await store.search("other", query(collection))
        with pytest.raises(TenantIsolationViolationError):
            await store.search("ws", query(collection, filter={"workspace_id": "other"}))
        assert (
            await store.search(
                "ws",
                query(
                    collection,
                    filter={
                        "$or": [{"workspace_id": "other"}, {"n": 100}],
                    },
                ),
            )
            == []
        )


async def test_replaced_metadata_does_not_underfill(case: StoreCase, collection: str) -> None:
    store = case.make()
    await store.create_collection("ws", collection, 2)
    await store.upsert(
        "ws",
        collection,
        [
            record(collection, "a", metadata={"kind": "old"}),
            record(collection, "b", vector=[0.8, 0.2], metadata={"kind": "old"}),
        ],
    )
    await store.upsert("ws", collection, [record(collection, "a", metadata={"fresh": True})])
    hits = await store.search("ws", query(collection, top_k=1, filter={"kind": "old"}))
    assert [h.id for h in hits] == ["b"]


async def test_dimensions_and_unknown_ids(case: StoreCase, collection: str) -> None:
    store = case.make()
    with pytest.raises(VectorDimensionMismatchError):
        await store.create_collection("ws", collection, 0)
    with pytest.raises(CollectionNotFoundError):
        await store.create_generation("ws", collection, lineage(), 2)
    with pytest.raises(CollectionNotFoundError):
        await store.upsert("ws", collection, [record(collection)])
    assert await store.get_active_generation("ws", collection) is None
    assert await store.get("ws", collection, "a") is None
    assert await store.count("ws", collection) == 0
    assert await store.search("ws", query(collection)) == []
    assert await store.delete("ws", collection, ["a"]) == 0
    await store.create_collection("ws", collection, 2)
    with pytest.raises(GenerationNotFoundError):
        await store.upsert("ws", collection, [record(collection)], "missing")
    with pytest.raises(GenerationNotFoundError):
        await store.activate_generation("ws", collection, "missing")
    with pytest.raises(VectorDimensionMismatchError):
        await store.upsert("ws", collection, [record(collection, vector=[1])])
    with pytest.raises(VectorDimensionMismatchError):
        await store.search("ws", query(collection).model_copy(update={"vector": [1]}))
    assert await store.get("ws", collection, "a", "missing") is None
    assert await store.count("ws", collection, "missing") == 0
    assert await store.search("ws", query(collection, generation_id="missing")) == []
    assert await store.delete("ws", collection, ["a"], generation_id="missing") == 0
    if case.kind != "memory":
        with pytest.raises(VectorStoreError, match="Unsupported vector metric"):
            await store.create_collection("ws", "bad-metric", 2, metric="invalid")


@pytest.mark.parametrize("status", [401, 500])
async def test_http_errors_and_secret_redaction(
    case: StoreCase,
    collection: str,
    status: int,
) -> None:
    if case.kind == "memory" or case.live:
        pytest.skip("Injected HTTP failures only apply to offline HTTP adapters")
    store = case.make()
    case.fake.fail_status = status
    with pytest.raises(VectorStoreError) as error:
        await store.create_collection("ws", collection, 2)
    assert str(status) in str(error.value)
    assert "OFFLINE_FAILURE" in str(error.value)
    assert error.value.details["http_status"] == status
    assert "s3cr3t" not in repr(store) + str(error.value) + repr(error.value.details)
    case.fake.fail_status = status
    case.fake.fail_code = "s3cr3t"
    with pytest.raises(VectorStoreError) as redacted:
        await store.create_collection("ws", collection, 2)
    assert "s3cr3t" not in str(redacted.value) + repr(redacted.value.details)
