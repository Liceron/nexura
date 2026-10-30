# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Retrieval, reranking, and citation-backed RAG package for Nexura AgentKit."""

from __future__ import annotations

from nexura_runtime.knowledge.retrieval.errors import (
    ContextBudgetExceededError,
    InsufficientEvidenceError,
    RerankerFailureError,
    RetrievalError,
)
from nexura_runtime.knowledge.retrieval.fusion import (
    FailingReranker,
    HeuristicReranker,
    Reranker,
    reciprocal_rank_fusion,
    weighted_score_fusion,
)
from nexura_runtime.knowledge.retrieval.lexical import BM25Index
from nexura_runtime.knowledge.retrieval.models import (
    Citation,
    PackedContext,
    RagQuery,
    RagResponse,
    RetrievedItem,
)
from nexura_runtime.knowledge.retrieval.module import RagModule
from nexura_runtime.knowledge.retrieval.packer import ContextPacker
from nexura_runtime.knowledge.retrieval.pipeline import RagPipeline
from nexura_runtime.knowledge.retrieval.tool import create_retrieval_tool
from nexura_runtime.modules.registry import default_registry

_default_rag_module = RagModule()
if not default_registry.has(_default_rag_module.manifest.module_id):
    default_registry.register(_default_rag_module)

__all__ = [
    "RagPipeline",
    "RagQuery",
    "RagResponse",
    "Citation",
    "RetrievedItem",
    "PackedContext",
    "ContextPacker",
    "BM25Index",
    "Reranker",
    "HeuristicReranker",
    "FailingReranker",
    "reciprocal_rank_fusion",
    "weighted_score_fusion",
    "create_retrieval_tool",
    "RagModule",
    "RetrievalError",
    "RerankerFailureError",
    "ContextBudgetExceededError",
    "InsufficientEvidenceError",
]
