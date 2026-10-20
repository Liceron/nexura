# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Embedding orchestrator with lineage tracking, caching, batching, and budget enforcement."""

from __future__ import annotations

import time
from collections.abc import Sequence

from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.providers.base import ProviderProtocol
from nexura_agents.providers.errors import StreamCancelledError

from nexura_runtime.knowledge.chunks.models import IndexLineage, TextChunk
from nexura_runtime.knowledge.embeddings.cache import (
    EmbeddingCache,
    InMemoryEmbeddingCache,
)
from nexura_runtime.knowledge.embeddings.errors import (
    DimensionMismatchError,
    EmbeddingBudgetExhaustedError,
    EmbeddingProviderError,
)
from nexura_runtime.knowledge.embeddings.models import (
    BatchEmbeddingResult,
    EmbeddedChunk,
    EmbeddingBudget,
)
from nexura_runtime.knowledge.embeddings.provider import DeterministicEmbeddingProvider


class EmbeddingOrchestrator:
    """Orchestrates chunk embedding with caching, batching, and budget enforcement.

    Guarantees:
    - Reuses unchanged chunks via content-digest caching.
    - Prevents duplicate chunks on partial batch retries.
    - Fails immediately on dimension mismatch.
    - Enforces hard token budget limits before and during provider execution.
    - Honours ExecutionContext cancellation cleanly.
    """

    def __init__(
        self,
        provider: ProviderProtocol | None = None,
        *,
        lineage: IndexLineage | None = None,
        cache: EmbeddingCache | None = None,
        budget: EmbeddingBudget | None = None,
        max_batch_size: int = 32,
        max_batch_tokens: int = 8000,
    ) -> None:
        self.provider = provider or DeterministicEmbeddingProvider()
        self.cache = cache if cache is not None else InMemoryEmbeddingCache()
        self.budget = budget or EmbeddingBudget()
        self.max_batch_size = max(1, max_batch_size)
        self.max_batch_tokens = max(1, max_batch_tokens)

        # Default lineage if unspecified
        if lineage is not None:
            self.lineage = lineage
        else:
            model_name = getattr(self.provider, "model_id", "unknown-model")
            revision = getattr(self.provider, "revision", "v1")
            dimensions = getattr(self.provider, "dimensions", 8)
            self.lineage = IndexLineage(
                chunker_name="default",
                chunker_config_hash="default",
                model_name=model_name,
                model_revision=revision,
                dimensions=dimensions,
            )

    def _partition_batches(
        self,
        chunks: Sequence[TextChunk],
    ) -> list[list[TextChunk]]:
        """Partition chunks into sub-batches respecting max_batch_size and max_batch_tokens."""
        batches: list[list[TextChunk]] = []
        current_batch: list[TextChunk] = []
        current_tokens = 0

        for chunk in chunks:
            chunk_tokens = max(1, chunk.token_count)
            # If current batch would exceed size or token limit, start a new batch
            if current_batch and (
                len(current_batch) >= self.max_batch_size
                or current_tokens + chunk_tokens > self.max_batch_tokens
            ):
                batches.append(current_batch)
                current_batch = [chunk]
                current_tokens = chunk_tokens
            else:
                current_batch.append(chunk)
                current_tokens += chunk_tokens

        if current_batch:
            batches.append(current_batch)

        return batches

    async def embed_chunks(
        self,
        chunks: Sequence[TextChunk],
        *,
        context: ExecutionContext | None = None,
    ) -> BatchEmbeddingResult:
        """Embed a sequence of TextChunk records with caching and budget protection."""
        start_time = time.monotonic()

        # Cancellation check
        if context is not None and context.is_cancelled:
            raise StreamCancelledError(
                "Embedding cancelled before execution.",
                stream_id=context.run_id,
            )

        # 1. Deduplicate input chunks by chunk_id to prevent duplicates on retries
        unique_chunks: list[TextChunk] = []
        seen_chunk_ids: set[str] = set()
        for c in chunks:
            if c.chunk_id not in seen_chunk_ids:
                seen_chunk_ids.add(c.chunk_id)
                unique_chunks.append(c)

        # 2. Check cache for existing embeddings
        content_hashes = [c.content_hash for c in unique_chunks]
        cached_vectors = self.cache.get_many(self.lineage.lineage_id, content_hashes)

        uncached_chunks: list[TextChunk] = []
        for c in unique_chunks:
            if c.content_hash not in cached_vectors:
                uncached_chunks.append(c)

        # 3. Pre-flight budget check for uncached tokens
        needed_tokens = sum(c.token_count for c in uncached_chunks)
        if not self.budget.can_consume(needed_tokens):
            remaining = self.budget.remaining_tokens or 0
            raise EmbeddingBudgetExhaustedError(
                requested=needed_tokens,
                remaining=remaining,
                limit=self.budget.max_tokens or 0,
            )

        # 4. Partition uncached chunks and execute provider calls
        newly_computed: dict[str, list[float]] = {}
        if uncached_chunks:
            batches = self._partition_batches(uncached_chunks)

            for batch in batches:
                if context is not None and context.is_cancelled:
                    raise StreamCancelledError(
                        "Embedding cancelled during batch iteration.",
                        stream_id=context.run_id,
                    )

                batch_texts = [c.text for c in batch]
                batch_tokens = sum(c.token_count for c in batch)

                # Consume budget before calling provider
                self.budget.consume(batch_tokens)

                try:
                    result = await self.provider.embed(batch_texts, context=context)
                except Exception as exc:
                    if isinstance(exc, (StreamCancelledError, DimensionMismatchError)):
                        raise
                    raise EmbeddingProviderError(
                        provider_name=getattr(self.provider, "model_id", "unknown"),
                        message=str(exc),
                    ) from exc

                if len(result.embeddings) != len(batch):
                    raise EmbeddingProviderError(
                        provider_name=getattr(self.provider, "model_id", "unknown"),
                        message=(
                            f"Provider returned {len(result.embeddings)} embeddings for "
                            f"{len(batch)} input texts."
                        ),
                    )

                # Validate dimensions for every returned vector
                for vec in result.embeddings:
                    if len(vec) != self.lineage.dimensions:
                        raise DimensionMismatchError(
                            expected=self.lineage.dimensions,
                            actual=len(vec),
                        )

                # Store newly computed vectors in batch cache immediately
                batch_entries = {
                    c.content_hash: vec for c, vec in zip(batch, result.embeddings, strict=False)
                }
                self.cache.put_many(self.lineage.lineage_id, batch_entries)
                newly_computed.update(batch_entries)

        # 5. Assemble final EmbeddedChunk list
        all_vectors = {**cached_vectors, **newly_computed}
        embedded_chunks: list[EmbeddedChunk] = []

        for c in unique_chunks:
            vec = all_vectors[c.content_hash]
            embedded_chunks.append(
                EmbeddedChunk.from_chunk(chunk=c, embedding=vec, lineage=self.lineage)
            )

        duration_ms = round((time.monotonic() - start_time) * 1000, 2)
        total_tokens = sum(c.token_count for c in unique_chunks)

        return BatchEmbeddingResult(
            embedded_chunks=embedded_chunks,
            lineage=self.lineage,
            cached_count=len(cached_vectors),
            computed_count=len(uncached_chunks),
            total_tokens=total_tokens,
            duration_ms=duration_ms,
        )

    async def embed_texts(
        self,
        texts: Sequence[str],
        document_id: str = "doc_batch",
        *,
        context: ExecutionContext | None = None,
    ) -> BatchEmbeddingResult:
        """Convenience method to embed raw string texts."""
        chunks: list[TextChunk] = []
        for idx, text in enumerate(texts):
            tokens = max(1, len(text.split()))
            chunks.append(
                TextChunk.create(
                    document_id=document_id,
                    chunk_index=idx,
                    text=text,
                    start_char=0,
                    end_char=len(text),
                    token_count=tokens,
                )
            )
        return await self.embed_chunks(chunks, context=context)
