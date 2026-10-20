# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""B00/B01 module contract adapter for vector storage, search, and generation management."""

from __future__ import annotations

from typing import Any

from nexura_agents.contracts.base import BaseModule
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.manifest import EffectPolicy, ModuleManifest
from nexura_agents.contracts.result import ModuleResult

from nexura_runtime.knowledge.chunks.models import IndexLineage
from nexura_runtime.knowledge.vector.errors import VectorStoreError
from nexura_runtime.knowledge.vector.models import (
    VectorRecord,
    VectorSearchQuery,
)
from nexura_runtime.knowledge.vector.store import InMemoryVectorStore, VectorStore


class VectorStoreModule(BaseModule):
    """B00/B01 module wrapping vector upsert, search, deletion, and index generations."""

    def __init__(
        self,
        store: VectorStore | None = None,
        *,
        module_id: str = "nexura.knowledge.vector",
        version: str = "1.0.0",
        effect_policy: EffectPolicy = "idempotent_write",
    ) -> None:
        self.store = store or InMemoryVectorStore()

        self._manifest = ModuleManifest(
            schema_version=1,
            module_id=module_id,
            version=version,
            kind="knowledge",
            title="Vector Storage and Index Manager",
            description=(
                "Manages dense vector storage, exact and approximate similarity search, "
                "metadata filtering, and atomic index generations."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": [
                            "create_collection",
                            "upsert",
                            "search",
                            "delete",
                            "create_generation",
                            "activate_generation",
                            "count",
                        ],
                        "default": "search",
                        "description": "Vector store operation to execute",
                    },
                    "collection_id": {
                        "type": "string",
                        "default": "default",
                        "description": "Target collection identifier",
                    },
                    "dimensions": {
                        "type": "integer",
                        "description": "Vector dimensions for collection or generation creation",
                    },
                    "records": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "List of vector record dictionaries for upsert",
                    },
                    "vector": {
                        "type": "array",
                        "items": {"type": "number"},
                        "description": "Query vector for search",
                    },
                    "top_k": {
                        "type": "integer",
                        "default": 10,
                        "description": "Maximum search results to return",
                    },
                    "filter": {
                        "type": "object",
                        "description": "Metadata filter expression",
                    },
                    "ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of record IDs to delete",
                    },
                    "generation_id": {
                        "type": "string",
                        "description": "Target generation identifier",
                    },
                },
                "required": ["operation", "collection_id"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                    "operation": {"type": "string"},
                    "results": {"type": "array", "items": {"type": "object"}},
                    "count": {"type": "integer"},
                    "generation_id": {"type": "string"},
                },
                "required": ["operation"],
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
            source_reference="knowledge:vector",
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
        op = str(inputs.get("operation", "search")).lower()
        col_id = str(inputs.get("collection_id", "default"))
        ws_id = context.workspace_id

        try:
            if op == "create_collection":
                dims = int(inputs.get("dimensions", 8))
                gen = await self.store.create_collection(
                    workspace_id=ws_id,
                    collection_id=col_id,
                    dimensions=dims,
                )
                return ModuleResult.success(
                    outputs={
                        "operation": op,
                        "generation_id": gen.generation_id,
                        "dimensions": gen.dimensions,
                        "count": gen.record_count,
                    }
                )

            elif op == "upsert":
                raw_records = inputs.get("records", [])
                records: list[VectorRecord] = []
                for item in raw_records:
                    if isinstance(item, VectorRecord):
                        records.append(item)
                    elif isinstance(item, dict):
                        # Construct VectorRecord with defaults if missing
                        records.append(
                            VectorRecord(
                                id=item["id"],
                                workspace_id=ws_id,
                                collection_id=col_id,
                                generation_id=item.get("generation_id", ""),
                                vector=item["vector"],
                                document_id=item.get("document_id", ""),
                                text=item.get("text", ""),
                                metadata=item.get("metadata", {}),
                            )
                        )
                gen_id = inputs.get("generation_id")
                upserted = await self.store.upsert(
                    workspace_id=ws_id,
                    collection_id=col_id,
                    records=records,
                    generation_id=gen_id,
                )
                return ModuleResult.success(
                    outputs={
                        "operation": op,
                        "count": upserted,
                    }
                )

            elif op == "search":
                vector = inputs.get("vector", [])
                top_k = int(inputs.get("top_k", 10))
                filter_expr = inputs.get("filter")
                gen_id = inputs.get("generation_id")
                query = VectorSearchQuery(
                    vector=vector,
                    collection_id=col_id,
                    workspace_id=ws_id,
                    top_k=top_k,
                    filter=filter_expr,
                    generation_id=gen_id,
                )
                hits = await self.store.search(workspace_id=ws_id, query=query)
                return ModuleResult.success(
                    outputs={
                        "operation": op,
                        "results": [h.model_dump() for h in hits],
                        "count": len(hits),
                    }
                )

            elif op == "delete":
                ids = list(inputs.get("ids", []))
                hard_delete = bool(inputs.get("hard_delete", False))
                gen_id = inputs.get("generation_id")
                deleted = await self.store.delete(
                    workspace_id=ws_id,
                    collection_id=col_id,
                    ids=ids,
                    hard_delete=hard_delete,
                    generation_id=gen_id,
                )
                return ModuleResult.success(
                    outputs={
                        "operation": op,
                        "count": deleted,
                    }
                )

            elif op == "create_generation":
                dims = int(inputs.get("dimensions", 8))
                model_name = str(inputs.get("model_name", "custom_model"))
                lin = IndexLineage(
                    chunker_name="module_rebuild",
                    chunker_config_hash="default",
                    model_name=model_name,
                    model_revision="v1",
                    dimensions=dims,
                )
                gen = await self.store.create_generation(
                    workspace_id=ws_id,
                    collection_id=col_id,
                    lineage=lin,
                    dimensions=dims,
                )
                return ModuleResult.success(
                    outputs={
                        "operation": op,
                        "generation_id": gen.generation_id,
                        "dimensions": gen.dimensions,
                    }
                )

            elif op == "activate_generation":
                gen_id = str(inputs.get("generation_id", ""))
                await self.store.activate_generation(
                    workspace_id=ws_id,
                    collection_id=col_id,
                    generation_id=gen_id,
                )
                return ModuleResult.success(
                    outputs={
                        "operation": op,
                        "generation_id": gen_id,
                    }
                )

            elif op == "count":
                gen_id = inputs.get("generation_id")
                cnt = await self.store.count(
                    workspace_id=ws_id,
                    collection_id=col_id,
                    generation_id=gen_id,
                )
                return ModuleResult.success(
                    outputs={
                        "operation": op,
                        "count": cnt,
                    }
                )

            else:
                return ModuleResult.failure(
                    error_message=f"Unknown operation: '{op}'",
                    error_code="INVALID_OPERATION",
                )

        except VectorStoreError as exc:
            return ModuleResult.failure(
                error_message=str(exc),
                error_code=exc.code,
            )
        except Exception as exc:
            return ModuleResult.failure(
                error_message=f"Vector store operation failed: {exc}",
                error_code="EXECUTION_FAILED",
            )
