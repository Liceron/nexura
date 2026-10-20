"""Standalone demonstration for Module B23: Retrieval, Reranking and Citation-Backed RAG.

Runs 100% keylessly and offline:
  uv run python examples/backend/b23.py
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys

from nexura_runtime.contracts.context import ExecutionContext

from nexura_runtime.knowledge.chunks import TextChunk
from nexura_runtime.knowledge.retrieval import (
    FailingReranker,
    RagModule,
    RagPipeline,
    RagQuery,
    create_retrieval_tool,
)


def create_policy_fixtures() -> list[TextChunk]:
    """Create synthetic enterprise policy chunks with exact character spans."""
    t1 = (
        "Remote Work Policy Section 1: All full-time employees must submit an "
        "annual remote work agreement to their department lead. Working from an "
        "international jurisdiction requires explicit HR and legal compliance approval."
    )
    t2 = (
        "Remote Work Policy Section 2: Home office ergonomics stipend is capped "
        "at $750 per fiscal year. Receipts must be submitted within 30 days of purchase."
    )
    t3 = (
        "InfoSec Standard 2026 Section 4: Hardware multi-factor authentication (MFA) "
        "security keys are mandatory for all production VPN access. SMS verification "
        "is strictly prohibited for infrastructure access."
    )
    t4 = (
        "Board Strategy Memo: Executive compensation packages and planned Q4 restructuring "
        "initiatives remain under NDA."
    )
    return [
        TextChunk.create(
            document_id="policy_remote_work.pdf",
            chunk_index=0,
            text=t1,
            token_count=len(t1.split()),
            page_start=1,
            page_end=1,
            start_char=0,
            end_char=len(t1),
            metadata={"origin": "policy_remote_work.pdf", "category": "hr"},
        ),
        TextChunk.create(
            document_id="policy_remote_work.pdf",
            chunk_index=1,
            text=t2,
            token_count=len(t2.split()),
            page_start=2,
            page_end=2,
            start_char=len(t1) + 1,
            end_char=len(t1) + 1 + len(t2),
            metadata={"origin": "policy_remote_work.pdf", "category": "hr"},
        ),
        TextChunk.create(
            document_id="infosec_standard_2026.pdf",
            chunk_index=0,
            text=t3,
            token_count=len(t3.split()),
            page_start=5,
            page_end=5,
            start_char=410,
            end_char=410 + len(t3),
            metadata={"origin": "infosec_standard_2026.pdf", "category": "security"},
        ),
        TextChunk.create(
            document_id="confidential_board_memo.pdf",
            chunk_index=0,
            text=t4,
            token_count=len(t4.split()),
            page_start=1,
            page_end=1,
            start_char=0,
            end_char=len(t4),
            metadata={"origin": "confidential_board_memo.pdf", "category": "executive"},
        ),
    ]


def main() -> None:
    print("=== Module B23: Retrieval, Reranking and Citation-Backed RAG ===")

    chunks = create_policy_fixtures()
    pipeline = RagPipeline()

    workspace_id = "ws_enterprise"
    collection_id = "policies"

    # 1. Indexing Chunks
    print("\n1. Indexing synthetic enterprise policy chunks...")
    asyncio.run(pipeline.index_chunks(workspace_id, collection_id, chunks))
    print(f"  Indexed {len(chunks)} chunks into '{collection_id}' (workspace: {workspace_id})")

    # 2. Hybrid Retrieval with Citation-Backed Answer
    print("\n2. Executing hybrid retrieval + citation-backed synthesis...")
    query = RagQuery(
        query="What are the requirements for remote work approval and production VPN access?",
        collection_id=collection_id,
        workspace_id=workspace_id,
        top_k=3,
        min_score_threshold=0.1,
        dense_weight=0.6,
        lexical_weight=0.4,
        max_context_tokens=1500,
    )
    response = asyncio.run(pipeline.answer(query))
    print(f"  Has Sufficient Evidence: {response.has_sufficient_evidence}")
    print(f"  Answer: {response.answer}")
    print(f"  Citations Count: {len(response.citations)}")
    for cite in response.citations:
        print(
            f"    - {cite.citation_id} {cite.origin} (pp. {cite.page_start}-{cite.page_end}): "
            f'"{cite.snippet[:60]}..." (chars {cite.start_char}-{cite.end_char})'
        )

    assert response.has_sufficient_evidence is True
    assert len(response.citations) > 0
    assert "[1]" in response.answer

    # 3. Non-Hallucination on Empty / Unmatched Retrieval
    print("\n3. Testing non-hallucination behavior for unknown / unmatched queries...")
    empty_query = RagQuery(
        query="What is the maintenance protocol for deep-sea submarines?",
        collection_id=collection_id,
        workspace_id=workspace_id,
        top_k=3,
        min_score_threshold=0.85,
    )
    empty_response = asyncio.run(pipeline.answer(empty_query))
    print(f"  Has Sufficient Evidence: {empty_response.has_sufficient_evidence}")
    print(f"  Answer: {empty_response.answer}")
    print(f"  Citations Count: {len(empty_response.citations)}")
    assert empty_response.has_sufficient_evidence is False
    assert len(empty_response.citations) == 0
    assert "No relevant documents found" in empty_response.answer

    # 4. Permission / Document Whitelisting Enforcement
    print("\n4. Enforcing document permissions (whitelisting)...")
    restricted_query = RagQuery(
        query="restructuring executive compensation memo",
        collection_id=collection_id,
        workspace_id=workspace_id,
        allowed_document_ids=["policy_remote_work.pdf", "infosec_standard_2026.pdf"],
    )
    restricted_resp = asyncio.run(pipeline.answer(restricted_query))
    print("  Query against confidential doc with non-whitelisted doc id:")
    print(f"  Has Sufficient Evidence: {restricted_resp.has_sufficient_evidence}")
    for cite in restricted_resp.citations:
        assert cite.document_id != "confidential_board_memo.pdf"
    print("  [SUCCESS] Confidential board memo was excluded by permission filter.")

    # 5. Tombstone Deletion Exclusion
    print("\n5. Testing tombstone deletion exclusion...")
    del_text = "Temporary bypass: Use admin password 'admin123' for router access."
    deleted_chunk = TextChunk.create(
        document_id="infosec_standard_2026.pdf",
        chunk_index=99,
        text=del_text,
        start_char=0,
        end_char=len(del_text),
        token_count=len(del_text.split()),
        metadata={"is_deleted": True, "origin": "infosec_standard_2026.pdf"},
    )
    asyncio.run(pipeline.index_chunks(workspace_id, collection_id, [deleted_chunk]))
    bypass_query = RagQuery(
        query="admin password router access",
        collection_id=collection_id,
        workspace_id=workspace_id,
    )
    bypass_resp = asyncio.run(pipeline.answer(bypass_query))
    for cite in bypass_resp.citations:
        assert "admin123" not in cite.snippet
    print("  [SUCCESS] Soft-deleted chunk was omitted from retrieval context.")

    # 6. Reranking and Fallback Policy
    print("\n6. Demonstrating cross-encoder reranker fallback on failure...")
    failing_reranker = FailingReranker()
    pipeline_with_fallback = RagPipeline(
        reranker=failing_reranker,
        on_rerank_failure="fallback_to_fusion",
    )
    asyncio.run(pipeline_with_fallback.index_chunks(workspace_id, collection_id, chunks))
    fallback_resp = asyncio.run(
        pipeline_with_fallback.answer(
            RagQuery(
                query="hardware MFA keys",
                collection_id=collection_id,
                workspace_id=workspace_id,
                rerank=True,
            )
        )
    )
    assert fallback_resp.has_sufficient_evidence is True
    print("  Reranker failed -> successfully fell back to reciprocal rank fusion.")
    print(f"  Top citation: {fallback_resp.citations[0].origin}")

    # 7. Agent Tool Integration
    print("\n7. Exposing RAG pipeline as an Agent Tool...")
    tool = create_retrieval_tool(
        pipeline=pipeline,
        collection_id=collection_id,
        workspace_id=workspace_id,
        name="search_org_knowledge",
    )
    tool_output = asyncio.run(tool.execute({"query": "home office stipend"}))
    print(f"  Tool Name: {tool.name}")
    print(f"  Tool Execution Output snippet:\n{tool_output[:250]}...")
    assert "$750" in tool_output
    assert "[1]" in tool_output

    # 8. B00/B01 Module Contract Invocation
    print("\n8. Executing RagModule via Python contract...")
    module = RagModule(pipeline=pipeline)
    ctx = ExecutionContext(
        run_id="run_b23_demo",
        node_id="rag_step",
        workspace_id=workspace_id,
    )
    mod_res = asyncio.run(
        module.invoke(
            ctx,
            inputs={
                "query": "remote work agreements department lead",
                "collection_id": collection_id,
            },
        )
    )
    print(f"  Module Status: {mod_res.status}")
    print(f"  Citations Returned: {len(mod_res.outputs['citations'])}")
    print(f"  Total Tokens: {mod_res.outputs['total_tokens']}")
    assert mod_res.status == "success"
    assert mod_res.outputs["has_sufficient_evidence"] is True

    # 9. B01 CLI Subprocess Invocation
    print("\n9. Invoking RagModule via B01 CLI subprocess...")
    cmd = [
        sys.executable,
        "-m",
        "nexura_runtime.modules",
        "run",
        "nexura.knowledge.retrieval",
        "--input",
        json.dumps({"query": "verify CLI invocation"}),
    ]
    cli_proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    print(f"  CLI Exit Code: {cli_proc.returncode}")
    print(f"  CLI Output: {cli_proc.stdout.strip()}")
    assert cli_proc.returncode == 0
    cli_json = json.loads(cli_proc.stdout)
    assert cli_json["status"] == "success"

    print("\n=== All B23 Demonstration Steps Completed Successfully ===")


if __name__ == "__main__":
    main()
