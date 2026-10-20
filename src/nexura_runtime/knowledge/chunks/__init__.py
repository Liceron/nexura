# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Chunking, text segmentation, and token estimation package."""

from __future__ import annotations

from nexura_runtime.knowledge.chunks.chunkers import (
    BaseChunker,
    FixedWindowChunker,
    MarkdownChunker,
    RecursiveCharacterChunker,
)
from nexura_runtime.knowledge.chunks.errors import (
    ChunkingError,
    InvalidChunkerConfigError,
    TokenLimitExceededError,
)
from nexura_runtime.knowledge.chunks.models import (
    IndexLineage,
    TextChunk,
)
from nexura_runtime.knowledge.chunks.module import ChunkingModule
from nexura_runtime.knowledge.chunks.tokenizers import (
    CharacterTokenEstimator,
    HeuristicTokenEstimator,
    TokenEstimator,
    WordTokenEstimator,
    default_token_estimator,
)
from nexura_runtime.modules.registry import default_registry

_default_chunking_module = ChunkingModule()
if not default_registry.has(_default_chunking_module.manifest.module_id):
    default_registry.register(_default_chunking_module)

__all__ = [
    "BaseChunker",
    "FixedWindowChunker",
    "RecursiveCharacterChunker",
    "MarkdownChunker",
    "TextChunk",
    "IndexLineage",
    "TokenEstimator",
    "HeuristicTokenEstimator",
    "CharacterTokenEstimator",
    "WordTokenEstimator",
    "default_token_estimator",
    "ChunkingError",
    "TokenLimitExceededError",
    "InvalidChunkerConfigError",
    "ChunkingModule",
]
