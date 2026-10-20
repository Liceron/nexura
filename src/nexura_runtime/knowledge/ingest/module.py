# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""B00/B01 module contract adapter for document ingestion and parsing."""

from __future__ import annotations

import base64
from typing import Any

from nexura_agents.contracts.base import BaseModule
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.manifest import EffectPolicy, ModuleManifest
from nexura_agents.contracts.result import ModuleResult

from nexura_runtime.knowledge.ingest.pipeline import IngestionPipeline


class DocumentIngestModule(BaseModule):
    """B00/B01 module wrapping document ingestion into collection records."""

    def __init__(
        self,
        pipeline: IngestionPipeline | None = None,
        *,
        module_id: str = "nexura.knowledge.ingest",
        version: str = "1.0.0",
        effect_policy: EffectPolicy = "idempotent_write",
    ) -> None:
        self.pipeline = pipeline or IngestionPipeline()

        self._manifest = ModuleManifest(
            schema_version=1,
            module_id=module_id,
            version=version,
            kind="knowledge",
            title="Document Ingestion Parser",
            description=(
                "Parses and indexes text, CSV, JSON, HTML, PDF, and archive files "
                "into Document records."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "Raw string or base64-encoded binary content",
                    },
                    "filename": {
                        "type": "string",
                        "description": "Origin filename for extension matching",
                    },
                    "mime_type": {
                        "type": "string",
                        "description": "Explicit MIME type override",
                    },
                    "collection_id": {
                        "type": "string",
                        "description": "Target collection identifier",
                        "default": "default",
                    },
                    "is_base64": {
                        "type": "boolean",
                        "description": "Whether content is base64-encoded binary",
                        "default": False,
                    },
                },
                "required": ["content"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "string"},
                    "collection_id": {"type": "string"},
                    "content_hash": {"type": "string"},
                    "mime_type": {"type": "string"},
                    "text": {"type": "string"},
                    "pages_count": {"type": "integer"},
                },
                "required": ["document_id", "content_hash", "text"],
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
            source_reference="knowledge:ingest",
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
        content_raw = str(inputs.get("content", ""))
        filename = str(inputs.get("filename", "document.txt"))
        mime_type = inputs.get("mime_type")
        collection_id = str(inputs.get("collection_id", "default"))
        is_base64 = bool(inputs.get("is_base64", False))

        if is_base64:
            try:
                data = base64.b64decode(content_raw)
            except Exception as exc:
                return ModuleResult.failure(
                    error_message=f"Invalid base64 payload: {exc}",
                    error_code="MALFORMED_INPUT",
                )
        else:
            data = content_raw.encode("utf-8")

        doc = self.pipeline.ingest_bytes(
            data,
            filename=filename,
            mime_type=mime_type,
            collection_id=collection_id,
            workspace_id=context.workspace_id,
        )

        return ModuleResult.success(
            outputs={
                "document_id": doc.document_id,
                "collection_id": doc.collection_id,
                "content_hash": doc.content_hash,
                "mime_type": doc.mime_type,
                "text": doc.text,
                "pages_count": len(doc.pages),
            }
        )
