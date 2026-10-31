# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Transport ownership, persistence failures, API application errors and guardrails."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from nexura_runtime.knowledge.vector.errors import (
    GenerationNotFoundError,
    TenantIsolationViolationError,
    VectorDimensionMismatchError,
    VectorStoreError,
)

from .conftest import ADAPTERS, StoreCase
from .fakes import FAKES
from .test_conformance import lineage, query, record


@pytest.fixture(params=list(ADAPTERS))
async def wire_case(request: pytest.FixtureRequest) -> Any:
    case = StoreCase(request.param)
    yield case
    for store in case.stores:
        await store.aclose()


async def test_switch_failure_and_serialized_writers(wire_case: StoreCase, collection: str) -> None:
    store = wire_case.make()
    first = await store.create_collection("ws", collection, 2)
    generations = await asyncio.gather(
        *(store.create_generation("ws", collection, lineage(), 2) for _ in range(3))
    )
    assert len({g.generation_id for g in generations}) == 3
    wire_case.fake.fail_at = len(wire_case.fake.requests) + 2
    wire_case.fake.fail_status = 500
    with pytest.raises(VectorStoreError):
        await store.activate_generation("ws", collection, generations[0].generation_id)
    active = await store.get_active_generation("ws", collection)
    assert active and active.generation_id == first.generation_id
    await store.activate_generation("ws", collection, generations[0].generation_id)
    active = await wire_case.reopen().get_active_generation("ws", collection)
    assert active and active.generation_id == generations[0].generation_id
    for generation in generations[1:]:
        assert (
            await store.upsert("ws", collection, [record(collection)], generation.generation_id)
            == 1
        )


async def test_scope_validation_and_collision_resistance(
    wire_case: StoreCase,
    collection: str,
) -> None:
    store = wire_case.make()
    await store.create_collection("a:b", collection, 2)
    await store.create_collection("a_b", collection, 2)
    await store.upsert("a:b", collection, [record(collection, workspace="a:b")])
    assert await store.count("a_b", collection) == 0
    await store.create_collection("ws", collection, 2)
    with pytest.raises(VectorStoreError, match="collection"):
        await store.upsert("ws", collection, [record("other")])
    with pytest.raises(VectorStoreError, match="generation"):
        await store.upsert(
            "ws", collection, [record(collection).model_copy(update={"generation_id": "other"})]
        )
    with pytest.raises(VectorStoreError, match="Unsupported filter operator"):
        await store.search("ws", query(collection, filter={"n": {"$exec": "s3cr3t"}}))
    with pytest.raises(VectorStoreError, match="requires an iterable"):
        await store.search("ws", query(collection, filter={"n": {"$in": 1}}))
    with pytest.raises(GenerationNotFoundError):
        await store.activate_generation("other", collection, "wrong")
    # Validate the entire batch before any write, including wrong-tenant tail records.
    with pytest.raises(TenantIsolationViolationError):
        await store.upsert(
            "ws",
            collection,
            [
                record(collection),
                record(collection, "wrong", workspace="other"),
            ],
        )
    assert await store.count("ws", collection) == 0


async def test_bulk_and_large_record_batches(wire_case: StoreCase, collection: str) -> None:
    store = wire_case.make()
    await store.create_collection("ws", collection, 2)
    rows = [record(collection, str(i), metadata={"n": i}) for i in range(205)]
    assert await store.upsert("ws", collection, rows) == 205
    assert await store.count("ws", collection) == 205
    hits = await store.search("ws", query(collection, top_k=2, filter={"n": {"$gte": 203}}))
    assert {h.id for h in hits} == {"203", "204"}
    assert await store.delete("ws", collection, ["203", "204"]) == 2
    assert await store.count("ws", collection) == 203
    assert (
        await store.delete("ws", collection, [str(i) for i in range(205)], hard_delete=True) == 205
    )
    assert await store.count("ws", collection) == 0


async def test_native_filter_and_reserved_metadata(wire_case: StoreCase, collection: str) -> None:
    store = wire_case.make()
    await store.create_collection("ws", collection, 2)
    await store.upsert(
        "ws",
        collection,
        [
            record(
                collection,
                metadata={
                    "category": 'quote " newline\n',
                    "payload": "cannot replace stored payload",
                    "deleted": True,
                    "workspace_id": "other",
                    "$and": "ordinary metadata",
                },
            )
        ],
    )
    start = len(wire_case.fake.requests)
    hits = await store.search("ws", query(collection, filter={"category": 'quote " newline\n'}))
    assert len(hits) == 1 and hits[0].metadata["deleted"] is True
    requests = wire_case.fake.requests[start:]
    search_request = next(
        r
        for r in requests
        if (
            r.url.path.endswith(("/query", "/search", "/_search", "/graphql"))
            and (wire_case.kind != "weaviate" or "nearVector" in r.content.decode())
            and (wire_case.kind != "milvus" or r.url.path.endswith("/search"))
        )
    )
    # All dialects must push a native hashed scalar projection into the query.
    assert "category" not in search_request.content.decode()
    assert "quote" in search_request.content.decode()


