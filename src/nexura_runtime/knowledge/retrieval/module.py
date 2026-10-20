# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""B00/B01 module contract adapter for citation-backed RAG and retrieval."""

from __future__ import annotations

from typing import Any

from nexura_agents.contracts.base import BaseModule
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.manifest import EffectPolicy, ModuleManifest
from nexura_agents.contracts.result import ModuleResult

from nexura_runtime.knowledge.retrieval.errors import RetrievalError
from nexura_runtime.knowledge.retrieval.models import RagQuery
from nexura_runtime.knowledge.retrieval.pipeline import RagPipeline


class RagModule(BaseModule):
    """B00/B01 module wrapping hybrid retrieval, evidence packing, and citation-backed RAG."""

    def __init__(
        self,
        pipeline: RagPipeline | None = None,
        *,
        module_id: str = "nexura.knowledge.retrieval",
        version: str = "1.0.0",
        effect_policy: EffectPolicy = "read",
    ) -> None:
        self.pipeline = pipeline or RagPipeline()

        self._manifest = ModuleManifest(
            schema_version=1,
            module_id=module_id,
            version=version,
            kind="knowledge",
            title="Citation-Backed RAG and Retrieval",
            description=(
                "Executes hybrid dense/lexical retrieval, evidence context packing, "
                "and citation-backed answer generation with strict budget bounds."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "User question or retrieval query",
                    },
                    "collection_id": {
                        "type": "string",
                        "default": "default",
                        "description": "Target collection identifier",
                    },
                    "top_k": {
                        "type": "integer",
                        "default": 5,
                        "description": "Maximum source chunks to retrieve",
                    },
                    "max_context_tokens": {
                        "type": "integer",
                        "default": 2000,
                        "description": "Maximum token budget for packed context",
                    },
                    "generate_answer": {
                        "type": "boolean",
                        "default": True,
                        "description": "Whether to generate answer or return packed context only",
                    },
                    "rerank": {
                        "type": "boolean",
                        "default": False,
                        "description": "Whether to perform post-retrieval reranking",
                    },
                    "allowed_document_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional document permission whitelist",
                    },
                },
                "required": ["query"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "answer": {"type": "string"},
                    "has_sufficient_evidence": {"type": "boolean"},
                    "citations": {"type": "array", "items": {"type": "object"}},
                    "formatted_context": {"type": "string"},
                    "total_tokens": {"type": "integer"},
                },
                "required": [
                    "answer",
                    "has_sufficient_evidence",
                    "citations",
                    "formatted_context",
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
            source_reference="knowledge:retrieval",
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
        query_text = str(inputs.get("query", ""))
        collection_id = str(inputs.get("collection_id", "default"))
        top_k = int(inputs.get("top_k", 5))
        max_context_tokens = int(inputs.get("max_context_tokens", 2000))
        generate_answer = bool(inputs.get("generate_answer", True))
        rerank = bool(inputs.get("rerank", False))
        allowed_docs = inputs.get("allowed_document_ids")

        rag_q = RagQuery(
            query=query_text,
            collection_id=collection_id,
            workspace_id=context.workspace_id,
            top_k=top_k,
            max_context_tokens=max_context_tokens,
            allowed_document_ids=allowed_docs,
            rerank=rerank,
        )

        try:
            if generate_answer:
                resp = await self.pipeline.answer(rag_q, context=context)
                citations_data = [c.model_dump() for c in resp.citations]
                return ModuleResult.success(
                    outputs={
                        "answer": resp.answer,
                        "has_sufficient_evidence": resp.has_sufficient_evidence,
                        "citations": citations_data,
                        "formatted_context": resp.packed_context.formatted_context,
                        "total_tokens": resp.packed_context.total_tokens,
                    }
                )
            else:
                packed = await self.pipeline.retrieve(rag_q, context=context)
                citations_data = [c.model_dump() for c in packed.citations]
                return ModuleResult.success(
                    outputs={
                        "answer": "",
                        "has_sufficient_evidence": packed.has_sufficient_evidence,
                        "citations": citations_data,
                        "formatted_context": packed.formatted_context,
                        "total_tokens": packed.total_tokens,
                    }
                )
        except RetrievalError as exc:
            return ModuleResult.failure(
                error_message=str(exc),
                error_code=exc.code,
            )
        except Exception as exc:
            return ModuleResult.failure(
                error_message=f"RAG execution failed: {exc}",
                error_code="EXECUTION_FAILED",
            )
