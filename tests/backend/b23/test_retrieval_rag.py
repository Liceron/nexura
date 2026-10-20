"""Unit and acceptance tests for Module B23: Retrieval, Reranking, and Citation-Backed RAG."""

from __future__ import annotations

import pytest
from nexura_runtime.contracts.context import ExecutionContext

from nexura_runtime.knowledge.chunks import TextChunk
from nexura_runtime.knowledge.retrieval import (
    ContextBudgetExceededError,
    ContextPacker,
    FailingReranker,
    RagModule,
    RagPipeline,
    RagQuery,
    RerankerFailureError,
    RetrievedItem,
    create_retrieval_tool,
)
from nexura_runtime.knowledge.vector import InMemoryVectorStore
from nexura_runtime.modules.registry import default_registry


@pytest.fixture
def sample_chunks() -> list[TextChunk]:
    return [
        TextChunk.create(
            document_id="policy_remote.pdf",
            chunk_index=0,
            text="Remote Work Policy: Eligible employees may work remotely up to two days weekly.",
            start_char=0,
            end_char=77,
            token_count=16,
            page_start=1,
            page_end=1,
            metadata={"origin": "policy_remote.pdf", "category": "hr"},
        ),
        TextChunk.create(
            document_id="policy_remote.pdf",
            chunk_index=1,
            text="Remote equipment allowance provides up to $500 for home office setup expenses.",
            start_char=78,
            end_char=157,
            token_count=15,
            page_start=2,
            page_end=2,
            metadata={"origin": "policy_remote.pdf", "category": "finance"},
        ),
        TextChunk.create(
            document_id="security_guide.pdf",
            chunk_index=0,
            text="Security Guide: All production infrastructure access requires hardware MFA keys.",
            start_char=0,
            end_char=80,
            token_count=14,
            page_start=1,
            page_end=1,
            metadata={"origin": "security_guide.pdf", "category": "security"},
        ),
    ]


class TestCitationIntegrity:
    """Acceptance: Gold synthetic fixtures check retrieval and citation integrity."""

    @pytest.mark.asyncio
    async def test_citation_integrity_and_page_mapping(
        self,
        sample_chunks: list[TextChunk],
    ) -> None:
        pipeline = RagPipeline()
        await pipeline.index_chunks("ws_test", "docs", sample_chunks)

        query = RagQuery(
            query="remote equipment allowance",
            collection_id="docs",
            workspace_id="ws_test",
            top_k=3,
        )
        resp = await pipeline.answer(query)

        assert resp.has_sufficient_evidence
        assert len(resp.citations) >= 1

        top_citation = resp.citations[0]
        assert top_citation.document_id == "policy_remote.pdf"
        assert top_citation.origin == "policy_remote.pdf"
        assert top_citation.page_start == 2
        assert "500" in top_citation.snippet
        assert top_citation.citation_id == "[1]"


class TestPermissionAndDeletionExclusion:
    """Acceptance: deleted/restricted docs never appear."""

    @pytest.mark.asyncio
    async def test_deleted_docs_never_appear(
        self,
        sample_chunks: list[TextChunk],
    ) -> None:
        store = InMemoryVectorStore()
        pipeline = RagPipeline(vector_store=store)
        await pipeline.index_chunks("ws_test", "docs", sample_chunks)

        # Soft delete chunk 1 (equipment allowance)
        chunk_to_delete = sample_chunks[1].chunk_id
        await store.delete("ws_test", "docs", [chunk_to_delete], hard_delete=False)

        query = RagQuery(
            query="remote equipment allowance",
            collection_id="docs",
            workspace_id="ws_test",
            top_k=5,
        )
        packed = await pipeline.retrieve(query)

        retrieved_ids = [c.chunk_id for c in packed.citations]
        assert chunk_to_delete not in retrieved_ids

    @pytest.mark.asyncio
    async def test_restricted_documents_never_appear(
        self,
        sample_chunks: list[TextChunk],
    ) -> None:
        pipeline = RagPipeline()
        await pipeline.index_chunks("ws_test", "docs", sample_chunks)

        # Query restricting allowed documents strictly to 'security_guide.pdf'
        query = RagQuery(
            query="remote equipment and work days",
            collection_id="docs",
            workspace_id="ws_test",
            allowed_document_ids=["security_guide.pdf"],
            top_k=5,
        )
        packed = await pipeline.retrieve(query)

        for c in packed.citations:
            assert c.document_id == "security_guide.pdf"
            assert c.document_id != "policy_remote.pdf"


