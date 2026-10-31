# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Shared contracts and resource bounds for deterministic builder components."""

from __future__ import annotations

import json
import math
import time
from abc import abstractmethod
from typing import Any

import regex  # type: ignore[import-untyped]
from nexura_agents.contracts.base import BaseModule
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.errors import SchemaValidationError
from nexura_agents.contracts.manifest import DisplayMetadata, ModuleManifest
from nexura_agents.contracts.result import ModuleResult

MAX_BYTES = 131_072
MAX_OUTPUT = 262_144
MAX_ITEMS = 1024
TEXT = {"type": "string", "maxLength": 65_536}
OBJECT = {"type": "object"}
BOOLEAN = {"type": "boolean"}
NUMBER = {"type": "number"}
PATH = {"type": "string", "minLength": 1, "maxLength": 256}
OPERATORS = ["eq", "ne", "gt", "gte", "lt", "lte", "in", "contains", "regex"]


def choice(*values: str) -> dict[str, Any]:
    return {"type": "string", "enum": list(values)}


def integer(minimum: int = 0, maximum: int = MAX_ITEMS) -> dict[str, Any]:
    return {"type": "integer", "minimum": minimum, "maximum": maximum}


def array(items: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"type": "array", "items": items or {}, "maxItems": MAX_ITEMS}


