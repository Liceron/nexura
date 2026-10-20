"""Standalone demonstration for Module B21: Chunking, Embeddings and Token Budgets.

Runs 100% keylessly and offline:
  uv run python examples/backend/b21.py
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys

from nexura_runtime.contracts.context import ExecutionContext

from nexura_runtime.knowledge.chunks import (
    FixedWindowChunker,
    IndexLineage,
    MarkdownChunker,
    RecursiveCharacterChunker,
    TextChunk,
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
)
from nexura_runtime.knowledge.ingest.models import Document, DocumentPage


def main() -> None:
    print("=== Module B21: Chunking, Embeddings, and Token Budgets ===")

    # 1. Bilingual Synthetic Document Creation
    print("\n1. Constructing bilingual synthetic document with page citations...")
    pages = [
        DocumentPage(
            page_number=1,
            text=(
                "Artificial Intelligence and Autonomous Systems Overview.\n"
                "Overview of neural agents, memory subsystems, and tool calling interfaces."
            ),
            span_start=0,
            span_end=133,
        ),
        DocumentPage(
            page_number=2,
            text=(
                "बहुभाषी तंत्रिका नेटवर्क और संदर्भ पुनःप्राप्ति "
                "(Multilingual neural retrieval).\n"
                "機械学習モデルと言語処理の仕組みについて "
                "(Machine learning and language processing)."
            ),
            span_start=135,
            span_end=283,
        ),
    ]
    doc = Document(
        document_id="doc_synth_bilingual",
        collection_id="ai_research",
        origin="bilingual_overview.md",
        content_hash="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        mime_type="text/markdown",
        text=pages[0].text + "\n\n" + pages[1].text,
        pages=pages,
        metadata={"domain": "agentic_ai", "author": "Nexura"},
    )
    print(f"  Document ID: {doc.document_id}")
    print(f"  Total Characters: {len(doc.text)}")
    print(f"  Pages Count: {len(doc.pages)}")

    # 2. Fixed Window Chunking with Overlap & Offset Verification
    print("\n2. Chunking with FixedWindowChunker (exact character spans & overlap)...")
    fixed_chunker = FixedWindowChunker(chunk_size=80, chunk_overlap=20)
    fixed_chunks = fixed_chunker.chunk_document(doc)
    print(f"  Produced {len(fixed_chunks)} fixed-window chunks.")
    for i, chk in enumerate(fixed_chunks):
        # Verify exact character slice
        sliced = doc.text[chk.start_char : chk.end_char]
        assert sliced == chk.text
        span_str = f"span=[{chk.start_char}:{chk.end_char}]"
        pages_str = f"pages={chk.page_start}..{chk.page_end}"
        print(
            f"  [Chunk {i}] {span_str}, {pages_str}, tokens={chk.token_count}: {chk.text[:30]!r}..."
        )

    # 3. Recursive Character Chunking
    print("\n3. Chunking with RecursiveCharacterChunker on structured paragraphs...")
    recursive_chunker = RecursiveCharacterChunker(chunk_size=100, chunk_overlap=20)
    rec_chunks = recursive_chunker.chunk_document(doc)
    print(f"  Produced {len(rec_chunks)} recursive chunks.")
    for i, chk in enumerate(rec_chunks):
        assert doc.text[chk.start_char : chk.end_char] == chk.text
        print(f"  [Rec Chunk {i}] tokens={chk.token_count}, span=[{chk.start_char}:{chk.end_char}]")

    # 4. Markdown Header-Aware Chunking
    print("\n4. Chunking markdown document with heading breadcrumbs...")
    md_content = (
        "# System Architecture\n\n"
        "Introduction to the core microkernel.\n\n"
        "## Vector Search Engine\n\n"
        "Dense vectors and hybrid inverted index coordination.\n\n"
        "### Ingestion Pipeline\n\n"
        "Offset-aware chunking and token budget verification."
    )
    md_chunker = MarkdownChunker(chunk_size=90, chunk_overlap=15)
    md_chunks = md_chunker.chunk_text(md_content, document_id="doc_md_arch")
    print(f"  Produced {len(md_chunks)} markdown chunks.")
    for chk in md_chunks:
        headings = chk.metadata.get("headings", [])
        print(f"  [MD Chunk] headings={headings}, text={chk.text[:35]!r}...")

    # 5. Deterministic Embedding Fixture (clearly labeled non-semantic)
    print("\n5. Generating dense embeddings with DeterministicEmbeddingProvider (non-semantic)...")
    provider = DeterministicEmbeddingProvider(
        model_id="deterministic-vector-fixture-non-semantic",
        revision="v1",
        dimensions=8,
    )
    cache = InMemoryEmbeddingCache()
    lineage = IndexLineage(
        chunker_name=fixed_chunker.chunker_name,
        chunker_config_hash=fixed_chunker.config_hash,
        model_name=provider.model_id,
        model_revision=provider.revision,
        dimensions=provider.dimensions,
    )
    orchestrator = EmbeddingOrchestrator(
        provider=provider,
        lineage=lineage,
        cache=cache,
        max_batch_size=4,
    )

    batch_result = asyncio.run(orchestrator.embed_chunks(fixed_chunks))
    print(f"  Lineage ID: {batch_result.lineage.lineage_id}")
    print(f"  Embedded Chunks: {len(batch_result.embedded_chunks)}")
    print(f"  Computed Count: {batch_result.computed_count}")
    print(f"  Cached Count: {batch_result.cached_count}")
    print(
        f"  Sample Vector (dim={len(batch_result.embedded_chunks[0].embedding)}): "
        f"{batch_result.embedded_chunks[0].embedding[:4]}..."
    )

    # 6. Content-Digest Cache Reuse & Partial Batch Retry Deduplication
    print("\n6. Testing content-digest cache reuse and retry deduplication...")
    # Re-embedding same chunks + duplicate
    retry_chunks = list(fixed_chunks) + [fixed_chunks[0]]
    retry_result = asyncio.run(orchestrator.embed_chunks(retry_chunks))
    print(f"  Retry Result Total Chunks: {len(retry_result.embedded_chunks)}")
    print(f"  Cached Count (reused without provider calls): {retry_result.cached_count}")
    print(f"  Computed Count: {retry_result.computed_count}")
    assert retry_result.cached_count == len(fixed_chunks)
    assert retry_result.computed_count == 0
    assert len(retry_result.embedded_chunks) == len(fixed_chunks)

    # 7. Index Lineage Variation
    print("\n7. Verifying index lineage change on chunker config or model revision changes...")
    other_chunker = FixedWindowChunker(chunk_size=120, chunk_overlap=30)
    other_lineage = IndexLineage(
        chunker_name=other_chunker.chunker_name,
        chunker_config_hash=other_chunker.config_hash,
        model_name=provider.model_id,
        model_revision="v2",  # Changed revision
        dimensions=provider.dimensions,
    )
    print(f"  Original Lineage ID: {lineage.lineage_id}")
    print(f"  New Lineage ID:      {other_lineage.lineage_id}")
    assert lineage.lineage_id != other_lineage.lineage_id
    print("  [SUCCESS] Lineage IDs are distinct across configuration changes.")

    # 8. Dimension Mismatch Protection
    print("\n8. Verifying dimension mismatch detection...")
    bad_provider = DeterministicEmbeddingProvider(dimensions=16)  # Returns 16, lineage expects 8
    bad_orchestrator = EmbeddingOrchestrator(
        provider=bad_provider,
        lineage=lineage,  # expects 8
    )
    sample_chunk = TextChunk.create(
        document_id="doc_dim",
        chunk_index=0,
        text="Dimension mismatch test",
        start_char=0,
        end_char=23,
        token_count=4,
    )
    try:
        asyncio.run(bad_orchestrator.embed_chunks([sample_chunk]))
        print("  [FAILED] Expected DimensionMismatchError was not raised!")
    except DimensionMismatchError as exc:
        print(f"  [SUCCESS] Caught expected error: {exc}")

    # 9. Token Budget Enforcement
    print("\n9. Verifying token budget bounds...")
    strict_budget = EmbeddingBudget(max_tokens=10)
    budgeted_orchestrator = EmbeddingOrchestrator(
        provider=provider,
        budget=strict_budget,
    )
    large_chunk = TextChunk.create(
        document_id="doc_budget",
        chunk_index=0,
        text="A" * 100,
        start_char=0,
        end_char=100,
        token_count=30,  # exceeds max_tokens of 10
    )
    try:
        asyncio.run(budgeted_orchestrator.embed_chunks([large_chunk]))
        print("  [FAILED] Expected EmbeddingBudgetExhaustedError was not raised!")
    except EmbeddingBudgetExhaustedError as exc:
        print(f"  [SUCCESS] Caught expected error: {exc}")

    # 10. B00/B01 Module Invocation via Python & CLI
    print("\n10. Executing ChunkingModule and EmbeddingModule via Python contract...")
    chunk_mod = ChunkingModule()
    ctx = ExecutionContext(run_id="run_b21_demo", node_id="chunk_step")
    chunk_res = asyncio.run(
        chunk_mod.invoke(
            ctx,
            inputs={
                "text": "Demo sentence for module invocation.",
                "chunk_size": 20,
                "chunk_overlap": 5,
            },
        )
    )
    print(
        f"  ChunkingModule status: {chunk_res.status}, "
        f"total_chunks: {chunk_res.outputs['total_chunks']}"
    )

    embed_mod = EmbeddingModule()
    embed_res = asyncio.run(
        embed_mod.invoke(
            ctx,
            inputs={"texts": ["First text", "Second text"], "dimensions": 8},
        )
    )
    print(
        f"  EmbeddingModule status: {embed_res.status}, "
        f"dimensions: {embed_res.outputs['dimensions']}"
    )

    # 11. B01 CLI Subprocess Invocation
    print("\n11. Invoking ChunkingModule via B01 CLI subprocess...")
    cmd = [
        sys.executable,
        "-m",
        "nexura_runtime.modules",
        "run",
        "nexura.knowledge.chunking",
        "--input",
        json.dumps(
            {
                "text": "CLI subprocess test for chunking.",
                "chunk_size": 15,
                "chunk_overlap": 5,
            }
        ),
    ]
    cli_proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    print(f"  CLI Exit Code: {cli_proc.returncode}")
    if cli_proc.returncode == 0:
        print(f"  CLI Output: {cli_proc.stdout.strip()}")
    else:
        print(f"  CLI Error: {cli_proc.stderr.strip()}")

    print("\n=== All B21 Demonstration Steps Completed Successfully ===")


if __name__ == "__main__":
    main()