class TestEmptyRetrievalEvidenceBounds:
    """Acceptance: empty retrieval does not fabricate evidence."""

    @pytest.mark.asyncio
    async def test_empty_retrieval_signals_insufficient_evidence(self) -> None:
        pipeline = RagPipeline()
        # Empty collection
        await pipeline.vector_store.create_collection("ws_empty", "empty_col", dimensions=8)

        query = RagQuery(
            query="What is the quantum telemetry bandwidth limit?",
            collection_id="empty_col",
            workspace_id="ws_empty",
        )
        resp = await pipeline.answer(query)

        assert not resp.has_sufficient_evidence
        assert len(resp.citations) == 0
        assert "No relevant documents found" in resp.answer
        assert not resp.packed_context.has_sufficient_evidence


class TestContextBudgetEnforcement:
    """Acceptance: context budget enforced."""

    @pytest.mark.asyncio
    async def test_context_budget_bounds_tokens(
        self,
        sample_chunks: list[TextChunk],
    ) -> None:
        pipeline = RagPipeline()
        await pipeline.index_chunks("ws_test", "docs", sample_chunks)

        # Restrict context budget to allow only ~1 snippet
        query = RagQuery(
            query="policy remote work security equipment",
            collection_id="docs",
            workspace_id="ws_test",
            max_context_tokens=65,  # Very tight budget
            top_k=5,
        )
        packed = await pipeline.retrieve(query)

        assert packed.total_tokens <= 65
        assert len(packed.items) <= 2
        assert packed.truncated

    def test_context_budget_too_small_raises(self) -> None:
        packer = ContextPacker()
        item = RetrievedItem(
            chunk_id="chk_big",
            document_id="doc_big",
            text="A" * 500,
            score=0.9,
            token_count=100,
        )
        # Budget cannot fit header + 1 snippet
        with pytest.raises(ContextBudgetExceededError):
            packer.pack([item], max_context_tokens=5)


class TestRerankerFailureBehavior:
    """Acceptance: ranker failure has explicit configured behavior."""

    @pytest.mark.asyncio
    async def test_ranker_failure_fallback_to_fusion(
        self,
        sample_chunks: list[TextChunk],
    ) -> None:
        failing_reranker = FailingReranker("Network timeout to cross-encoder")
        pipeline = RagPipeline(
            reranker=failing_reranker,
            on_rerank_failure="fallback_to_fusion",
        )
        await pipeline.index_chunks("ws_test", "docs", sample_chunks)

        query = RagQuery(
            query="remote work policy",
            collection_id="docs",
            workspace_id="ws_test",
            rerank=True,
        )
        # Should not raise, falls back gracefully to fusion scores
        packed = await pipeline.retrieve(query)
        assert packed.has_sufficient_evidence
        assert len(packed.items) > 0

    @pytest.mark.asyncio
    async def test_ranker_failure_raise_policy(
        self,
        sample_chunks: list[TextChunk],
    ) -> None:
        failing_reranker = FailingReranker("Model endpoint disconnected")
        pipeline = RagPipeline(
            reranker=failing_reranker,
            on_rerank_failure="raise",
        )
        await pipeline.index_chunks("ws_test", "docs", sample_chunks)

        query = RagQuery(
            query="remote work policy",
            collection_id="docs",
            workspace_id="ws_test",
            rerank=True,
        )
        with pytest.raises(RerankerFailureError) as exc_info:
            await pipeline.retrieve(query)

        assert "Model endpoint disconnected" in str(exc_info.value)


class TestRetrievalAsTool:
    """Retrieval-as-tool integration."""

    @pytest.mark.asyncio
    async def test_create_retrieval_tool(
        self,
        sample_chunks: list[TextChunk],
    ) -> None:
        pipeline = RagPipeline()
        await pipeline.index_chunks("ws_test", "docs", sample_chunks)

        tool = create_retrieval_tool(
            pipeline=pipeline,
            collection_id="docs",
            workspace_id="ws_test",
            name="search_kb",
        )
        assert tool.name == "search_kb"

        # Execute tool function
        result_text = await tool.execute({"query": "hardware MFA keys"})
        assert "security_guide.pdf" in result_text
        assert "[1]" in result_text


class TestModuleContractAndRegistry:
    """B00/B01 module contract and registry integration."""

    @pytest.mark.asyncio
    async def test_rag_module_invoke(
        self,
        sample_chunks: list[TextChunk],
    ) -> None:
        pipeline = RagPipeline()
        await pipeline.index_chunks("ws_mod", "kb_col", sample_chunks)

        mod = RagModule(pipeline=pipeline)
        ctx = ExecutionContext(run_id="run_rag_mod", node_id="rag_step", workspace_id="ws_mod")

        res = await mod.invoke(
            ctx,
            inputs={
                "query": "remote equipment expenses",
                "collection_id": "kb_col",
                "generate_answer": True,
            },
        )
        assert res.status == "success"
        assert res.outputs["has_sufficient_evidence"]
        assert len(res.outputs["citations"]) >= 1
        assert "500" in res.outputs["answer"] or "equipment" in res.outputs["answer"]

    def test_rag_module_registered_in_default_registry(self) -> None:
        assert default_registry.has("nexura.knowledge.retrieval")