@pytest.mark.parametrize("kind", list(ADAPTERS))
async def test_timeout_context_manager_and_injected_client(kind: str) -> None:
    fake = FAKES[kind]()
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    options = {"dimensions": 2} if kind == "pinecone" else {}
    async with ADAPTERS[kind](
        "http://offline.invalid", "s3cr3t", client=client, timeout=7, **options
    ) as store:
        await store.create_collection("ws", "timeout", 2)
        assert all(r.extensions["timeout"]["read"] == 7 for r in fake.requests)
    assert not client.is_closed
    await client.aclose()
    owned = ADAPTERS[kind](
        "http://offline.invalid", "s3cr3t", transport=httpx.MockTransport(fake), **options
    )
    async with owned:
        pass
    assert owned._client.is_closed


@pytest.mark.parametrize("kind", list(ADAPTERS))
async def test_transport_error_redaction_and_non_json_http_errors(kind: str) -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("s3cr3t header and payload", request=request)

    options = {"dimensions": 2} if kind == "pinecone" else {}
    async with ADAPTERS[kind](
        "http://offline.invalid", "s3cr3t", transport=httpx.MockTransport(timeout), **options
    ) as store:
        with pytest.raises(VectorStoreError) as error:
            await store.create_collection("ws", "c", 2)
        assert "s3cr3t" not in str(error.value)
    async with ADAPTERS[kind](
        "http://offline.invalid",
        "s3cr3t",
        transport=httpx.MockTransport(lambda _: httpx.Response(502, text="s3cr3t")),
        **options,
    ) as store:
        with pytest.raises(VectorStoreError, match="502") as error:
            await store.create_collection("ws", "c", 2)
        assert "s3cr3t" not in str(error.value)


async def test_milvus_application_error() -> None:
    async with ADAPTERS["milvus"](
        "http://offline.invalid",
        "s3cr3t",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "code": 1100,
                    "message": "s3cr3t",
                },
            )
        ),
    ) as store:
        with pytest.raises(VectorStoreError, match="1100") as error:
            await store.create_collection("ws", "c", 2)
        assert "s3cr3t" not in str(error.value)


@pytest.mark.parametrize("kind", ["elasticsearch", "opensearch"])
async def test_search_engine_bulk_item_errors(kind: str) -> None:
    case = StoreCase(kind)
    store = case.make()
    try:
        await store.create_collection("ws", "c", 2)
        case.fake.bulk_error = True
        with pytest.raises(VectorStoreError, match="400.*mapper_parsing_exception") as error:
            await store.upsert("ws", "c", [record("c")])
        assert "s3cr3t" not in str(error.value)
    finally:
        await store.aclose()


async def test_pinecone_fixed_index_configuration() -> None:
    case = StoreCase("pinecone")
    store = case.make()
    try:
        await store.create_collection("ws", "c", 2)
        with pytest.raises(VectorDimensionMismatchError):
            await store.create_generation("ws", "c", lineage(), 3)
        with pytest.raises(VectorStoreError, match="pre-provisioned"):
            await store.create_generation("ws", "c", lineage(), 2, metric="dot_product")
    finally:
        await store.aclose()


async def test_weaviate_graphql_and_batch_errors() -> None:
    case = StoreCase("weaviate")
    store = case.make()
    try:
        await store.create_collection("ws", "c", 2)
        original = case.fake.handle

        def fail_graphql(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
            if request.url.path == "/v1/graphql":
                return httpx.Response(200, json={"errors": [{"message": "s3cr3t"}]})
            return original(request, body)

        case.fake.handle = fail_graphql
        with pytest.raises(VectorStoreError, match="graphql_error") as error:
            await store.count("ws", "c")
        assert "s3cr3t" not in str(error.value)

        def fail_batch(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
            if request.url.path == "/v1/batch/objects":
                return httpx.Response(
                    200,
                    json=[
                        {
                            "result": {
                                "errors": {"error": [{"message": "s3cr3t"}]},
                            }
                        }
                    ],
                )
            return original(request, body)

        case.fake.handle = fail_batch
        with pytest.raises(VectorStoreError, match="batch_error") as error:
            await store.upsert("ws", "c", [record("c")])
        assert "s3cr3t" not in str(error.value)
    finally:
        await store.aclose()


@pytest.mark.parametrize("kind", list(ADAPTERS))
async def test_candidate_bound_is_explicit(kind: str) -> None:
    case = StoreCase(kind)
    store = case.make()
    try:
        await store.create_collection("ws", "c", 2)
        await store.upsert("ws", "c", [record("c"), record("c", "b")])
        store.max_candidates = 1
        with pytest.raises(VectorStoreError, match="candidate limit"):
            await store.search("ws", query("c"))
    finally:
        await store.aclose()


def test_manifest_and_record_ids_are_not_delimiter_ambiguous() -> None:
    from nexura_vectorstores.base import space_name

    assert space_name("a:b", "c", "d") != space_name("a", "b:c", "d")
    assert space_name("a/b", "c") != space_name("a_b", "c")


def test_package_metadata_and_notices() -> None:
    import importlib.metadata
    from pathlib import Path

    metadata = importlib.metadata.metadata("nexura-vectorstores")
    # Released in lockstep with the runtime it implements the protocol for.
    assert metadata["Version"] == importlib.metadata.version("nexura-runtime")
    assert metadata["License-Expression"] == "Apache-2.0"
    assert set(metadata.get_all("License-File", [])) == {"LICENSE", "NOTICE"}
    assert not any("qdrant-client" in d for d in importlib.metadata.requires("nexura-vectorstores"))
    root = Path(__file__).resolve().parents[2]
    package = root / "packages/nexura-vectorstores"
    for name in ("LICENSE", "NOTICE"):
        assert (package / name).read_bytes() == (
            root / "packages/nexura-qdrant" / name
        ).read_bytes()
