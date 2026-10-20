# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Module wrapper exposing JSON transforms, merge, and classification routing via B00/B01."""

from __future__ import annotations

from typing import Any

from nexura_agents.contracts.base import BaseModule
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.errors import SchemaValidationError
from nexura_agents.contracts.manifest import ModuleManifest
from nexura_agents.contracts.result import ModuleResult

from nexura_runtime.modules.data.transform import (
    classify_route,
    json_merge,
    json_select,
    reduce_data,
)


class DataTransformModule(BaseModule):
    """B00/B01 module for deterministic JSON selection, merging, routing, and reductions."""

    manifest = ModuleManifest(
        schema_version=1,
        module_id="nexura.data.transform",
        version="1.0.0",
        kind="data_transform",
        title="Data Transformation and Routing",
        description=(
            "Deterministic JSON querying, deep merging with conflict reducers, and routing."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["select", "merge", "classify", "reduce"],
                },
                "data": {},
                "path": {"type": "string"},
                "base": {"type": "object"},
                "override": {"type": "object"},
                "conflict_policy": {"type": "string"},
                "rules": {"type": "array"},
                "default_route": {"type": "string"},
                "items": {"type": "array"},
                "reduce_operation": {"type": "string"},
                "field": {"type": "string"},
            },
            "required": ["operation"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "result": {},
                "route": {"type": "string"},
                "operation": {"type": "string"},
            },
        },
        config_schema={"type": "object"},
        named_ports=[],
        capabilities={"streaming": False, "network": False},
        effect_policy="read",
        dependency_extra=None,
        min_sdk_version="0.3.0",
        timeout_seconds=10.0,
        output_limit_bytes=5_000_000,
        required_credential_types=[],
        supported_modalities=["text"],
        supported_locales=["all"],
        source_reference="src/nexura_runtime/modules/data/",
        verification_status="mock_verified",
    )

    async def invoke(
        self,
        context: ExecutionContext,
        inputs: dict[str, Any],
        config: dict[str, Any] | None = None,
    ) -> ModuleResult:
        operation = inputs["operation"]

        if operation == "select":
            data = inputs.get("data")
            path = inputs.get("path", "")
            strict = inputs.get("strict", False)
            default_val = inputs.get("default")
            selected = json_select(data, path, default=default_val, strict=strict)
            return ModuleResult.success(outputs={"result": selected, "operation": "select"})

        if operation == "merge":
            base = inputs.get("base")
            override = inputs.get("override")
            if not isinstance(base, dict) or not isinstance(override, dict):
                raise SchemaValidationError(
                    "Merge operation requires 'base' and 'override' dictionaries."
                )
            policy = inputs.get("conflict_policy", "error")
            merged = json_merge(base, override, conflict_policy=policy)
            return ModuleResult.success(outputs={"result": merged, "operation": "merge"})

        if operation == "classify":
            data_dict = inputs.get("data")
            rules = inputs.get("rules", [])
            default_r = inputs.get("default_route", "default")
            if not isinstance(data_dict, dict):
                raise SchemaValidationError("Classify operation requires 'data' dictionary.")
            route = classify_route(data_dict, rules, default_route=default_r)
            return ModuleResult.success(
                outputs={"route": route, "result": route, "operation": "classify"}
            )

        if operation == "reduce":
            items = inputs.get("items", [])
            red_op = inputs.get("reduce_operation", "count")
            field = inputs.get("field")
            if not isinstance(items, list):
                raise SchemaValidationError("Reduce operation requires 'items' list.")
            reduced = reduce_data(items, red_op, field=field)
            return ModuleResult.success(outputs={"result": reduced, "operation": "reduce"})

        raise SchemaValidationError(f"Unsupported operation: '{operation}'")
