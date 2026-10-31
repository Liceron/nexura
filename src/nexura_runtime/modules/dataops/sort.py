# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Stable ordering of JSON arrays."""

from typing import Any

from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.errors import SchemaValidationError

from nexura_runtime.modules._component import (
    PATH,
    ComponentModule,
    array,
    choice,
    get_field,
    manifest,
)


class SortModule(ComponentModule):
    manifest = manifest(
        "data",
        "sort",
        "Stable ascending/descending sort on scalar values or dotted fields; "
        "require comparable keys.",
        {"items": array(), "field": PATH, "order": choice("asc", "desc")},
        ["items"],
        {"items": array()},
        {"items": [{"score": 7}, {"score": 2}], "field": "score"},
    )

    def transform(self, inputs: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        def key(item: Any) -> Any:
            value = get_field(item, inputs["field"]) if "field" in inputs else item
            if value is None or isinstance(value, (dict, list)):
                raise SchemaValidationError(
                    "Sort keys must be non-null scalar values; check field paths."
                )
            return value

        return {
            "items": sorted(inputs["items"], key=key, reverse=inputs.get("order", "asc") == "desc")
        }
