# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Lightweight, self-contained JSON Schema validator.

Supports essential JSON Schema Draft 7/2020-12 constructs without heavyweight external dependencies:
- type (string, number, integer, boolean, object, array, null)
- properties, required, additionalProperties
- enum
- items
- minimum, maximum, minLength, maxLength, minItems, maxItems
"""

from __future__ import annotations

import math
from typing import Any

from nexura_agents.contracts.errors import SchemaValidationError


def validate_schema(
    data: Any,
    schema: dict[str, Any] | None,
    *,
    path: str = "$",
    schema_kind: str = "input",
) -> None:
    """Validate data against a JSON schema dictionary.

    Raises:
        SchemaValidationError: If validation fails.
    """
    if not schema:
        return

    # 1. Type validation
    expected_type = schema.get("type")
    if expected_type is not None:
        types = [expected_type] if isinstance(expected_type, str) else expected_type
        matched = False
        for t in types:
            if t == "string" and isinstance(data, str):
                matched = True
                break
            if t == "integer" and isinstance(data, int) and not isinstance(data, bool):
                matched = True
                break
            if (
                t == "number"
                and isinstance(data, (int, float))
                and not isinstance(data, bool)
                and math.isfinite(data)
            ):
                matched = True
                break
            if t == "boolean" and isinstance(data, bool):
                matched = True
                break
            if t == "null" and data is None:
                matched = True
                break
            if t == "array" and isinstance(data, (list, tuple)):
                matched = True
                break
            if t == "object" and isinstance(data, dict):
                matched = True
                break

        if not matched:
            raise SchemaValidationError(
                f"Validation error at '{path}': expected type {expected_type}, "
                f"got {type(data).__name__}",
                schema_kind=schema_kind,
                details={
                    "path": path,
                    "expected_type": expected_type,
                    "actual_type": type(data).__name__,
                },
            )

    # 2. Enum validation
    if "enum" in schema and data not in schema["enum"]:
        raise SchemaValidationError(
            f"Validation error at '{path}': value {data!r} is not one of {schema['enum']}",
            schema_kind=schema_kind,
            details={"path": path, "allowed_values": schema["enum"], "actual_value": data},
        )

    # 3. String constraints
    if isinstance(data, str):
        if "minLength" in schema and len(data) < schema["minLength"]:
            raise SchemaValidationError(
                f"Validation error at '{path}': string length {len(data)} "
                f"is less than minLength {schema['minLength']}",
                schema_kind=schema_kind,
                details={
                    "path": path,
                    "minLength": schema["minLength"],
                    "actualLength": len(data),
                },
            )
        if "maxLength" in schema and len(data) > schema["maxLength"]:
            raise SchemaValidationError(
                f"Validation error at '{path}': string length {len(data)} "
                f"exceeds maxLength {schema['maxLength']}",
                schema_kind=schema_kind,
                details={
                    "path": path,
                    "maxLength": schema["maxLength"],
                    "actualLength": len(data),
                },
            )

    # 4. Number constraints
    if isinstance(data, (int, float)) and not isinstance(data, bool):
        if "minimum" in schema and data < schema["minimum"]:
            raise SchemaValidationError(
                f"Validation error at '{path}': value {data} "
                f"is less than minimum {schema['minimum']}",
                schema_kind=schema_kind,
                details={"path": path, "minimum": schema["minimum"], "actual_value": data},
            )
        if "maximum" in schema and data > schema["maximum"]:
            raise SchemaValidationError(
                f"Validation error at '{path}': value {data} exceeds maximum {schema['maximum']}",
                schema_kind=schema_kind,
                details={"path": path, "maximum": schema["maximum"], "actual_value": data},
            )

    # 5. Object constraints
    if isinstance(data, dict):
        required_fields = schema.get("required", [])
        for field in required_fields:
            if field not in data:
                raise SchemaValidationError(
                    f"Validation error at '{path}': missing required field '{field}'",
                    schema_kind=schema_kind,
                    details={"path": f"{path}.{field}", "missing_field": field},
                )

        properties = schema.get("properties", {})
        additional_allowed = schema.get("additionalProperties", True)

        if additional_allowed is False:
            extra_keys = set(data.keys()) - set(properties.keys())
            if extra_keys:
                raise SchemaValidationError(
                    f"Validation error at '{path}': unexpected extra properties "
                    f"{sorted(extra_keys)}",
                    schema_kind=schema_kind,
                    details={"path": path, "extra_properties": sorted(extra_keys)},
                )

        for prop_name, prop_schema in properties.items():
            if prop_name in data:
                validate_schema(
                    data[prop_name],
                    prop_schema,
                    path=f"{path}.{prop_name}",
                    schema_kind=schema_kind,
                )

    # 6. Array constraints
    if isinstance(data, (list, tuple)):
        # A malformed bound (not a non-negative integer) is ignored, as before it was enforced.
        min_items, max_items = schema.get("minItems"), schema.get("maxItems")
        if not (isinstance(min_items, int) and not isinstance(min_items, bool) and min_items >= 0):
            min_items = None
        if not (isinstance(max_items, int) and not isinstance(max_items, bool) and max_items >= 0):
            max_items = None
        if min_items is not None and len(data) < min_items:
            raise SchemaValidationError(
                f"Validation error at '{path}': {len(data)} items "
                f"is less than minItems {schema['minItems']}",
                schema_kind=schema_kind,
                details={"path": path, "minItems": schema["minItems"], "actualItems": len(data)},
            )
        if max_items is not None and len(data) > max_items:
            raise SchemaValidationError(
                f"Validation error at '{path}': {len(data)} items "
                f"exceeds maxItems {schema['maxItems']}",
                schema_kind=schema_kind,
                details={"path": path, "maxItems": schema["maxItems"], "actualItems": len(data)},
            )
    if isinstance(data, (list, tuple)) and "items" in schema:
        item_schema = schema["items"]
        if isinstance(item_schema, dict):
            for i, item in enumerate(data):
                validate_schema(
                    item,
                    item_schema,
                    path=f"{path}[{i}]",
                    schema_kind=schema_kind,
                )
