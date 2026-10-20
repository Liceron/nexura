# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Module for reading and transforming stored artifacts."""

from __future__ import annotations

import hashlib
from typing import Any

from nexura_agents.contracts import (
    BaseModule,
    ExecutionContext,
    ModuleManifest,
    ModuleResult,
    PortDefinition,
)

from nexura_runtime.artifacts.errors import ArtifactError
from nexura_runtime.artifacts.store import LocalArtifactStore


class ArtifactReaderModule(BaseModule):
    """Reads stored artifacts within the active workspace and emits text content."""

    @property
    def manifest(self) -> ModuleManifest:
        return ModuleManifest(
            schema_version=1,
            module_id="nexura.artifacts.reader",
            version="1.0.0",
            kind="transform",
            title="Artifact Reader Module",
            description="Reads an artifact from the workspace artifact store and extracts text.",
            effect_policy="read",
            input_schema={
                "type": "object",
                "properties": {"artifact_id": {"type": "string"}},
                "required": ["artifact_id"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "size_bytes": {"type": "integer"},
                    "digest": {"type": "string"},
                },
                "required": ["content", "size_bytes", "digest"],
                "additionalProperties": False,
            },
            config_schema={
                "type": "object",
                "properties": {"encoding": {"type": "string", "enum": ["utf-8", "ascii"]}},
                "additionalProperties": False,
            },
            named_ports=[
                PortDefinition(name="artifact_id", type="string"),
                PortDefinition(name="content", type="string"),
                PortDefinition(name="size_bytes", type="integer"),
                PortDefinition(name="digest", type="string"),
            ],
            capabilities={"network": False, "streaming": False},
        )

    async def invoke(
        self,
        context: ExecutionContext,
        inputs: dict[str, Any],
        config: dict[str, Any],
    ) -> ModuleResult:
        store = context.artifact_store
        if store is None and "artifact_store" in context.attributes:
            store = context.attributes["artifact_store"]

        if store is None:
            raise ArtifactError(
                "An ArtifactStore instance must be provided in context or context.attributes",
                code="INVALID_CONFIG",
            )

        artifact_id = inputs["artifact_id"]
        encoding = config.get("encoding", "utf-8")

        if isinstance(store, LocalArtifactStore):
            data = await store.get(context.workspace_id, artifact_id)
        else:
            raw_data = await store.get(artifact_id)
            if raw_data is None:
                raise ArtifactError(
                    f"Artifact '{artifact_id}' not found",
                    code="ARTIFACT_NOT_FOUND",
                )
            data = raw_data

        text = data.decode(encoding)
        digest = hashlib.sha256(data).hexdigest()

        return ModuleResult.success(
            outputs={
                "content": text,
                "size_bytes": len(data),
                "digest": digest,
            }
        )
