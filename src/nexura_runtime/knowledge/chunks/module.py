# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""B00/B01 module contract adapter for chunking text and documents."""

from __future__ import annotations

from typing import Any

from nexura_agents.contracts.base import BaseModule
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.manifest import EffectPolicy, ModuleManifest
from nexura_agents.contracts.result import ModuleResult

from nexura_runtime.knowledge.chunks.chunkers import (
    BaseChunker,
    FixedWindowChunker,
    MarkdownChunker,
    RecursiveCharacterChunker,
)
from nexura_runtime.knowledge.chunks.errors import ChunkingError


class ChunkingModule(BaseModule):
    """B00/B01 module wrapping text and document chunking."""

    def __init__(
        self,
        *,
        module_id: str = "nexura.knowledge.chunking",
        version: str = "1.0.0",
        effect_policy: EffectPolicy = "read",
    ) -> None:
        self._manifest = ModuleManifest(
            schema_version=1,
            module_id=module_id,
            version=version,
            kind="knowledge",
            title="Document and Text Chunker",
            description=(
                "Splits documents and strings into deterministic TextChunk records with exact "
                "character offsets, overlap, and token estimates."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Source text string to chunk",
                    },
                    "document_id": {
                        "type": "string",
                        "description": "Document identifier",
                        "default": "doc_1",
                    },
                    "strategy": {
                        "type": "string",
                        "enum": ["recursive", "fixed", "markdown"],
                        "default": "recursive",
                        "description": "Chunking strategy",
                    },
                    "chunk_size": {
                        "type": "integer",
                        "default": 500,
                        "description": "Target chunk size in characters",
                    },
                    "chunk_overlap": {
                        "type": "integer",
                        "default": 50,
                        "description": "Overlap between consecutive chunks in characters",
                    },
                    "max_chunk_tokens": {
                        "type": "integer",
                        "description": "Hard limit on tokens per chunk",
                    },
                },
                "required": ["text"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "chunks": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "List of TextChunk records",
                    },
                    "total_chunks": {"type": "integer"},
                    "total_tokens": {"type": "integer"},
                    "chunker_config_hash": {"type": "string"},
                },
                "required": ["chunks", "total_chunks", "total_tokens", "chunker_config_hash"],
            },
            config_schema={"type": "object"},
            named_ports=[],
            capabilities={"streaming": False, "network": False},
            effect_policy=effect_policy,
            dependency_extra="knowledge",
            min_sdk_version="0.3.0",
            timeout_seconds=30.0,
            output_limit_bytes=2_000_000,
            required_credential_types=[],
            supported_modalities=["text"],
            supported_locales=["all"],
            source_reference="knowledge:chunks",
            verification_status="mock_verified",
        )

    @property
    def manifest(self) -> ModuleManifest:
        return self._manifest

    async def invoke(
        self,
        context: ExecutionContext,
        inputs: dict[str, Any],
        config: dict[str, Any] | None = None,
    ) -> ModuleResult:
        text = str(inputs.get("text", ""))
        doc_id = str(inputs.get("document_id", "doc_1"))
        strategy = str(inputs.get("strategy", "recursive")).lower()
        chunk_size = int(inputs.get("chunk_size", 500))
        chunk_overlap = int(inputs.get("chunk_overlap", 50))
        max_chunk_tokens = inputs.get("max_chunk_tokens")
        if max_chunk_tokens is not None:
            max_chunk_tokens = int(max_chunk_tokens)

        try:
            chunker: BaseChunker
            if strategy == "fixed":
                chunker = FixedWindowChunker(
                    chunk_size=chunk_size,
                    chunk_overlap=chunk_overlap,
                    max_chunk_tokens=max_chunk_tokens,
                )
            elif strategy == "markdown":
                chunker = MarkdownChunker(
                    chunk_size=chunk_size,
                    chunk_overlap=chunk_overlap,
                    max_chunk_tokens=max_chunk_tokens,
                )
            else:
                chunker = RecursiveCharacterChunker(
                    chunk_size=chunk_size,
                    chunk_overlap=chunk_overlap,
                    max_chunk_tokens=max_chunk_tokens,
                )

            chunks = chunker.chunk_text(text, document_id=doc_id)
            chunks_data = [c.model_dump() for c in chunks]
            total_tokens = sum(c.token_count for c in chunks)

            return ModuleResult.success(
                outputs={
                    "chunks": chunks_data,
                    "total_chunks": len(chunks),
                    "total_tokens": total_tokens,
                    "chunker_config_hash": chunker.config_hash,
                }
            )
        except ChunkingError as exc:
            return ModuleResult.failure(
                error_message=str(exc),
                error_code="CHUNKING_ERROR",
            )
        except Exception as exc:
            return ModuleResult.failure(
                error_message=f"Unexpected chunking failure: {exc}",
                error_code="EXECUTION_FAILED",
            )
