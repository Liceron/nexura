"""Integration and negative tests for PostgreSQL pgvector and Qdrant vector adapters."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

import pytest
from nexura_postgres import PgVectorStoreAdapter
from nexura_qdrant import QdrantVectorStoreAdapter

from nexura_runtime.knowledge.chunks.models import IndexLineage
from nexura_runtime.knowledge.vector.errors import (
    CollectionNotFoundError,
    GenerationNotFoundError,
    ImmutableGenerationError,
    TenantIsolationViolationError,
    VectorDimensionMismatchError,
    VectorStoreError,
)
from nexura_runtime.knowledge.vector.models import (
    VectorRecord,
    VectorSearchQuery,
)
from nexura_runtime.knowledge.vector.store import VectorStore


def _load_infrastructure_config() -> dict[str, Any]:
    infra_file = Path(__file__).resolve().parents[3] / ".agy" / "review-infrastructure.json"
    if infra_file.exists():
        try:
            data = json.loads(infra_file.read_text())
            if isinstance(data, dict):
                return dict(data)
        except Exception:
            pass

    return {
        "postgres_uri": os.environ.get("NEXURA_TEST_POSTGRES_URI"),
        "qdrant_url": os.environ.get("NEXURA_TEST_QDRANT_URL"),
    }


INFRA = _load_infrastructure_config()
POSTGRES_URI = INFRA.get("postgres_uri")
QDRANT_URL = INFRA.get("qdrant_url")


def _get_adapter(adapter_name: str) -> Any:
    if adapter_name == "pgvector":
        if not POSTGRES_URI:
            pytest.skip("PostgreSQL test service not available")
        return PgVectorStoreAdapter(dsn=POSTGRES_URI)
    elif adapter_name == "qdrant":
        if not QDRANT_URL:
            pytest.skip("Qdrant test service not available")
        return QdrantVectorStoreAdapter(url=QDRANT_URL)
    raise ValueError(f"Unknown adapter {adapter_name}")


ADAPTER_PARAMS = ["pgvector", "qdrant"]


class TestProtocolAndAvailability:
    """Verify VectorStore protocol compliance and availability."""

    def test_protocol_conformance(self) -> None:
        if POSTGRES_URI:
            pg = PgVectorStoreAdapter(dsn=POSTGRES_URI)
            assert isinstance(pg, VectorStore)
            assert pg.is_available is True
        if QDRANT_URL:
            qd = QdrantVectorStoreAdapter(url=QDRANT_URL)
            assert isinstance(qd, VectorStore)
            assert qd.is_available is True

    def test_missing_dependency_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nexura_runtime.contracts.errors import MissingDependencyError

        monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
        with pytest.raises(MissingDependencyError) as exc_pg:
            PgVectorStoreAdapter()
        assert "asyncpg" in str(exc_pg.value)

        with pytest.raises(MissingDependencyError) as exc_qd:
            QdrantVectorStoreAdapter()
        assert "qdrant-client" in str(exc_qd.value)


class TestNegativeValidations:
    """Verify negative validation of dimensions, filters, scoping, and stale generations."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("adapter_name", ADAPTER_PARAMS)
    async def test_dimension_validation(self, adapter_name: str) -> None:
        adapter = _get_adapter(adapter_name)
        ws_id = f"ws_neg_dim_{uuid.uuid4().hex[:8]}"
        col_id = "test_col"
        try:
            # Dimension <= 0 on create_collection
            with pytest.raises(VectorDimensionMismatchError) as exc1:
                await adapter.create_collection(ws_id, col_id, dimensions=0)
            assert exc1.value.details["expected_dimension"] == 1
            assert exc1.value.details["actual_dimension"] == 0

            # Create valid collection with dim=3
            gen = await adapter.create_collection(ws_id, col_id, dimensions=3)

            # Dimension <= 0 on create_generation
            lin = IndexLineage(
                chunker_name="fix",
                chunker_config_hash="h",
                model_name="emb",
                model_revision="v1",
                dimensions=0,
            )
            with pytest.raises(VectorDimensionMismatchError):
                await adapter.create_generation(ws_id, col_id, lineage=lin, dimensions=0)

            # Upsert vector with wrong dimension (2 instead of 3)
            bad_rec = VectorRecord(
                id="rec_bad_dim",
                workspace_id=ws_id,
                collection_id=col_id,
                generation_id=gen.generation_id,
                vector=[1.0, 2.0],
                text="Wrong dims",
            )
            with pytest.raises(VectorDimensionMismatchError) as exc2:
                await adapter.upsert(ws_id, col_id, [bad_rec])
            assert exc2.value.details["expected_dimension"] == 3
            assert exc2.value.details["actual_dimension"] == 2

            # Search with wrong dimension (4 instead of 3)
            bad_query = VectorSearchQuery(
                vector=[1.0, 2.0, 3.0, 4.0],
                collection_id=col_id,
                workspace_id=ws_id,
            )
            with pytest.raises(VectorDimensionMismatchError) as exc3:
                await adapter.search(ws_id, bad_query)
            assert exc3.value.details["expected_dimension"] == 3
            assert exc3.value.details["actual_dimension"] == 4

        finally:
            await adapter.drop_collection(ws_id, col_id)
            await adapter.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("adapter_name", ADAPTER_PARAMS)
    async def test_tenant_scoping_and_cross_workspace_rejection(self, adapter_name: str) -> None:
        adapter = _get_adapter(adapter_name)
        ws_a = f"ws_scop_a_{uuid.uuid4().hex[:8]}"
        ws_b = f"ws_scop_b_{uuid.uuid4().hex[:8]}"
        col_id = "records"
        try:
            await adapter.create_collection(ws_a, col_id, dimensions=2)

            # 1. Upsert record declaring ws_b into ws_a must be REJECTED, not silently relabeled
            cross_rec = VectorRecord(
                id="cross_1",
                workspace_id=ws_b,  # Belonging to another workspace
                collection_id=col_id,
                generation_id="",
                vector=[1.0, 0.0],
                text="Cross workspace data",
            )
            with pytest.raises(TenantIsolationViolationError):
                await adapter.upsert(ws_a, col_id, [cross_rec])

            # 2. Search query referencing ws_b when executing under ws_a must be REJECTED
            cross_query = VectorSearchQuery(
                vector=[1.0, 0.0],
                collection_id=col_id,
                workspace_id=ws_b,  # Mismatched query reference
            )
            with pytest.raises(TenantIsolationViolationError):
                await adapter.search(ws_a, cross_query)

            # 3. Direct external tenant filter override attempting to filter other workspace
            override_query = VectorSearchQuery(
                vector=[1.0, 0.0],
                collection_id=col_id,
                workspace_id=ws_a,
                filter={"workspace_id": ws_b},  # Malicious external tenant override
            )
            with pytest.raises(TenantIsolationViolationError):
                await adapter.search(ws_a, override_query)

        finally:
            await adapter.drop_collection(ws_a, col_id)
            await adapter.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("adapter_name", ADAPTER_PARAMS)
    async def test_filter_expression_validation_and_unsupported_rejection(
        self, adapter_name: str
    ) -> None:
        adapter = _get_adapter(adapter_name)
        ws_id = f"ws_filt_val_{uuid.uuid4().hex[:8]}"
        col_id = "kb"
        try:
            await adapter.create_collection(ws_id, col_id, dimensions=2)

            # Unsupported operator $regex
            q1 = VectorSearchQuery(
                vector=[1.0, 0.0],
                collection_id=col_id,
                workspace_id=ws_id,
                filter={"title": {"$regex": ".*test.*"}},
            )
            with pytest.raises(VectorStoreError):
                await adapter.search(ws_id, q1)

            # Unsupported top-level operator $where
            q2 = VectorSearchQuery(
                vector=[1.0, 0.0],
                collection_id=col_id,
                workspace_id=ws_id,
                filter={"$where": "this.views > 10"},
            )
            with pytest.raises(VectorStoreError):
                await adapter.search(ws_id, q2)

            # Invalid structure for $and (expects list)
            q3 = VectorSearchQuery(
                vector=[1.0, 0.0],
                collection_id=col_id,
                workspace_id=ws_id,
                filter={"$and": "not_a_list"},
            )
            with pytest.raises(VectorStoreError):
                await adapter.search(ws_id, q3)

            # Invalid structure for $in (expects sequence)
            q4 = VectorSearchQuery(
                vector=[1.0, 0.0],
                collection_id=col_id,
                workspace_id=ws_id,
                filter={"category": {"$in": 42}},
            )
            with pytest.raises(VectorStoreError):
                await adapter.search(ws_id, q4)

        finally:
            await adapter.drop_collection(ws_id, col_id)
            await adapter.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("adapter_name", ADAPTER_PARAMS)
    async def test_not_found_errors(self, adapter_name: str) -> None:
        adapter = _get_adapter(adapter_name)
        ws_id = f"ws_nf_{uuid.uuid4().hex[:8]}"
        try:
            # Collection not found on create_generation
            lin = IndexLineage(
                chunker_name="fix",
                chunker_config_hash="h",
                model_name="emb",
                model_revision="v1",
                dimensions=2,
            )
            with pytest.raises(CollectionNotFoundError):
                await adapter.create_generation(ws_id, "nonexistent", lineage=lin, dimensions=2)

            # Collection not found on upsert
            rec = VectorRecord(
                id="r1",
                workspace_id=ws_id,
                collection_id="nonexistent",
                generation_id="",
                vector=[1.0, 0.0],
            )
            with pytest.raises(CollectionNotFoundError):
                await adapter.upsert(ws_id, "nonexistent", [rec])

            # Generation not found on activate_generation
            await adapter.create_collection(ws_id, "col_real", dimensions=2)
            with pytest.raises(GenerationNotFoundError):
                await adapter.activate_generation(ws_id, "col_real", "gen_fake_999")

            # Generation not found on upsert to specific unknown generation
            with pytest.raises(GenerationNotFoundError):
                await adapter.upsert(ws_id, "col_real", [rec], generation_id="gen_unknown")

        finally:
            await adapter.drop_collection(ws_id, "col_real")
            await adapter.close()


