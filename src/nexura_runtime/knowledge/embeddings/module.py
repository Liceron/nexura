# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""B00/B01 module contract adapter for embedding generation."""

from __future__ import annotations

from typing import Any

from nexura_agents.contracts.base import BaseModule
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.manifest import EffectPolicy, ModuleManifest
from nexura_agents.contracts.result import ModuleResult

from nexura_runtime.knowledge.chunks.models import IndexLineage, TextChunk
from nexura_runtime.knowledge.embeddings.cache import InMemoryEmbeddingCache
from nexura_runtime.knowledge.embeddings.embedder import EmbeddingOrchestrator
from nexura_runtime.knowledge.embeddings.errors import EmbeddingError
from nexura_runtime.knowledge.embeddings.provider import DeterministicEmbeddingProvider


class EmbeddingModule(BaseModule):
    """B00/B01 module wrapping dense vector embedding generation."""

    def __init__(
        self,
        orchestrator: EmbeddingOrchestrator | None = None,
        *,
        module_id: str = "nexura.knowledge.embeddings",
        version: str = "1.0.0",
        effect_policy: EffectPolicy = "idempotent_write",
    ) -> None:
        self.orchestrator = orchestrator or EmbeddingOrchestrator(
            provider=DeterministicEmbeddingProvider(),
            cache=InMemoryEmbeddingCache(),
        )

        self._manifest = ModuleManifest(
            schema_version=1,
            module_id=module_id,
            version=version,
            kind="knowledge",
            title="Vector Embedding Generator",
            description=(
                "Generates dense vector embeddings for text chunks or strings with deterministic "
                "lineage, dimension verification, and content-digest caching."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "texts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of raw text strings to embed",
                    },
                    "chunks": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "List of serialized TextChunk dictionaries",
                    },
                    "dimensions": {
                        "type": "integer",
                        "default": 8,
                        "description": "Target embedding vector dimension",
                    },
                    "model_name": {
                        "type": "string",
                        "default": "deterministic-vector-fixture-non-semantic",
                        "description": "Embedding model identifier",
                    },
                },
            },
            output_schema={
                "type": "object",
                "properties": {
                    "embeddings": {
                        "type": "array",
                        "items": {
                            "type": "array",
                            "items": {"type": "number"},
                        },
                        "description": "List of dense vector embeddings",
                    },
                    "embedded_chunks": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "List of EmbeddedChunk dictionaries",
                    },
                    "lineage_id": {"type": "string"},
                    "dimensions": {"type": "integer"},
                    "cached_count": {"type": "integer"},
                    "computed_count": {"type": "integer"},
                    "total_tokens": {"type": "integer"},
                },
                "required": [
                    "embeddings",
                    "lineage_id",
                    "dimensions",
                    "cached_count",
                    "computed_count",
                    "total_tokens",
                ],
            },
            config_schema={"type": "object"},
            named_ports=[],
            capabilities={"streaming": False, "network": False},
            effect_policy=effect_policy,
            dependency_extra="knowledge",
            min_sdk_version="0.3.0",
            timeout_seconds=60.0,
            output_limit_bytes=2_000_000,
            required_credential_types=[],
            supported_modalities=["text"],
            supported_locales=["all"],
            source_reference="knowledge:embeddings",
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
        texts = inputs.get("texts")
        raw_chunks = inputs.get("chunks")
        dimensions = int(inputs.get("dimensions", 8))
        model_name = str(inputs.get("model_name", "deterministic-vector-fixture-non-semantic"))

        chunks: list[TextChunk] = []
        if raw_chunks:
            for item in raw_chunks:
                if isinstance(item, TextChunk):
                    chunks.append(item)
                elif isinstance(item, dict):
                    chunks.append(TextChunk.model_validate(item))
        elif texts:
            for idx, text in enumerate(texts):
                chunks.append(
                    TextChunk.create(
                        document_id="input_texts",
                        chunk_index=idx,
                        text=str(text),
                        start_char=0,
                        end_char=len(str(text)),
                        token_count=max(1, len(str(text).split())),
                    )
                )
        else:
            return ModuleResult.failure(
                error_message="Either 'texts' or 'chunks' must be provided.",
                error_code="INVALID_INPUT",
            )

        # Update lineage if different from default
        lineage = IndexLineage(
            chunker_name="module_invoker",
            chunker_config_hash="default",
            model_name=model_name,
            model_revision="v1",
            dimensions=dimensions,
        )

        orchestrator = self.orchestrator
        if (
            orchestrator.lineage.dimensions != dimensions
            or orchestrator.lineage.model_name != model_name
        ):
            provider = DeterministicEmbeddingProvider(
                model_id=model_name,
                dimensions=dimensions,
            )
            orchestrator = EmbeddingOrchestrator(
                provider=provider,
                lineage=lineage,
                cache=orchestrator.cache,
                budget=orchestrator.budget,
            )

        try:
            result = await orchestrator.embed_chunks(chunks, context=context)
            return ModuleResult.success(
                outputs={
                    "embeddings": [ec.embedding for ec in result.embedded_chunks],
                    "embedded_chunks": [ec.model_dump() for ec in result.embedded_chunks],
                    "lineage_id": result.lineage.lineage_id,
                    "dimensions": result.lineage.dimensions,
                    "cached_count": result.cached_count,
                    "computed_count": result.computed_count,
                    "total_tokens": result.total_tokens,
                }
            )
        except EmbeddingError as exc:
            return ModuleResult.failure(
                error_message=str(exc),
                error_code="EMBEDDING_ERROR",
            )
        except Exception as exc:
            return ModuleResult.failure(
                error_message=f"Embedding invocation failed: {exc}",
                error_code="EXECUTION_FAILED",
            )
