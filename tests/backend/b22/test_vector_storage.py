"""Unit and acceptance tests for Module B22: Vector Storage and Indexes."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from nexura_runtime.contracts.context import ExecutionContext

from nexura_runtime.knowledge.chunks.models import IndexLineage
from nexura_runtime.knowledge.vector import (
    InMemoryVectorStore,
    SqliteVectorStore,
    VectorDimensionMismatchError,
    VectorRecord,
    VectorSearchQuery,
    VectorStoreModule,
)
from nexura_runtime.knowledge.vector.errors import (
    ImmutableGenerationError,
)
from nexura_runtime.modules.registry import default_registry


class TestTenantIsolationAndScoping:
    """Acceptance: Wrong-tenant filters cannot be overridden."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("store_cls", [InMemoryVectorStore, SqliteVectorStore])
    async def test_wrong_tenant_filters_cannot_be_overridden(self, store_cls: type) -> None:
        store = store_cls() if store_cls is InMemoryVectorStore else store_cls(":memory:")

        # Workspace A setup
        await store.create_collection("ws_A", "finance", dimensions=4)
        rec_a = VectorRecord(
            id="doc_a1",
            workspace_id="ws_A",
            collection_id="finance",
            generation_id="",
            vector=[1.0, 0.0, 0.0, 0.0],
            text="Confidential financial report of Tenant A",
            metadata={"confidential": True, "tenant": "A"},
        )
        await store.upsert("ws_A", "finance", [rec_a])

        # Workspace B setup
        await store.create_collection("ws_B", "finance", dimensions=4)
        rec_b = VectorRecord(
            id="doc_b1",
            workspace_id="ws_B",
            collection_id="finance",
            generation_id="",
            vector=[1.0, 0.0, 0.0, 0.0],
            text="Internal strategy of Tenant B",
            metadata={"confidential": True, "tenant": "B"},
        )
        await store.upsert("ws_B", "finance", [rec_b])

        # Query from Workspace A attempting to access Workspace B via query parameter or filter
        malicious_query = VectorSearchQuery(
            vector=[1.0, 0.0, 0.0, 0.0],
            collection_id="finance",
            workspace_id="ws_B",  # Malicious attempted override
            top_k=10,
            filter={"workspace_id": "ws_B", "tenant": "B"},  # Filter override attempt
        )

        # Enforced search scoping in ws_A
        results = await store.search("ws_A", malicious_query)

        # Must NOT return doc_b1!
        returned_ids = [r.id for r in results]
        assert "doc_b1" not in returned_ids
        # Since filter specified tenant="B", and ws_A only has tenant="A", results should be empty
        assert len(results) == 0

        # Normal query in ws_A returns doc_a1
        normal_query = VectorSearchQuery(
            vector=[1.0, 0.0, 0.0, 0.0],
            collection_id="finance",
            workspace_id="ws_A",
            top_k=5,
        )
        a_results = await store.search("ws_A", normal_query)
        assert len(a_results) == 1
        assert a_results[0].id == "doc_a1"


