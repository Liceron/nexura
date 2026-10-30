# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Vector similarity and distance metrics."""

from __future__ import annotations

import math
from collections.abc import Sequence


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Compute cosine similarity between two float vectors in [-1.0, 1.0]."""
    if len(a) != len(b):
        raise ValueError(f"Vector dimensions must match: {len(a)} != {len(b)}")

    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0

    for x, y in zip(a, b, strict=True):
        dot += x * y
        norm_a += x * x
        norm_b += y * y

    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0

    sim = dot / (math.sqrt(norm_a) * math.sqrt(norm_b))
    # Numerical clamp
    return max(-1.0, min(1.0, sim))


def dot_product(a: Sequence[float], b: Sequence[float]) -> float:
    """Compute dot product between two float vectors."""
    if len(a) != len(b):
        raise ValueError(f"Vector dimensions must match: {len(a)} != {len(b)}")

    return sum(x * y for x, y in zip(a, b, strict=True))


def euclidean_distance(a: Sequence[float], b: Sequence[float]) -> float:
    """Compute Euclidean distance between two float vectors."""
    if len(a) != len(b):
        raise ValueError(f"Vector dimensions must match: {len(a)} != {len(b)}")

    sq_sum = sum((x - y) ** 2 for x, y in zip(a, b, strict=True))
    return math.sqrt(sq_sum)


def compute_score(
    metric: str,
    query_vector: Sequence[float],
    candidate_vector: Sequence[float],
) -> float:
    """Compute similarity score according to the index metric (higher is better)."""
    if metric == "cosine":
        return cosine_similarity(query_vector, candidate_vector)
    elif metric == "dot_product":
        return dot_product(query_vector, candidate_vector)
    elif metric == "euclidean":
        dist = euclidean_distance(query_vector, candidate_vector)
        return 1.0 / (1.0 + dist)
    else:
        raise ValueError(f"Unsupported vector metric: '{metric}'")
