# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Offline BM25 lexical keyword search engine."""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence

_TOKEN_PATTERN = re.compile(r"\w+", re.UNICODE)


def tokenize(text: str) -> list[str]:
    """Tokenize text into lowercased alphanumeric words."""
    return [match.group(0).lower() for match in _TOKEN_PATTERN.finditer(text)]


class BM25Index:
    """In-memory BM25 index over documents/chunks."""

    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self.doc_ids: list[str] = []
        self.doc_lengths: list[int] = []
        self.avg_doc_len: float = 0.0
        self.doc_freqs: dict[str, int] = {}
        self.term_freqs: list[Counter[str]] = []
        self.total_docs: int = 0

    def fit(self, docs: Sequence[tuple[str, str]]) -> None:
        """Index a collection of (doc_id, text) pairs."""
        self.doc_ids = []
        self.doc_lengths = []
        self.term_freqs = []
        self.doc_freqs = {}

        total_len = 0
        for doc_id, text in docs:
            tokens = tokenize(text)
            doc_len = len(tokens)
            self.doc_ids.append(doc_id)
            self.doc_lengths.append(doc_len)
            total_len += doc_len

            counts = Counter(tokens)
            self.term_freqs.append(counts)

            for term in counts:
                self.doc_freqs[term] = self.doc_freqs.get(term, 0) + 1

        self.total_docs = len(docs)
        self.avg_doc_len = (total_len / self.total_docs) if self.total_docs > 0 else 1.0

    def score(self, query: str) -> dict[str, float]:
        """Score all indexed documents against query terms, returning normalized scores."""
        if self.total_docs == 0:
            return {}

        query_tokens = tokenize(query)
        if not query_tokens:
            return {}

        scores: dict[str, float] = {}

        for i, doc_id in enumerate(self.doc_ids):
            doc_len = self.doc_lengths[i]
            counts = self.term_freqs[i]
            doc_score = 0.0

            for term in query_tokens:
                if term not in counts:
                    continue

                freq = counts[term]
                df = self.doc_freqs.get(term, 0)
                # Robertson-Spärck Jones IDF
                idf = math.log(1.0 + (self.total_docs - df + 0.5) / (df + 0.5))

                numerator = freq * (self.k1 + 1.0)
                len_norm = 1.0 - self.b + self.b * (doc_len / self.avg_doc_len)
                denominator = freq + self.k1 * len_norm
                doc_score += idf * (numerator / denominator)

            if doc_score > 0.0:
                scores[doc_id] = doc_score

        # Normalize scores to [0.0, 1.0]
        if not scores:
            return {}

        max_score = max(scores.values())
        if max_score > 0.0:
            return {d_id: round(s / max_score, 6) for d_id, s in scores.items()}

        return scores