class TestIdempotencyAndTombstones:
    """Acceptance: upsert/delete idempotent."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("store_cls", [InMemoryVectorStore, SqliteVectorStore])
    async def test_upsert_and_delete_idempotent(self, store_cls: type) -> None:
        store = store_cls() if store_cls is InMemoryVectorStore else store_cls(":memory:")
        await store.create_collection("ws_main", "articles", dimensions=3)

        rec = VectorRecord(
            id="art_1",
            workspace_id="ws_main",
            collection_id="articles",
            generation_id="",
            vector=[0.5, 0.5, 0.0],
            text="Initial text",
            metadata={"views": 10},
        )

        # Initial upsert
        u1 = await store.upsert("ws_main", "articles", [rec])
        assert u1 == 1
        assert await store.count("ws_main", "articles") == 1

        # Duplicate upsert with updated text
        rec_updated = rec.model_copy(update={"text": "Updated text", "metadata": {"views": 15}})
        u2 = await store.upsert("ws_main", "articles", [rec_updated])
        assert u2 == 1
        assert await store.count("ws_main", "articles") == 1  # Count does not duplicate!

        fetched = await store.get("ws_main", "articles", "art_1")
        assert fetched is not None
        assert fetched.text == "Updated text"
        assert fetched.metadata["views"] == 15

        # Soft delete (tombstone)
        d1 = await store.delete("ws_main", "articles", ["art_1"], hard_delete=False)
        assert d1 == 1
        assert await store.count("ws_main", "articles") == 0
        assert await store.get("ws_main", "articles", "art_1") is None

        # Redundant delete is idempotent
        d2 = await store.delete("ws_main", "articles", ["art_1"], hard_delete=False)
        assert d2 == 0

        # Re-upsert brings record back from tombstone
        u3 = await store.upsert("ws_main", "articles", [rec])
        assert u3 == 1
        assert await store.count("ws_main", "articles") == 1

        # Hard delete
        d3 = await store.delete("ws_main", "articles", ["art_1"], hard_delete=True)
        assert d3 == 1
        assert await store.count("ws_main", "articles") == 0


class TestIndexGenerationsAndAtomicSwitch:
    """Acceptance: stale generation never mixes embeddings."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("store_cls", [InMemoryVectorStore, SqliteVectorStore])
    async def test_stale_generation_never_mixes_embeddings(self, store_cls: type) -> None:
        store = store_cls() if store_cls is InMemoryVectorStore else store_cls(":memory:")

        # 1. Create collection with generation 1 (dim=4)
        lin_v1 = IndexLineage(
            chunker_name="fixed",
            chunker_config_hash="cfg1",
            model_name="small_embedder",
            model_revision="v1",
            dimensions=4,
        )
        gen1 = await store.create_collection("ws_prod", "kb", dimensions=4, lineage=lin_v1)
        assert gen1.status == "active"

        rec1 = VectorRecord(
            id="chunk_1",
            workspace_id="ws_prod",
            collection_id="kb",
            generation_id=gen1.generation_id,
            vector=[1.0, 0.0, 0.0, 0.0],
            text="Knowledge snippet v1",
        )
        await store.upsert("ws_prod", "kb", [rec1], generation_id=gen1.generation_id)

        # 2. Build generation 2 under new model revision with dim=8
        lin_v2 = IndexLineage(
            chunker_name="fixed",
            chunker_config_hash="cfg2",
            model_name="large_embedder",
            model_revision="v2",
            dimensions=8,
        )
        gen2 = await store.create_generation("ws_prod", "kb", lineage=lin_v2, dimensions=8)
        assert gen2.status == "building"

        rec2 = VectorRecord(
            id="chunk_1",  # Same chunk ID re-indexed under new model
            workspace_id="ws_prod",
            collection_id="kb",
            generation_id=gen2.generation_id,
            vector=[0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            text="Knowledge snippet v2 re-embedded",
        )
        await store.upsert("ws_prod", "kb", [rec2], generation_id=gen2.generation_id)

        # 3. Search on gen1 expects 4 dims, returns gen1 only
        q_gen1 = VectorSearchQuery(
            vector=[1.0, 0.0, 0.0, 0.0],
            collection_id="kb",
            workspace_id="ws_prod",
            generation_id=gen1.generation_id,
        )
        res_g1 = await store.search("ws_prod", q_gen1)
        assert len(res_g1) == 1
        assert res_g1[0].text == "Knowledge snippet v1"
        assert res_g1[0].generation_id == gen1.generation_id

        # 4. Search on gen2 expects 8 dims, returns gen2 only
        q_gen2 = VectorSearchQuery(
            vector=[0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            collection_id="kb",
            workspace_id="ws_prod",
            generation_id=gen2.generation_id,
        )
        res_g2 = await store.search("ws_prod", q_gen2)
        assert len(res_g2) == 1
        assert res_g2[0].text == "Knowledge snippet v2 re-embedded"
        assert res_g2[0].generation_id == gen2.generation_id

        # 5. Default search targets active generation (currently gen1)
        default_q = VectorSearchQuery(
            vector=[1.0, 0.0, 0.0, 0.0],
            collection_id="kb",
            workspace_id="ws_prod",
        )
        def_res = await store.search("ws_prod", default_q)
        assert len(def_res) == 1
        assert def_res[0].generation_id == gen1.generation_id

        # 6. Atomic switch to gen2
        await store.activate_generation("ws_prod", "kb", gen2.generation_id)
        active_gen = await store.get_active_generation("ws_prod", "kb")
        assert active_gen is not None
        assert active_gen.generation_id == gen2.generation_id
        assert active_gen.status == "active"

        # 7. Default search now targets gen2 (expects 8 dims)
        def_res_v2 = await store.search("ws_prod", q_gen2)
        assert len(def_res_v2) == 1
        assert def_res_v2[0].generation_id == gen2.generation_id

        # 8. Retired generation rejects mutations
        with pytest.raises(ImmutableGenerationError):
            await store.upsert("ws_prod", "kb", [rec1], generation_id=gen1.generation_id)


class TestVectorDimensionsValidation:
    """Eager validation of vector dimensions."""

    @pytest.mark.asyncio
    async def test_dimension_mismatch_fails_eagerly(self) -> None:
        store = InMemoryVectorStore()
        await store.create_collection("ws_1", "embeddings", dimensions=4)

        bad_record = VectorRecord(
            id="bad_1",
            workspace_id="ws_1",
            collection_id="embeddings",
            generation_id="",
            vector=[1.0, 2.0],  # 2 dims instead of 4
            text="Bad dimensions",
        )

        with pytest.raises(VectorDimensionMismatchError) as exc_info:
            await store.upsert("ws_1", "embeddings", [bad_record])
        assert exc_info.value.details["expected_dimension"] == 4
        assert exc_info.value.details["actual_dimension"] == 2

        bad_query = VectorSearchQuery(
            vector=[1.0, 2.0, 3.0, 4.0, 5.0],  # 5 dims instead of 4
            collection_id="embeddings",
            workspace_id="ws_1",
        )
        with pytest.raises(VectorDimensionMismatchError) as exc_info2:
            await store.search("ws_1", bad_query)
        assert exc_info2.value.details["expected_dimension"] == 4
        assert exc_info2.value.details["actual_dimension"] == 5


class TestPersistentSqliteVectorStore:
    """Acceptance: actual disposable vector backend integration required in addition to mocks."""

    @pytest.mark.asyncio
    async def test_persistent_sqlite_disk_reopen(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".sqlite3") as tmp_f:
            db_path = tmp_f.name

        # Instance 1: write data
        store1 = SqliteVectorStore(db_path)
        gen = await store1.create_collection("tenant_disk", "docs", dimensions=3)
        rec = VectorRecord(
            id="disk_1",
            workspace_id="tenant_disk",
            collection_id="docs",
            generation_id=gen.generation_id,
            vector=[0.0, 1.0, 0.0],
            text="Disk persistent document text",
            metadata={"status": "indexed", "score": 98},
        )
        await store1.upsert("tenant_disk", "docs", [rec])
        store1.close()

        # Instance 2: reopen and verify query and count survive
        store2 = SqliteVectorStore(db_path)
        active_gen = await store2.get_active_generation("tenant_disk", "docs")
        assert active_gen is not None
        assert active_gen.dimensions == 3
        assert active_gen.record_count == 1

        query = VectorSearchQuery(
            vector=[0.0, 1.0, 0.0],
            collection_id="docs",
            workspace_id="tenant_disk",
            filter={"status": "indexed", "score": {"$gte": 90}},
        )
        results = await store2.search("tenant_disk", query)
        assert len(results) == 1
        assert results[0].id == "disk_1"
        assert results[0].score == 1.0
        assert results[0].metadata["score"] == 98

        store2.close()
        Path(db_path).unlink(missing_ok=True)


class TestModuleContractsAndRegistry:
    """B00/B01 module contract and registry invocation."""

    @pytest.mark.asyncio
    async def test_vector_store_module_invoke(self) -> None:
        mod = VectorStoreModule()
        ctx = ExecutionContext(run_id="run_vector_mod", node_id="vec_step", workspace_id="ws_mod")

        # 1. Create collection
        c_res = await mod.invoke(
            ctx,
            inputs={"operation": "create_collection", "collection_id": "test_col", "dimensions": 4},
        )
        assert c_res.status == "success"

        # 2. Upsert
        u_res = await mod.invoke(
            ctx,
            inputs={
                "operation": "upsert",
                "collection_id": "test_col",
                "records": [
                    {
                        "id": "item_1",
                        "vector": [1.0, 0.0, 0.0, 0.0],
                        "text": "Module test snippet",
                        "metadata": {"tag": "demo"},
                    }
                ],
            },
        )
        assert u_res.status == "success"
        assert u_res.outputs["count"] == 1

        # 3. Search
        s_res = await mod.invoke(
            ctx,
            inputs={
                "operation": "search",
                "collection_id": "test_col",
                "vector": [1.0, 0.0, 0.0, 0.0],
                "top_k": 5,
            },
        )
        assert s_res.status == "success"
        assert len(s_res.outputs["results"]) == 1
        assert s_res.outputs["results"][0]["id"] == "item_1"

    def test_vector_module_registered_in_default_registry(self) -> None:
        assert default_registry.has("nexura.knowledge.vector")