class TestLifecycleAndIsolation:
    """Verify operations, two-workspace isolation, atomic activation, and persistence."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("adapter_name", ADAPTER_PARAMS)
    async def test_two_workspace_complete_isolation(self, adapter_name: str) -> None:
        adapter = _get_adapter(adapter_name)
        ws_a = f"ws_iso_a_{uuid.uuid4().hex[:8]}"
        ws_b = f"ws_iso_b_{uuid.uuid4().hex[:8]}"
        col_id = "documents"
        try:
            # Tenant A setup
            gen_a = await adapter.create_collection(ws_a, col_id, dimensions=3)
            rec_a = VectorRecord(
                id="doc_1",
                workspace_id=ws_a,
                collection_id=col_id,
                generation_id=gen_a.generation_id,
                vector=[1.0, 0.0, 0.0],
                text="Tenant A private memo",
                metadata={"tenant": "A", "level": "secret"},
            )
            await adapter.upsert(ws_a, col_id, [rec_a])

            # Tenant B setup (identical record ID and collection ID)
            gen_b = await adapter.create_collection(ws_b, col_id, dimensions=3)
            rec_b = VectorRecord(
                id="doc_1",
                workspace_id=ws_b,
                collection_id=col_id,
                generation_id=gen_b.generation_id,
                vector=[0.0, 1.0, 0.0],
                text="Tenant B public memo",
                metadata={"tenant": "B", "level": "public"},
            )
            await adapter.upsert(ws_b, col_id, [rec_b])

            # Searches in Tenant A must return doc_1 of Tenant A only
            q_a = VectorSearchQuery(
                vector=[1.0, 0.0, 0.0],
                collection_id=col_id,
                workspace_id=ws_a,
            )
            res_a = await adapter.search(ws_a, q_a)
            assert len(res_a) == 1
            assert res_a[0].id == "doc_1"
            assert res_a[0].text == "Tenant A private memo"
            assert res_a[0].metadata["tenant"] == "A"

            # Searches in Tenant B must return doc_1 of Tenant B only
            q_b = VectorSearchQuery(
                vector=[0.0, 1.0, 0.0],
                collection_id=col_id,
                workspace_id=ws_b,
            )
            res_b = await adapter.search(ws_b, q_b)
            assert len(res_b) == 1
            assert res_b[0].id == "doc_1"
            assert res_b[0].text == "Tenant B public memo"
            assert res_b[0].metadata["tenant"] == "B"

            # Get in Tenant A returns Tenant A record
            get_a = await adapter.get(ws_a, col_id, "doc_1")
            assert get_a is not None
            assert get_a.text == "Tenant A private memo"

            # Counts are isolated
            assert await adapter.count(ws_a, col_id) == 1
            assert await adapter.count(ws_b, col_id) == 1

        finally:
            await adapter.drop_collection(ws_a, col_id)
            await adapter.drop_collection(ws_b, col_id)
            await adapter.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("adapter_name", ADAPTER_PARAMS)
    async def test_idempotency_and_tombstones(self, adapter_name: str) -> None:
        adapter = _get_adapter(adapter_name)
        ws_id = f"ws_idemp_{uuid.uuid4().hex[:8]}"
        col_id = "inventory"
        try:
            await adapter.create_collection(ws_id, col_id, dimensions=2)

            rec = VectorRecord(
                id="item_42",
                workspace_id=ws_id,
                collection_id=col_id,
                generation_id="",
                vector=[0.6, 0.8],
                text="Original description",
                metadata={"qty": 10},
            )

            # 1. Initial upsert
            u1 = await adapter.upsert(ws_id, col_id, [rec])
            assert u1 == 1
            assert await adapter.count(ws_id, col_id) == 1

            # 2. Idempotent duplicate upsert with updated content
            rec_updated = rec.model_copy(
                update={"text": "Updated description", "metadata": {"qty": 15}}
            )
            u2 = await adapter.upsert(ws_id, col_id, [rec_updated])
            assert u2 == 1
            assert await adapter.count(ws_id, col_id) == 1  # Count does not duplicate

            fetched = await adapter.get(ws_id, col_id, "item_42")
            assert fetched is not None
            assert fetched.text == "Updated description"
            assert fetched.metadata["qty"] == 15

            # 3. Soft delete (tombstone)
            d1 = await adapter.delete(ws_id, col_id, ["item_42"], hard_delete=False)
            assert d1 == 1
            assert await adapter.count(ws_id, col_id) == 0
            assert await adapter.get(ws_id, col_id, "item_42") is None

            # 4. Repeated soft delete is idempotent (0 deleted)
            d2 = await adapter.delete(ws_id, col_id, ["item_42"], hard_delete=False)
            assert d2 == 0

            # 5. Re-upsert revives tombstoned record
            u3 = await adapter.upsert(ws_id, col_id, [rec])
            assert u3 == 1
            assert await adapter.count(ws_id, col_id) == 1
            revived = await adapter.get(ws_id, col_id, "item_42")
            assert revived is not None
            assert revived.is_deleted is False

            # 6. Hard delete removes record completely
            d3 = await adapter.delete(ws_id, col_id, ["item_42"], hard_delete=True)
            assert d3 == 1
            assert await adapter.count(ws_id, col_id) == 0

        finally:
            await adapter.drop_collection(ws_id, col_id)
            await adapter.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("adapter_name", ADAPTER_PARAMS)
    async def test_atomic_generation_switching_and_stale_rejection(self, adapter_name: str) -> None:
        adapter = _get_adapter(adapter_name)
        ws_id = f"ws_gen_switch_{uuid.uuid4().hex[:8]}"
        col_id = "catalog"
        try:
            # 1. Gen 1 with dim=3
            lin1 = IndexLineage(
                chunker_name="fixed",
                chunker_config_hash="h1",
                model_name="small_embed",
                model_revision="v1",
                dimensions=3,
            )
            gen1 = await adapter.create_collection(ws_id, col_id, dimensions=3, lineage=lin1)
            assert gen1.status == "active"

            rec1 = VectorRecord(
                id="doc_v1",
                workspace_id=ws_id,
                collection_id=col_id,
                generation_id=gen1.generation_id,
                vector=[1.0, 0.0, 0.0],
                text="Gen 1 snippet",
            )
            await adapter.upsert(ws_id, col_id, [rec1], generation_id=gen1.generation_id)

            # 2. Gen 2 with dim=4 in building status
            lin2 = IndexLineage(
                chunker_name="fixed",
                chunker_config_hash="h2",
                model_name="large_embed",
                model_revision="v2",
                dimensions=4,
            )
            gen2 = await adapter.create_generation(ws_id, col_id, lineage=lin2, dimensions=4)
            assert gen2.status == "building"

            rec2 = VectorRecord(
                id="doc_v2",
                workspace_id=ws_id,
                collection_id=col_id,
                generation_id=gen2.generation_id,
                vector=[0.0, 1.0, 0.0, 0.0],
                text="Gen 2 snippet",
            )
            await adapter.upsert(ws_id, col_id, [rec2], generation_id=gen2.generation_id)

            # 3. Default search hits Gen 1 (expects dim=3)
            q1 = VectorSearchQuery(
                vector=[1.0, 0.0, 0.0],
                collection_id=col_id,
                workspace_id=ws_id,
            )
            res1 = await adapter.search(ws_id, q1)
            assert len(res1) == 1
            assert res1[0].id == "doc_v1"
            assert res1[0].generation_id == gen1.generation_id

            # 4. Atomic activation of Gen 2
            await adapter.activate_generation(ws_id, col_id, gen2.generation_id)
            active_gen = await adapter.get_active_generation(ws_id, col_id)
            assert active_gen is not None
            assert active_gen.generation_id == gen2.generation_id
            assert active_gen.status == "active"
            assert active_gen.dimensions == 4
            assert active_gen.record_count == 1

            # 5. Default search now hits Gen 2 (expects dim=4)
            q2 = VectorSearchQuery(
                vector=[0.0, 1.0, 0.0, 0.0],
                collection_id=col_id,
                workspace_id=ws_id,
            )
            res2 = await adapter.search(ws_id, q2)
            assert len(res2) == 1
            assert res2[0].id == "doc_v2"
            assert res2[0].generation_id == gen2.generation_id

            # 6. Stale generation writes REJECTED with ImmutableGenerationError
            with pytest.raises(ImmutableGenerationError):
                await adapter.upsert(ws_id, col_id, [rec1], generation_id=gen1.generation_id)

        finally:
            await adapter.drop_collection(ws_id, col_id)
            await adapter.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("adapter_name", ADAPTER_PARAMS)
    async def test_metadata_filtering_rich_expressions(self, adapter_name: str) -> None:
        adapter = _get_adapter(adapter_name)
        ws_id = f"ws_filt_rich_{uuid.uuid4().hex[:8]}"
        col_id = "products"
        try:
            gen = await adapter.create_collection(ws_id, col_id, dimensions=2)

            records = [
                VectorRecord(
                    id="p1",
                    workspace_id=ws_id,
                    collection_id=col_id,
                    generation_id=gen.generation_id,
                    vector=[1.0, 0.0],
                    text="Laptop",
                    metadata={"cat": "electronics", "price": 1200, "tags": ["work", "portable"]},
                ),
                VectorRecord(
                    id="p2",
                    workspace_id=ws_id,
                    collection_id=col_id,
                    generation_id=gen.generation_id,
                    vector=[0.9, 0.1],
                    text="Phone",
                    metadata={"cat": "electronics", "price": 800, "tags": ["portable"]},
                ),
                VectorRecord(
                    id="p3",
                    workspace_id=ws_id,
                    collection_id=col_id,
                    generation_id=gen.generation_id,
                    vector=[0.0, 1.0],
                    text="Desk",
                    metadata={"cat": "furniture", "price": 300, "tags": ["office"]},
                ),
            ]
            await adapter.upsert(ws_id, col_id, records)

            # Test 1: equality
            q_eq = VectorSearchQuery(
                vector=[1.0, 0.0],
                collection_id=col_id,
                workspace_id=ws_id,
                filter={"cat": "furniture"},
            )
            r_eq = await adapter.search(ws_id, q_eq)
            assert len(r_eq) == 1
            assert r_eq[0].id == "p3"

            # Test 2: comparison ($gt and $lte)
            q_range = VectorSearchQuery(
                vector=[1.0, 0.0],
                collection_id=col_id,
                workspace_id=ws_id,
                filter={"price": {"$gt": 500, "$lte": 1000}},
            )
            r_range = await adapter.search(ws_id, q_range)
            assert len(r_range) == 1
            assert r_range[0].id == "p2"

            # Test 3: set ($in)
            q_in = VectorSearchQuery(
                vector=[1.0, 0.0],
                collection_id=col_id,
                workspace_id=ws_id,
                filter={"cat": {"$in": ["electronics"]}},
                top_k=5,
            )
            r_in = await adapter.search(ws_id, q_in)
            assert len(r_in) == 2

            # Test 4: logical $and
            q_and = VectorSearchQuery(
                vector=[1.0, 0.0],
                collection_id=col_id,
                workspace_id=ws_id,
                filter={"$and": [{"cat": "electronics"}, {"price": {"$gte": 1000}}]},
            )
            r_and = await adapter.search(ws_id, q_and)
            assert len(r_and) == 1
            assert r_and[0].id == "p1"

            # Test 5: containment ($contains)
            q_cont = VectorSearchQuery(
                vector=[1.0, 0.0],
                collection_id=col_id,
                workspace_id=ws_id,
                filter={"tags": {"$contains": "work"}},
            )
            r_cont = await adapter.search(ws_id, q_cont)
            assert len(r_cont) == 1
            assert r_cont[0].id == "p1"

        finally:
            await adapter.drop_collection(ws_id, col_id)
            await adapter.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("adapter_name", ADAPTER_PARAMS)
    async def test_reopen_authoritative_remote_metadata(self, adapter_name: str) -> None:
        """Verify no local in-memory state is authoritative: reopen restores all remote metadata."""
        adapter1 = _get_adapter(adapter_name)
        ws_id = f"ws_reopen_{uuid.uuid4().hex[:8]}"
        col_id = "persistence_test"
        try:
            # 1. Instance 1 writes data and collection
            gen1 = await adapter1.create_collection(ws_id, col_id, dimensions=3)
            rec = VectorRecord(
                id="doc_persist",
                workspace_id=ws_id,
                collection_id=col_id,
                generation_id=gen1.generation_id,
                vector=[0.0, 1.0, 0.0],
                text="Remote database document",
                metadata={"status": "confirmed", "level": 42},
            )
            await adapter1.upsert(ws_id, col_id, [rec])
            assert await adapter1.count(ws_id, col_id) == 1

            # 2. Close instance 1 completely
            await adapter1.close()

            # 3. Instance 2 created fresh against same remote backend
            adapter2 = _get_adapter(adapter_name)

            # Active generation and counts survived in remote DB
            active_gen = await adapter2.get_active_generation(ws_id, col_id)
            assert active_gen is not None
            assert active_gen.generation_id == gen1.generation_id
            assert active_gen.dimensions == 3
            assert active_gen.record_count == 1

            # Get survives
            retrieved = await adapter2.get(ws_id, col_id, "doc_persist")
            assert retrieved is not None
            assert retrieved.text == "Remote database document"
            assert retrieved.metadata["level"] == 42
            assert retrieved.vector == [0.0, 1.0, 0.0]

            # Search with filter survives
            q = VectorSearchQuery(
                vector=[0.0, 1.0, 0.0],
                collection_id=col_id,
                workspace_id=ws_id,
                filter={"status": "confirmed", "level": {"$gte": 40}},
            )
            res = await adapter2.search(ws_id, q)
            assert len(res) == 1
            assert res[0].id == "doc_persist"
            assert res[0].score == 1.0

            # Delete works on instance 2
            del_count = await adapter2.delete(ws_id, col_id, ["doc_persist"], hard_delete=False)
            assert del_count == 1
            assert await adapter2.count(ws_id, col_id) == 0

            await adapter2.drop_collection(ws_id, col_id)
            await adapter2.close()

        finally:
            # Ensure cleanup if error occurred
            try:
                cleanup_adapter = _get_adapter(adapter_name)
                await cleanup_adapter.drop_collection(ws_id, col_id)
                await cleanup_adapter.close()
            except Exception:
                pass
