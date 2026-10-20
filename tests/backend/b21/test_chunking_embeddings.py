"""Acceptance and unit tests for B21: Chunking, Embeddings, and Token Budgets."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from nexura_runtime.contracts.context import ExecutionContext
from nexura_runtime.providers.errors import StreamCancelledError

from nexura_runtime.knowledge.chunks import (
    FixedWindowChunker,
    HeuristicTokenEstimator,
    IndexLineage,
    InvalidChunkerConfigError,
    MarkdownChunker,
    RecursiveCharacterChunker,
    TextChunk,
    TokenLimitExceededError,
)
from nexura_runtime.knowledge.chunks.module import ChunkingModule
from nexura_runtime.knowledge.embeddings import (
    DeterministicEmbeddingProvider,
    DimensionMismatchError,
    EmbeddingBudget,
    EmbeddingBudgetExhaustedError,
    EmbeddingModule,
    EmbeddingOrchestrator,
    InMemoryEmbeddingCache,
    SqliteEmbeddingCache,
)
from nexura_runtime.knowledge.ingest.models import Document, DocumentPage
from nexura_runtime.modules.registry import default_registry


class TestIndexLineage:
    """Acceptance: Changing chunker/model revision creates a distinct index lineage."""

    def test_lineage_distinct_across_chunker_configs(self) -> None:
        chunker1 = FixedWindowChunker(chunk_size=200, chunk_overlap=20)
        chunker2 = FixedWindowChunker(chunk_size=300, chunk_overlap=30)
        chunker3 = RecursiveCharacterChunker(chunk_size=200, chunk_overlap=20)

        lin1 = IndexLineage(
            chunker_name=chunker1.chunker_name,
            chunker_config_hash=chunker1.config_hash,
            model_name="deterministic-fixture",
            model_revision="v1",
            dimensions=8,
        )
        lin2 = IndexLineage(
            chunker_name=chunker2.chunker_name,
            chunker_config_hash=chunker2.config_hash,
            model_name="deterministic-fixture",
            model_revision="v1",
            dimensions=8,
        )
        lin3 = IndexLineage(
            chunker_name=chunker3.chunker_name,
            chunker_config_hash=chunker3.config_hash,
            model_name="deterministic-fixture",
            model_revision="v1",
            dimensions=8,
        )

        assert lin1.lineage_id != lin2.lineage_id
        assert lin1.lineage_id != lin3.lineage_id

    def test_lineage_distinct_across_model_revisions_and_dimensions(self) -> None:
        base_lin = IndexLineage(
            chunker_name="fixed_window",
            chunker_config_hash="abc",
            model_name="text-embedding-v1",
            model_revision="2024-01",
            dimensions=8,
        )
        new_rev_lin = IndexLineage(
            chunker_name="fixed_window",
            chunker_config_hash="abc",
            model_name="text-embedding-v1",
            model_revision="2024-06",
            dimensions=8,
        )
        new_dim_lin = IndexLineage(
            chunker_name="fixed_window",
            chunker_config_hash="abc",
            model_name="text-embedding-v1",
            model_revision="2024-01",
            dimensions=16,
        )

        assert base_lin.lineage_id != new_rev_lin.lineage_id
        assert base_lin.lineage_id != new_dim_lin.lineage_id
        assert not base_lin.is_compatible_with(new_rev_lin)
        assert not base_lin.is_compatible_with(new_dim_lin)


class TestUnicodeOffsetsAndOverlap:
    """Acceptance: Unicode offsets and overlap verified."""

    def test_bilingual_fixed_window_offsets_and_overlap(self) -> None:
        # Multilingual text with English, Hindi, Japanese, and emojis
        text = (
            "Hello world! This is an introduction to multilingual retrieval.\n"
            "कृत्रिम बुद्धिमत्ता और तंत्रिका नेटवर्क का परिचय।\n"
            "機械学習モデルと言語処理の仕組みについて。\n"
            "Autonomous agents are evolving rapidly 🚀✨🤖 and shaping the future."
        )

        chunker = FixedWindowChunker(chunk_size=60, chunk_overlap=15)
        chunks = chunker.chunk_text(text, document_id="doc_bilingual")

        assert len(chunks) > 1

        for i, chunk in enumerate(chunks):
            # Verify exact character slicing matches chunk.text
            sliced = text[chunk.start_char : chunk.end_char]
            assert sliced == chunk.text, f"Offset mismatch in chunk {i}"

            # Verify consecutive chunk overlap
            if i > 0:
                prev_chunk = chunks[i - 1]
                expected_start = prev_chunk.end_char - chunker.chunk_overlap
                assert chunk.start_char == expected_start, (
                    f"Overlap mismatch: chunk {i} start={chunk.start_char}, "
                    f"expected={expected_start}"
                )

    def test_recursive_character_chunker_offsets(self) -> None:
        text = (
            "Paragraph one describes architecture fundamentals.\n\n"
            "Paragraph two introduces indexing algorithms:\n"
            "- Step 1: Ingestion\n"
            "- Step 2: Parsing\n"
            "- Step 3: Chunking\n\n"
            "Paragraph three concludes with vector embeddings and distance metrics."
        )

        chunker = RecursiveCharacterChunker(chunk_size=100, chunk_overlap=20)
        chunks = chunker.chunk_text(text, document_id="doc_recursive")

        assert len(chunks) >= 2
        for i, chunk in enumerate(chunks):
            sliced = text[chunk.start_char : chunk.end_char]
            assert sliced == chunk.text, f"Offset mismatch in chunk {i}"

    def test_markdown_chunker_breadcrumbs_and_offsets(self) -> None:
        md_text = (
            "# Agent Architecture\n\n"
            "This document describes the high-level architecture.\n\n"
            "## Storage Subsystem\n\n"
            "We use transactional SQLite for local metadata.\n\n"
            "### Vector Cache\n\n"
            "Vectors are stored keyed by lineage and content hash."
        )

        chunker = MarkdownChunker(chunk_size=120, chunk_overlap=20)
        chunks = chunker.chunk_text(md_text, document_id="doc_md")

        assert len(chunks) >= 3
        # First chunk should have Agent Architecture heading
        assert chunks[0].metadata.get("headings") == ["Agent Architecture"]
        # Later chunk under Vector Cache should include hierarchy
        last_chunk = chunks[-1]
        assert "Vector Cache" in last_chunk.metadata.get("headings", [])
        for chunk in chunks:
            assert md_text[chunk.start_char : chunk.end_char] == chunk.text

    def test_token_estimation_non_zero_for_unicode(self) -> None:
        estimator = HeuristicTokenEstimator()
        assert estimator.estimate_tokens("Hello world") >= 2
        assert estimator.estimate_tokens("नमस्ते दुनिया") >= 4
        assert estimator.estimate_tokens("こんにちは世界") >= 4
        assert estimator.estimate_tokens("🚀🤖✨") >= 6
        assert estimator.estimate_tokens("") == 0

    def test_token_limit_exceeded_error(self) -> None:
        chunker = FixedWindowChunker(
            chunk_size=100,
            chunk_overlap=10,
            max_chunk_tokens=5,  # Very low token limit
        )
        with pytest.raises(TokenLimitExceededError):
            chunker.chunk_text("This text contains substantially more than five tokens.")

    def test_invalid_chunker_config_fails(self) -> None:
        with pytest.raises(InvalidChunkerConfigError):
            FixedWindowChunker(chunk_size=50, chunk_overlap=50)

        with pytest.raises(InvalidChunkerConfigError):
            FixedWindowChunker(chunk_size=0, chunk_overlap=0)


class TestEmbeddingAndDimensionsMismatch:
    """Acceptance: dimensions mismatch fails."""

    @pytest.mark.asyncio
    async def test_dimension_mismatch_fails(self) -> None:
        # Provider returns 16 dimensions, but lineage expects 8
        provider = DeterministicEmbeddingProvider(dimensions=16)
        lineage = IndexLineage(
            chunker_name="fixed",
            chunker_config_hash="abc",
            model_name="deterministic-vector-fixture-non-semantic",
            model_revision="v1",
            dimensions=8,  # Expects 8!
        )
        orchestrator = EmbeddingOrchestrator(
            provider=provider,
            lineage=lineage,
        )

        chunks = [
            TextChunk.create(
                document_id="doc_1",
                chunk_index=0,
                text="Dimension test chunk",
                start_char=0,
                end_char=20,
                token_count=5,
            )
        ]

        with pytest.raises(DimensionMismatchError) as exc_info:
            await orchestrator.embed_chunks(chunks)

        assert exc_info.value.details["expected_dimension"] == 8
        assert exc_info.value.details["actual_dimension"] == 16


class TestPartialBatchRetryAndCaching:
    """Acceptance: partial batch retry does not duplicate chunks."""

    @pytest.mark.asyncio
    async def test_partial_batch_retry_does_not_duplicate_chunks(self) -> None:
        provider = DeterministicEmbeddingProvider(dimensions=8)
        cache = InMemoryEmbeddingCache()
        orchestrator = EmbeddingOrchestrator(
            provider=provider,
            cache=cache,
            max_batch_size=2,
        )

        chunk1 = TextChunk.create(
            document_id="doc_retry",
            chunk_index=0,
            text="First sentence.",
            start_char=0,
            end_char=15,
            token_count=3,
        )
        chunk2 = TextChunk.create(
            document_id="doc_retry",
            chunk_index=1,
            text="Second sentence.",
            start_char=16,
            end_char=32,
            token_count=3,
        )
        chunk3 = TextChunk.create(
            document_id="doc_retry",
            chunk_index=2,
            text="Third sentence.",
            start_char=33,
            end_char=48,
            token_count=3,
        )

        # First run: embed chunk 1 & 2
        res1 = await orchestrator.embed_chunks([chunk1, chunk2])
        assert len(res1.embedded_chunks) == 2
        assert res1.computed_count == 2
        assert res1.cached_count == 0

        # Simulate retry with [chunk1, chunk2, chunk3] (including duplicate instances of chunk1)
        retry_chunks = [chunk1, chunk2, chunk3, chunk1]
        res2 = await orchestrator.embed_chunks(retry_chunks)

        # Output must be deduplicated by chunk_id
        chunk_ids = [ec.chunk_id for ec in res2.embedded_chunks]
        assert len(chunk_ids) == 3
        assert len(set(chunk_ids)) == 3
        assert res2.cached_count == 2  # chunks 1 & 2 were retrieved from cache
        assert res2.computed_count == 1  # only chunk 3 was computed

    @pytest.mark.asyncio
    async def test_sqlite_cache_persistence(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".sqlite3") as tmp_file:
            db_path = tmp_file.name

        cache = SqliteEmbeddingCache(db_path)
        provider = DeterministicEmbeddingProvider(dimensions=8)
        orchestrator = EmbeddingOrchestrator(provider=provider, cache=cache)

        chunks = [
            TextChunk.create(
                document_id="doc_sql",
                chunk_index=0,
                text="Persistent cached chunk text.",
                start_char=0,
                end_char=29,
                token_count=5,
            )
        ]

        # Initial embedding
        res1 = await orchestrator.embed_chunks(chunks)
        assert res1.computed_count == 1
        assert res1.cached_count == 0

        # Create new orchestrator pointing to the same SQLite DB
        new_cache = SqliteEmbeddingCache(db_path)
        new_orchestrator = EmbeddingOrchestrator(provider=provider, cache=new_cache)

        res2 = await new_orchestrator.embed_chunks(chunks)
        assert res2.computed_count == 0
        assert res2.cached_count == 1
        assert res2.embedded_chunks[0].embedding == res1.embedded_chunks[0].embedding
        new_cache.close()
        Path(db_path).unlink(missing_ok=True)


class TestBudgetAndCancellation:
    """Enforcement of token budgets and execution context cancellation."""

    @pytest.mark.asyncio
    async def test_token_budget_exhausted_fails(self) -> None:
        provider = DeterministicEmbeddingProvider(dimensions=8)
        budget = EmbeddingBudget(max_tokens=10)
        orchestrator = EmbeddingOrchestrator(
            provider=provider,
            budget=budget,
        )

        chunk_large = TextChunk.create(
            document_id="doc_budget",
            chunk_index=0,
            text="A" * 100,
            start_char=0,
            end_char=100,
            token_count=25,  # Exceeds max_tokens of 10
        )

        with pytest.raises(EmbeddingBudgetExhaustedError) as exc_info:
            await orchestrator.embed_chunks([chunk_large])

        assert exc_info.value.details["requested"] == 25
        assert exc_info.value.details["limit"] == 10

    @pytest.mark.asyncio
    async def test_cancellation_honoured(self) -> None:
        provider = DeterministicEmbeddingProvider(dimensions=8)
        orchestrator = EmbeddingOrchestrator(provider=provider)

        ctx = ExecutionContext(run_id="run_cancelled", node_id="embed_step")
        ctx.cancel()

        chunk = TextChunk.create(
            document_id="doc_cancel",
            chunk_index=0,
            text="Test cancelled",
            start_char=0,
            end_char=14,
            token_count=3,
        )

        with pytest.raises(StreamCancelledError):
            await orchestrator.embed_chunks([chunk], context=ctx)


class TestDocumentIntegration:
    """Integration with B20 Document and DocumentPage."""

    def test_chunk_document_preserves_page_citations(self) -> None:
        pages = [
            DocumentPage(
                page_number=1,
                text="Page 1 introduction to system concepts and design.",
                span_start=0,
                span_end=51,
            ),
            DocumentPage(
                page_number=2,
                text="Page 2 detailing module specifications and tests.",
                span_start=53,
                span_end=102,
            ),
        ]
        doc = Document(
            document_id="doc_multi_page",
            collection_id="specs",
            origin="spec.txt",
            content_hash="abc123hash",
            mime_type="text/plain",
            text=(
                "Page 1 introduction to system concepts and design.\n\n"
                "Page 2 detailing module specifications and tests."
            ),
            pages=pages,
            metadata={"source": "engineering_docs"},
        )

        chunker = FixedWindowChunker(chunk_size=40, chunk_overlap=10)
        chunks = chunker.chunk_document(doc)

        assert len(chunks) >= 2
        # First chunk spans page 1
        assert chunks[0].page_start == 1
        # Chunk metadata carries document metadata
        assert chunks[0].metadata.get("source") == "engineering_docs"
        # Later chunk reaches page 2
        last_chunk = chunks[-1]
        assert last_chunk.page_end == 2


class TestModuleContractsAndRegistry:
    """B00/B01 module contract and registry invocation."""

    @pytest.mark.asyncio
    async def test_chunking_module_invoke(self) -> None:
        mod = ChunkingModule()
        ctx = ExecutionContext(run_id="run_chunk_mod", node_id="chunk_step")
        res = await mod.invoke(
            ctx,
            inputs={
                "text": "Hello world from chunking module test.",
                "chunk_size": 20,
                "chunk_overlap": 5,
                "strategy": "fixed",
            },
        )
        assert res.status == "success"
        assert res.outputs["total_chunks"] >= 2
        assert "chunker_config_hash" in res.outputs

    @pytest.mark.asyncio
    async def test_embedding_module_invoke(self) -> None:
        mod = EmbeddingModule()
        ctx = ExecutionContext(run_id="run_embed_mod", node_id="embed_step")
        res = await mod.invoke(
            ctx,
            inputs={
                "texts": ["Sentence one", "Sentence two"],
                "dimensions": 8,
            },
        )
        assert res.status == "success"
        assert len(res.outputs["embeddings"]) == 2
        assert res.outputs["dimensions"] == 8
        assert res.outputs["computed_count"] == 2

    def test_modules_registered_in_default_registry(self) -> None:
        assert default_registry.has("nexura.knowledge.chunking")
        assert default_registry.has("nexura.knowledge.embeddings")
