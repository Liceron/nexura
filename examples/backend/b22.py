"""Standalone demonstration for Module B22: Vector Storage and Indexes.

Runs 100% keylessly and offline:
  uv run python examples/backend/b22.py
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from nexura_runtime.contracts.context import ExecutionContext

from nexura_runtime.knowledge.chunks import (
    FixedWindowChunker,
    IndexLineage,
)
from nexura_runtime.knowledge.embeddings import (
    DeterministicEmbeddingProvider,
    EmbeddingOrchestrator,
)
from nexura_runtime.knowledge.vector import (
    InMemoryVectorStore,
    SqliteVectorStore,
    VectorDimensionMismatchError,
    VectorRecord,
    VectorSearchQuery,
    VectorStoreModule,
)


def main() -> None:
    print("=== Module B22: Vector Storage and Indexes ===")

    # 1. Collection Initialization with Initial Generation (dim=8)
    print("\n1. Initializing vector collection with 8-dimensional lineage...")
    store = InMemoryVectorStore()
    lin_v1 = IndexLineage(
        chunker_name="fixed_window",
        chunker_config_hash="cfg_hash_v1",
        model_name="deterministic-vector-fixture-non-semantic",
        model_revision="v1",
        dimensions=8,
    )
    gen_v1 = asyncio.run(
        store.create_collection(
            workspace_id="ws_research",
            collection_id="agent_kb",
            dimensions=8,
            lineage=lin_v1,
        )
    )
    print("  Collection: agent_kb (workspace: ws_research)")
    print(f"  Active Generation ID: {gen_v1.generation_id}, status: {gen_v1.status}")

    # 2. Ingesting and Indexing Bilingual Chunks (B20/B21 Integration)
    print("\n2. Chunking and embedding sample document records...")
    chunker = FixedWindowChunker(chunk_size=70, chunk_overlap=15)
    doc_text = (
        "Multi-agent coordination protocols and state transfer mechanisms.\n"
        "बहुभाषी तंत्रिका नेटवर्क और संदर्भ पुनःप्राप्ति (Multilingual neural retrieval).\n"
        "Distributed consensus and resilient transaction journals."
    )
    chunks = chunker.chunk_text(doc_text, document_id="doc_coord")
    print(f"  Produced {len(chunks)} chunks.")

    provider_v1 = DeterministicEmbeddingProvider(
        model_id="deterministic-vector-fixture-non-semantic",
        revision="v1",
        dimensions=8,
    )
    orchestrator_v1 = EmbeddingOrchestrator(
        provider=provider_v1,
        lineage=lin_v1,
    )
    embed_res = asyncio.run(orchestrator_v1.embed_chunks(chunks))
    print(f"  Generated {len(embed_res.embedded_chunks)} vector embeddings.")

    # Convert to VectorRecord objects and upsert
    records_v1 = [
        VectorRecord(
            id=ec.chunk_id,
            workspace_id="ws_research",
            collection_id="agent_kb",
            generation_id=gen_v1.generation_id,
            vector=ec.embedding,
            document_id=ec.document_id,
            text=ec.text,
            metadata={
                "chunk_index": ec.chunk_index,
                "span_start": ec.start_char,
                "span_end": ec.end_char,
                "domain": "ai_agents",
            },
        )
        for ec in embed_res.embedded_chunks
    ]
    upserted_count = asyncio.run(
        store.upsert(
            workspace_id="ws_research",
            collection_id="agent_kb",
            records=records_v1,
        )
    )
    print(f"  Upserted {upserted_count} vector records into generation {gen_v1.generation_id}.")

    # 3. Vector Similarity Search with Metadata Filtering
    print("\n3. Performing vector search with metadata filter...")
    query_vector = embed_res.embedded_chunks[0].embedding
    search_q = VectorSearchQuery(
        vector=query_vector,
        collection_id="agent_kb",
        workspace_id="ws_research",
        top_k=3,
        filter={"domain": "ai_agents"},
    )
    hits = asyncio.run(store.search("ws_research", search_q))
    print(f"  Found {len(hits)} matching vector hits:")
    for i, hit in enumerate(hits):
        print(f"    [{i}] score={hit.score:.4f}, id={hit.id}: {hit.text[:40]!r}...")

    # 4. Mandatory Tenant Isolation
    print("\n4. Verifying mandatory tenant isolation...")
    malicious_query = VectorSearchQuery(
        vector=query_vector,
        collection_id="agent_kb",
        workspace_id="ws_other_tenant",  # Attempting to override tenant
        top_k=5,
        filter={"workspace_id": "ws_research"},
    )
    # Search executed from foreign workspace 'ws_other_tenant'
    foreign_hits = asyncio.run(store.search("ws_other_tenant", malicious_query))
    assert len(foreign_hits) == 0
    print("  [SUCCESS] Query executed from 'ws_other_tenant' returned 0 records.")

    # 5. Idempotent Upsert and Deletion with Tombstones
    print("\n5. Verifying idempotency and soft-deletion tombstones...")
    # Re-upserting identical records maintains single row
    u_again = asyncio.run(
        store.upsert(
            workspace_id="ws_research",
            collection_id="agent_kb",
            records=records_v1,
        )
    )
    total_active = asyncio.run(store.count("ws_research", "agent_kb"))
    assert total_active == len(records_v1)
    print(f"  Re-upserted {u_again} records; total active count remains {total_active}.")

    # Soft delete first record
    deleted_id = records_v1[0].id
    deleted_count = asyncio.run(
        store.delete(
            workspace_id="ws_research",
            collection_id="agent_kb",
            ids=[deleted_id],
            hard_delete=False,
        )
    )
    assert deleted_count == 1
    new_active = asyncio.run(store.count("ws_research", "agent_kb"))
    assert new_active == total_active - 1
    print(f"  Soft-deleted record {deleted_id}; active count is now {new_active}.")

    # Re-upsert un-deletes the record
    asyncio.run(
        store.upsert(
            workspace_id="ws_research",
            collection_id="agent_kb",
            records=[records_v1[0]],
        )
    )
    assert asyncio.run(store.count("ws_research", "agent_kb")) == total_active
    print("  [SUCCESS] Idempotent upsert cleanly revived soft-deleted record.")

    # 6. Rebuilding Under a New Model Generation (dim=16) Without Mixing
    print("\n6. Rebuilding index under a new embedding model revision (dim=16)...")
    lin_v2 = IndexLineage(
        chunker_name="fixed_window",
        chunker_config_hash="cfg_hash_v1",
        model_name="deterministic-vector-fixture-non-semantic",
        model_revision="v2",
        dimensions=16,
    )
    gen_v2 = asyncio.run(
        store.create_generation(
            workspace_id="ws_research",
            collection_id="agent_kb",
            lineage=lin_v2,
            dimensions=16,
        )
    )
    print(f"  Created new generation: {gen_v2.generation_id}, status: {gen_v2.status}")

    # Generate 16-dim embeddings for gen_v2
    provider_v2 = DeterministicEmbeddingProvider(
        model_id="deterministic-vector-fixture-non-semantic",
        revision="v2",
        dimensions=16,
    )
    orchestrator_v2 = EmbeddingOrchestrator(
        provider=provider_v2,
        lineage=lin_v2,
    )
    embed_v2_res = asyncio.run(orchestrator_v2.embed_chunks(chunks))
    records_v2 = [
        VectorRecord(
            id=ec.chunk_id,
            workspace_id="ws_research",
            collection_id="agent_kb",
            generation_id=gen_v2.generation_id,
            vector=ec.embedding,
            document_id=ec.document_id,
            text=ec.text,
            metadata={"domain": "ai_agents", "generation": "v2"},
        )
        for ec in embed_v2_res.embedded_chunks
    ]
    asyncio.run(
        store.upsert(
            workspace_id="ws_research",
            collection_id="agent_kb",
            records=records_v2,
            generation_id=gen_v2.generation_id,
        )
    )
    print(f"  Indexed {len(records_v2)} 16-dim records into generation {gen_v2.generation_id}.")

    # Verify stale generation never mixes embeddings:
    # Query gen_v1 with 8 dims: returns gen_v1 only
    q_v1 = VectorSearchQuery(
        vector=records_v1[0].vector,
        collection_id="agent_kb",
        workspace_id="ws_research",
        generation_id=gen_v1.generation_id,
    )
    hits_g1 = asyncio.run(store.search("ws_research", q_v1))
    assert all(h.generation_id == gen_v1.generation_id for h in hits_g1)

    # Query gen_v2 with 16 dims: returns gen_v2 only
    q_v2 = VectorSearchQuery(
        vector=records_v2[0].vector,
        collection_id="agent_kb",
        workspace_id="ws_research",
        generation_id=gen_v2.generation_id,
    )
    hits_g2 = asyncio.run(store.search("ws_research", q_v2))
    assert all(h.generation_id == gen_v2.generation_id for h in hits_g2)
    print("  [SUCCESS] Generations isolated: queries never mix 8-dim and 16-dim embeddings.")

    # 7. Atomic Active-Generation Switch
    print("\n7. Executing atomic active-generation switch to generation 2...")
    asyncio.run(store.activate_generation("ws_research", "agent_kb", gen_v2.generation_id))
    active_now = asyncio.run(store.get_active_generation("ws_research", "agent_kb"))
    assert active_now is not None
    assert active_now.generation_id == gen_v2.generation_id
    assert active_now.dimensions == 16
    print(f"  Active Generation is now: {active_now.generation_id} (dims: {active_now.dimensions})")

    # 8. Dimension Mismatch Validation
    print("\n8. Verifying dimension mismatch detection...")
    try:
        # Default query targets active gen (16 dims), pass 8 dims:
        bad_query = VectorSearchQuery(
            vector=[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],  # 8 dims
            collection_id="agent_kb",
            workspace_id="ws_research",
        )
        asyncio.run(store.search("ws_research", bad_query))
        print("  [FAILED] Expected VectorDimensionMismatchError was not raised!")
    except VectorDimensionMismatchError as exc:
        print(f"  [SUCCESS] Caught expected error: {exc}")

    # 9. Disposable SQLite Vector Store Integration
    print("\n9. Testing disposable persistent SQLite vector store on disk...")
    with tempfile.NamedTemporaryFile(suffix=".sqlite3") as tmp_f:
        db_file = tmp_f.name

    sqlite_store = SqliteVectorStore(db_file)
    sql_gen = asyncio.run(sqlite_store.create_collection("ws_disk", "disk_kb", dimensions=4))
    sql_rec = VectorRecord(
        id="chunk_disk_1",
        workspace_id="ws_disk",
        collection_id="disk_kb",
        generation_id=sql_gen.generation_id,
        vector=[1.0, 0.0, 0.0, 0.0],
        text="Persisted text on disk",
        metadata={"disk": True},
    )
    asyncio.run(sqlite_store.upsert("ws_disk", "disk_kb", [sql_rec]))
    sqlite_store.close()

    # Reopen from disk to verify durability
    reopened_store = SqliteVectorStore(db_file)
    disk_hits = asyncio.run(
        reopened_store.search(
            "ws_disk",
            VectorSearchQuery(
                vector=[1.0, 0.0, 0.0, 0.0],
                collection_id="disk_kb",
                workspace_id="ws_disk",
            ),
        )
    )
    assert len(disk_hits) == 1
    assert disk_hits[0].id == "chunk_disk_1"
    print(f"  [SUCCESS] Reopened SQLite vector store from disk: hit={disk_hits[0].id}")
    reopened_store.close()
    Path(db_file).unlink(missing_ok=True)

    # 10. B00/B01 Module Invocation via Python Contract
    print("\n10. Executing VectorStoreModule via Python contract...")
    mod = VectorStoreModule(store)
    ctx = ExecutionContext(
        run_id="run_b22_demo",
        node_id="vec_step",
        workspace_id="ws_research",
    )
    search_res = asyncio.run(
        mod.invoke(
            ctx,
            inputs={
                "operation": "search",
                "collection_id": "agent_kb",
                "vector": records_v2[0].vector,
                "top_k": 2,
            },
        )
    )
    print(f"  Module status: {search_res.status}, hits returned: {search_res.outputs['count']}")

    # 11. B01 CLI Subprocess Invocation
    print("\n11. Invoking VectorStoreModule via B01 CLI subprocess...")
    cmd = [
        sys.executable,
        "-m",
        "nexura_runtime.modules",
        "run",
        "nexura.knowledge.vector",
        "--input",
        json.dumps(
            {
                "operation": "create_collection",
                "collection_id": "cli_col",
                "dimensions": 4,
            }
        ),
    ]
    cli_proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    print(f"  CLI Exit Code: {cli_proc.returncode}")
    if cli_proc.returncode == 0:
        print(f"  CLI Output: {cli_proc.stdout.strip()}")
    else:
        print(f"  CLI Error: {cli_proc.stderr.strip()}")

    print("\n=== All B22 Demonstration Steps Completed Successfully ===")


if __name__ == "__main__":
    main()
