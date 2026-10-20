# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Orchestrated RAG pipeline combining hybrid retrieval, reranking, and citations."""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Literal

from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.providers.base import BaseProvider, ProviderProtocol
from nexura_agents.providers.errors import StreamCancelledError
from nexura_agents.types import Message

from nexura_runtime.knowledge.chunks.models import IndexLineage, TextChunk
from nexura_runtime.knowledge.embeddings.provider import DeterministicEmbeddingProvider
from nexura_runtime.knowledge.retrieval.fusion import (
    HeuristicReranker,
    Reranker,
    execute_reranking,
    weighted_score_fusion,
)
from nexura_runtime.knowledge.retrieval.lexical import BM25Index
from nexura_runtime.knowledge.retrieval.models import (
    PackedContext,
    RagQuery,
    RagResponse,
    RetrievedItem,
)
from nexura_runtime.knowledge.retrieval.packer import ContextPacker
from nexura_runtime.knowledge.vector.models import (
    VectorRecord,
    VectorSearchQuery,
)
from nexura_runtime.knowledge.vector.store import InMemoryVectorStore, VectorStore


class RagPipeline:
    """End-to-end RAG orchestrator with hybrid search, citation tracking, and evidence bounds.

    Guarantees:
    - Fuses dense vector similarity with lexical BM25 keyword matching.
    - Strict document and workspace permission enforcement before context packing.
    - Bounded context token budget enforcement.
    - Explicit insufficient evidence signaling on empty retrieval (zero hallucination).
    - Preserves verifiable source, page, and character span citations.
    """

    def __init__(
        self,
        vector_store: VectorStore | None = None,
        embedding_provider: ProviderProtocol | None = None,
        model_provider: BaseProvider | None = None,
        reranker: Reranker | None = None,
        *,
        on_rerank_failure: Literal["fallback_to_fusion", "raise"] = "fallback_to_fusion",
        context_packer: ContextPacker | None = None,
    ) -> None:
        self.vector_store = vector_store or InMemoryVectorStore()
        self.embedding_provider = embedding_provider or DeterministicEmbeddingProvider()
        self.model_provider = model_provider
        self.reranker = reranker or HeuristicReranker()
        self.on_rerank_failure = on_rerank_failure
        self.context_packer = context_packer or ContextPacker()
        self._bm25_indices: dict[tuple[str, str], BM25Index] = {}

    def _get_bm25_index(self, workspace_id: str, collection_id: str) -> BM25Index:
        key = (workspace_id, collection_id)
        if key not in self._bm25_indices:
            self._bm25_indices[key] = BM25Index()
        return self._bm25_indices[key]

    async def index_chunks(
        self,
        workspace_id: str,
        collection_id: str,
        chunks: Sequence[TextChunk],
        *,
        lineage: IndexLineage | None = None,
    ) -> int:
        """Helper to embed and index TextChunks into the vector store and BM25 index."""
        if not chunks:
            return 0

        # Embed texts
        texts = [c.text for c in chunks]
        embed_res = await self.embedding_provider.embed(texts)
        dims = len(embed_res.embeddings[0]) if embed_res.embeddings else 8

        # Ensure collection exists
        await self.vector_store.create_collection(
            workspace_id=workspace_id,
            collection_id=collection_id,
            dimensions=dims,
            lineage=lineage,
        )

        records: list[VectorRecord] = []
        doc_tuples: list[tuple[str, str]] = []

        for c, vec in zip(chunks, embed_res.embeddings, strict=False):
            meta = dict(c.metadata)
            meta.update(
                {
                    "page_start": c.page_start,
                    "page_end": c.page_end,
                    "start_char": c.start_char,
                    "end_char": c.end_char,
                }
            )
            records.append(
                VectorRecord(
                    id=c.chunk_id,
                    workspace_id=workspace_id,
                    collection_id=collection_id,
                    generation_id="",
                    vector=vec,
                    document_id=c.document_id,
                    text=c.text,
                    metadata=meta,
                )
            )
            doc_tuples.append((c.chunk_id, c.text))

        # Upsert into vector store
        upserted = await self.vector_store.upsert(workspace_id, collection_id, records)

        # Update BM25 index
        bm25 = self._get_bm25_index(workspace_id, collection_id)
        bm25.fit(doc_tuples)

        return upserted

    async def retrieve(
        self,
        query: RagQuery,
        *,
        context: ExecutionContext | None = None,
    ) -> PackedContext:
        """Perform hybrid retrieval, fusion, reranking, and token-bounded context packing."""
        if context is not None and context.is_cancelled:
            raise StreamCancelledError(
                "Retrieval cancelled before execution.",
                stream_id=context.run_id,
            )

        # 1. Dense retrieval
        embed_out = await self.embedding_provider.embed([query.query], context=context)
        q_vector = embed_out.embeddings[0]

        dense_search_q = VectorSearchQuery(
            vector=q_vector,
            collection_id=query.collection_id,
            workspace_id=query.workspace_id,
            top_k=query.top_k * 2,  # Oversample for fusion
            filter=query.filter,
            include_metadata=True,
            include_vector=False,
        )
        dense_hits = await self.vector_store.search(query.workspace_id, dense_search_q)
        dense_scores = {h.id: h.score for h in dense_hits}
        dense_items_map = {h.id: h for h in dense_hits}

        # 2. Lexical retrieval
        bm25 = self._get_bm25_index(query.workspace_id, query.collection_id)
        lexical_scores = bm25.score(query.query)

        # 3. Hybrid fusion
        fused_scores = weighted_score_fusion(
            dense_scores=dense_scores,
            lexical_scores=lexical_scores,
            dense_weight=query.dense_weight,
            lexical_weight=query.lexical_weight,
        )

        # Assemble candidate items
        candidates: list[RetrievedItem] = []
        for c_id, f_score in fused_scores.items():
            hit = dense_items_map.get(c_id)
            if not hit:
                # Retrieve from vector store if found in BM25 but not dense top-k
                rec = await self.vector_store.get(query.workspace_id, query.collection_id, c_id)
                if not rec or rec.is_deleted:
                    continue
                meta = dict(rec.metadata)
                text = rec.text
                doc_id = rec.document_id
            else:
                meta = dict(hit.metadata)
                text = hit.text
                doc_id = hit.document_id

            page_start = int(meta.get("page_start", 1))
            page_end = int(meta.get("page_end", page_start))
            start_char = int(meta.get("start_char", 0))
            end_char = int(meta.get("end_char", len(text)))

            item = RetrievedItem(
                chunk_id=c_id,
                document_id=doc_id,
                origin=str(meta.get("origin", doc_id)),
                text=text,
                page_start=page_start,
                page_end=page_end,
                start_char=start_char,
                end_char=end_char,
                dense_score=dense_scores.get(c_id),
                lexical_score=lexical_scores.get(c_id),
                fused_score=f_score,
                score=f_score,
                metadata=meta,
            )
            candidates.append(item)

        candidates.sort(key=lambda x: x.fused_score, reverse=True)
        top_candidates = candidates[: query.top_k]

        # 4. Reranking (if enabled)
        if query.rerank and self.reranker and top_candidates:
            reranked, _ = await execute_reranking(
                self.reranker,
                query.query,
                top_candidates,
                on_failure=self.on_rerank_failure,
            )
            ranked_items = reranked
        else:
            ranked_items = top_candidates

        # 5. Pack context with permission filtering and token budget enforcement
        return self.context_packer.pack(
            ranked_items,
            max_context_tokens=query.max_context_tokens,
            allowed_document_ids=query.allowed_document_ids,
            min_score_threshold=query.min_score_threshold,
        )

    async def answer(
        self,
        query: RagQuery,
        *,
        context: ExecutionContext | None = None,
    ) -> RagResponse:
        """Execute full citation-backed RAG: retrieve, pack evidence, and generate response."""
        t0 = time.monotonic()
        packed = await self.retrieve(query, context=context)
        retrieval_dur = round((time.monotonic() - t0) * 1000, 2)

        # Empty evidence check: never fabricate or hallucinate!
        if not packed.has_sufficient_evidence:
            return RagResponse(
                query=query.query,
                answer="No relevant documents found to answer the query.",
                has_sufficient_evidence=False,
                citations=[],
                packed_context=packed,
                retrieval_duration_ms=retrieval_dur,
                generation_duration_ms=0.0,
            )

        t1 = time.monotonic()
        # Generate citation-backed answer
        if self.model_provider is not None:
            system_prompt = (
                "You are an accurate, citation-backed knowledge assistant. "
                "Answer the user query strictly using the provided reference sources. "
                "Cite your sources using bracketed citations such as [1] or [2] "
                "when stating facts. "
                "If the reference sources do not contain the answer, state that explicitly."
            )
            user_msg = f"{packed.formatted_context}\n\nQuestion: {query.query}\nAnswer:"
            messages = [
                Message(role="system", content=system_prompt),
                Message(role="user", content=user_msg),
            ]
            resp = await self.model_provider.generate(messages, context=context)
            raw_answer = resp.message.content or ""
        else:
            # Deterministic offline fallback answer
            top_hit = packed.items[0]
            top_cit = packed.citations[0]
            raw_answer = (
                f"According to {top_cit.origin} (Page {top_cit.page_start}), "
                f"{top_hit.text.strip()} {top_cit.citation_id}"
            )

        gen_dur = round((time.monotonic() - t1) * 1000, 2)

        return RagResponse(
            query=query.query,
            answer=raw_answer,
            has_sufficient_evidence=True,
            citations=packed.citations,
            packed_context=packed,
            retrieval_duration_ms=retrieval_dur,
            generation_duration_ms=gen_dur,
        )
