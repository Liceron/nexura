# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Fusion algorithms and reranking protocols."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Literal, Protocol, runtime_checkable

from nexura_runtime.knowledge.retrieval.errors import RerankerFailureError
from nexura_runtime.knowledge.retrieval.models import RetrievedItem

_WORD_PATTERN = re.compile(r"\w+", re.UNICODE)


def reciprocal_rank_fusion(
    dense_ranks: dict[str, int],
    lexical_ranks: dict[str, int],
    *,
    k: int = 60,
    dense_weight: float = 0.5,
    lexical_weight: float = 0.5,
) -> dict[str, float]:
    """Compute Reciprocal Rank Fusion (RRF) scores across two ranked candidate lists."""
    all_keys = set(dense_ranks.keys()) | set(lexical_ranks.keys())
    scores: dict[str, float] = {}

    for doc_id in all_keys:
        score = 0.0
        if doc_id in dense_ranks:
            score += dense_weight / (k + dense_ranks[doc_id])
        if doc_id in lexical_ranks:
            score += lexical_weight / (k + lexical_ranks[doc_id])
        scores[doc_id] = round(score, 6)

    return scores


def weighted_score_fusion(
    dense_scores: dict[str, float],
    lexical_scores: dict[str, float],
    *,
    dense_weight: float = 0.7,
    lexical_weight: float = 0.3,
) -> dict[str, float]:
    """Combine dense and lexical scores using normalized linear weighting."""
    all_keys = set(dense_scores.keys()) | set(lexical_scores.keys())
    total_weight = dense_weight + lexical_weight
    if total_weight <= 0:
        total_weight = 1.0

    w_d = dense_weight / total_weight
    w_l = lexical_weight / total_weight

    scores: dict[str, float] = {}
    for doc_id in all_keys:
        d_val = dense_scores.get(doc_id, 0.0)
        l_val = lexical_scores.get(doc_id, 0.0)
        scores[doc_id] = round(w_d * d_val + w_l * l_val, 6)

    return scores


@runtime_checkable
class Reranker(Protocol):
    """Protocol for post-retrieval candidate reranking."""

    async def rerank(
        self,
        query: str,
        items: Sequence[RetrievedItem],
    ) -> list[RetrievedItem]:
        """Rerank retrieved candidate items."""
        ...


class HeuristicReranker:
    """Fast, offline term-overlap and phrase-matching reranker."""

    def __init__(self, phrase_boost: float = 0.3) -> None:
        self.phrase_boost = phrase_boost

    async def rerank(
        self,
        query: str,
        items: Sequence[RetrievedItem],
    ) -> list[RetrievedItem]:
        query_words = set(_WORD_PATTERN.findall(query.lower()))
        clean_query = " ".join(query.lower().split())
        reranked: list[RetrievedItem] = []

        for item in items:
            item_text = item.text.lower()
            item_words = set(_WORD_PATTERN.findall(item_text))

            # Word overlap ratio
            overlap = len(query_words & item_words) / max(1, len(query_words))

            # Exact substring match boost
            has_phrase = 1.0 if clean_query and clean_query in item_text else 0.0

            # Title or heading match boost
            headings = item.metadata.get("headings", [])
            heading_match = 0.2 if any(clean_query in str(h).lower() for h in headings) else 0.0

            boost = overlap + (self.phrase_boost * has_phrase) + heading_match
            # Combine fused score with boost
            final_score = round(0.5 * item.fused_score + 0.5 * min(1.0, boost), 6)

            updated = item.model_copy(
                update={
                    "rerank_score": round(boost, 6),
                    "score": final_score,
                }
            )
            reranked.append(updated)

        reranked.sort(key=lambda x: x.score, reverse=True)
        return reranked


class FailingReranker:
    """Fixture reranker that simulates failure for policy testing."""

    def __init__(self, reason: str = "Simulated cross-encoder model failure") -> None:
        self.reason = reason

    async def rerank(
        self,
        query: str,
        items: Sequence[RetrievedItem],
    ) -> list[RetrievedItem]:
        raise RuntimeError(self.reason)


async def execute_reranking(
    reranker: Reranker,
    query: str,
    items: Sequence[RetrievedItem],
    *,
    on_failure: Literal["fallback_to_fusion", "raise"] = "fallback_to_fusion",
) -> tuple[list[RetrievedItem], bool]:
    """Execute reranking with configurable failure policy.

    Returns (reranked_items, was_fallback_used).
    """
    try:
        results = await reranker.rerank(query, items)
        return results, False
    except Exception as exc:
        if on_failure == "raise":
            reranker_name = getattr(reranker, "__class__", type(reranker)).__name__
            raise RerankerFailureError(reranker_name, str(exc)) from exc
        # Fallback to existing fused score ranking
        fallback_items = sorted(items, key=lambda x: x.fused_score, reverse=True)
        return fallback_items, True
