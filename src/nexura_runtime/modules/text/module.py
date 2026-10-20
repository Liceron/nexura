# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Module wrapper exposing PromptTemplate via B00/B01 contracts."""

from __future__ import annotations

from typing import Any

from nexura_agents.contracts.base import BaseModule
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.manifest import ModuleManifest
from nexura_agents.contracts.result import ModuleResult

from nexura_runtime.modules.text.template import PromptTemplate


class PromptTemplateModule(BaseModule):
    """B00/B01 module for rendering safe, deterministic prompt templates."""

    manifest = ModuleManifest(
        schema_version=1,
        module_id="nexura.prompt.template",
        version="1.0.0",
        kind="data_transform",
        title="Prompt Template Renderer",
        description="Safe, deterministic prompt template interpolation with variable validation.",
        input_schema={
            "type": "object",
            "properties": {
                "template": {"type": "string"},
                "variables": {"type": "object"},
                "max_output_length": {"type": "integer"},
            },
            "required": ["template"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "rendered": {"type": "string"},
                "variables_used": {"type": "array"},
                "length": {"type": "integer"},
            },
        },
        config_schema={
            "type": "object",
            "properties": {
                "default_variables": {"type": "object"},
                "max_output_length": {"type": "integer"},
            },
        },
        named_ports=[],
        capabilities={"streaming": False, "network": False},
        effect_policy="read",
        dependency_extra=None,
        min_sdk_version="0.3.0",
        timeout_seconds=10.0,
        output_limit_bytes=2_000_000,
        required_credential_types=[],
        supported_modalities=["text"],
        supported_locales=["en", "hi", "all"],
        source_reference="src/nexura_runtime/modules/text/",
        verification_status="mock_verified",
    )

    async def invoke(
        self,
        context: ExecutionContext,
        inputs: dict[str, Any],
        config: dict[str, Any] | None = None,
    ) -> ModuleResult:
        cfg = config or {}
        template_str = inputs["template"]
        variables = inputs.get("variables", {})

        # Configuration options cannot be overridden by user variables
        default_vars = cfg.get("default_variables", {})
        max_len = cfg.get("max_output_length") or inputs.get("max_output_length") or 500_000

        engine = PromptTemplate(
            template_str,
            default_variables=default_vars,
            max_output_length=max_len,
        )

        rendered = engine.render(variables)
        used_vars = sorted(list(engine.get_variables()))

        return ModuleResult.success(
            outputs={
                "rendered": rendered,
                "variables_used": used_vars,
                "length": len(rendered),
            }
        )
