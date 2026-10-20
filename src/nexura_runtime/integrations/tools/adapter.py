# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Adapter extending @tool definitions into the B00/B01 module contract."""

from __future__ import annotations

from typing import Any

from nexura_agents.contracts.base import BaseModule
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.manifest import EffectPolicy, ModuleManifest
from nexura_agents.contracts.result import ModuleResult
from nexura_agents.tools import Tool


class ToolModule(BaseModule):
    """B00/B01 module wrapping a typed @tool or Tool instance."""

    def __init__(
        self,
        tool: Tool,
        *,
        effect_policy: EffectPolicy = "idempotent_write",
        module_id: str | None = None,
        version: str = "1.0.0",
    ) -> None:
        self.tool = tool
        mod_id = module_id or f"nexura.tools.{tool.name}"

        # Clean parameter schema for module contract input validation
        input_schema = tool.args_schema.model_json_schema()
        input_schema.pop("title", None)

        self._manifest = ModuleManifest(
            schema_version=1,
            module_id=mod_id,
            version=version,
            kind="tool",
            title=f"Tool: {tool.name}",
            description=tool.description,
            input_schema=input_schema,
            output_schema={
                "type": "object",
                "properties": {
                    "result": {},
                },
            },
            config_schema={"type": "object"},
            named_ports=[],
            capabilities={"streaming": False, "network": False},
            effect_policy=effect_policy,
            dependency_extra=None,
            min_sdk_version="0.3.0",
            timeout_seconds=30.0,
            output_limit_bytes=2_000_000,
            required_credential_types=[],
            supported_modalities=["text"],
            supported_locales=["all"],
            source_reference=f"tool:{tool.name}",
            verification_status="mock_verified",
        )

    @property
    def manifest(self) -> ModuleManifest:
        """Module manifest defining metadata, schemas, and policies."""
        return self._manifest

    async def invoke(
        self,
        context: ExecutionContext,
        inputs: dict[str, Any],
        config: dict[str, Any] | None = None,
    ) -> ModuleResult:
        # Schema validation has already succeeded in BaseModule.execute()
        # prior to calling this method, guaranteeing zero tool effects on invalid args.
        result = await self.tool.arun(**inputs)
        return ModuleResult.success(outputs={"result": result})


def tool_to_module(
    tool: Tool,
    *,
    effect_policy: EffectPolicy = "idempotent_write",
    module_id: str | None = None,
) -> ToolModule:
    """Convert an existing @tool or Tool instance into a B00/B01 BaseModule."""
    return ToolModule(tool, effect_policy=effect_policy, module_id=module_id)