def obj(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def manifest(
    category: str,
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
    outputs: dict[str, Any],
    example: dict[str, Any],
) -> ModuleManifest:
    title = name.replace("_", " ").title()
    return ModuleManifest(
        module_id=f"nexura.{category}.{name}",
        version="1.0.0",
        kind="data_transform",
        title=title,
        description=description,
        input_schema=obj(properties, required),
        output_schema=obj(outputs, list(outputs)),
        config_schema=obj({}, []),
        effect_policy="read",
        capabilities={"network": False, "streaming": False},
        timeout_seconds=2.0,
        output_limit_bytes=MAX_OUTPUT,
        license="Apache-2.0",
        source_reference="src/nexura_runtime/modules/",
        display=DisplayMetadata(label=title, group=category, examples=[example]),
    )


def bounded_json(value: Any, limit: int = MAX_BYTES) -> None:
    """Reject non-JSON, deep, cyclic, oversized and non-finite inputs before processing."""
    stack = [(value, 0)]
    nodes = 0
    estimated = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if depth > 24 or nodes > 16_384:
            raise SchemaValidationError("JSON exceeds depth 24 or 16384 values (or is cyclic).")
        if isinstance(item, dict):
            if len(item) > MAX_ITEMS or any(not isinstance(k, str) for k in item):
                raise SchemaValidationError("Objects require string keys and at most 1024 fields.")
            stack.extend((v, depth + 1) for v in item.values())
            stack.extend((k, depth + 1) for k in item)
        elif isinstance(item, list):
            if len(item) > MAX_ITEMS:
                raise SchemaValidationError("Lists exceed the 1024 item limit.")
            stack.extend((v, depth + 1) for v in item)
        elif isinstance(item, str):
            try:
                estimated += len(item.encode("utf-8"))
            except UnicodeError as exc:
                raise SchemaValidationError("Text must be valid UTF-8 Unicode.") from exc
        elif item is None or isinstance(item, bool):
            estimated += 5
        elif isinstance(item, (int, float)):
            if isinstance(item, int) and item.bit_length() > 4096:
                raise SchemaValidationError("Integer exceeds 4096 bits.")
            if isinstance(item, float) and not math.isfinite(item):
                raise SchemaValidationError("Numbers must be finite.")
            estimated += 32
        else:
            raise SchemaValidationError("Inputs must contain only JSON values.")
        if estimated > limit:
            raise SchemaValidationError(f"JSON exceeds {limit} bytes.")
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")) > limit:
        raise SchemaValidationError(f"JSON exceeds {limit} bytes.")


class ComponentModule(BaseModule):
    """Validate direct invocation as well as registry execution; enforce aggregate limits."""

    routes = False

    async def execute(
        self, context: ExecutionContext, inputs: dict[str, Any], config: dict[str, Any]
    ) -> ModuleResult:
        bounded_json(inputs)
        bounded_json(config)
        return await super().execute(context, inputs, config)

    async def invoke(
        self,
        context: ExecutionContext,
        inputs: dict[str, Any],
        config: dict[str, Any] | None = None,
    ) -> ModuleResult:
        context.check_cancelled()
        bounded_json(inputs)
        bounded_json(config or {})
        self.manifest.validate_inputs(inputs)
        self.manifest.validate_config(config or {})
        started = time.monotonic()
        try:
            outputs = self.transform(inputs, context)
            bounded_json(outputs, MAX_OUTPUT)
            self.manifest.validate_outputs(outputs)
        except SchemaValidationError:
            raise
        except (ValueError, TypeError, KeyError, ArithmeticError, UnicodeError, regex.error) as exc:
            raise SchemaValidationError(f"{self.manifest.module_id}: {exc}") from exc
        context.check_cancelled()
        if time.monotonic() - started > 2:
            raise SchemaValidationError("Component exceeded the 2 second processing limit.")
        return ModuleResult.success(outputs, branch=outputs["route"] if self.routes else None)

    @abstractmethod
    def transform(self, inputs: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        """Compute one bounded, deterministic transformation."""


def compile_regex(pattern: str) -> Any:
    """Use the regex engine's interruptible timeout, including catastrophic patterns."""
    if len(pattern) > 512:
        raise SchemaValidationError("Regex pattern exceeds 512 characters.")
    try:
        return regex.compile(pattern, regex.VERSION0)
    except regex.error as exc:
        raise SchemaValidationError(f"Invalid regex: {exc}") from exc


def get_field(data: Any, path: str) -> Any:
    """Read dictionary-only dotted paths; absent values compare as null."""
    for part in path_parts(path):
        if not isinstance(data, dict) or part not in data:
            return None
        data = data[part]
    return data


def path_parts(path: str) -> list[str]:
    parts = path.split(".")
    if not path or len(path) > 256 or any(not p for p in parts) or len(parts) > 24:
        raise SchemaValidationError(
            "Dotted paths require 1-24 nonempty segments, at most 256 chars."
        )
    return parts


def set_field(data: dict[str, Any], path: str, value: Any) -> None:
    parts = path_parts(path)
    current = data
    for part in parts[:-1]:
        if part not in current:
            current[part] = {}
        if not isinstance(current[part], dict):
            raise SchemaValidationError(f"Dotted path conflicts with scalar at '{part}'.")
        current = current[part]
    current[parts[-1]] = value


class Condition:
    """Classify-compatible operators with one regex budget for the whole collection."""

    def __init__(self) -> None:
        self.deadline = time.monotonic() + 0.05
        self.patterns: dict[str, Any] = {}

    def matches(self, actual: Any, operator: str, target: Any) -> bool:
        # Reuse the classify operators except its unbounded regex implementation.
        from nexura_runtime.modules.data.transform import _evaluate_rule_condition

        if operator not in OPERATORS:
            raise SchemaValidationError(f"Unknown comparison operator: {operator}")
        if operator != "regex":
            return _evaluate_rule_condition(actual, operator, target)
        if not isinstance(target, str):
            raise SchemaValidationError("Regex comparison requires a string pattern.")
        if target not in self.patterns:
            self.patterns[target] = compile_regex(target)
        if not isinstance(actual, str):
            return False
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise SchemaValidationError("Regex comparison exceeded its 50ms budget.")
        try:
            return bool(self.patterns[target].search(actual, timeout=remaining))
        except TimeoutError as exc:
            raise SchemaValidationError("Regex comparison exceeded its 50ms budget.") from exc
