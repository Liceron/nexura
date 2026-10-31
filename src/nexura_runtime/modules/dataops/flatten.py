# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Reversible dictionary flattening with explicit ambiguity rejection."""

import copy
from typing import Any

from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.errors import SchemaValidationError

from nexura_runtime.modules._component import (
    OBJECT,
    ComponentModule,
    choice,
    manifest,
    path_parts,
    set_field,
)


class FlattenModule(ComponentModule):
    manifest = manifest(
        "data",
        "flatten",
        "Flatten nested object keys to dotted paths or unflatten; arrays stay intact.",
        {"data": OBJECT, "mode": choice("flatten", "unflatten")},
        ["data"],
        {"data": OBJECT},
        {"data": {"user": {"name": "Ada"}}},
    )

    def transform(self, inputs: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if inputs.get("mode", "flatten") == "unflatten":
            paths = set(inputs["data"])
            for path in sorted(paths):
                parts = path_parts(path)
                if any(".".join(parts[:index]) in paths for index in range(1, len(parts))):
                    raise SchemaValidationError("Unflatten paths conflict (parent and child).")
                set_field(result, path, copy.deepcopy(inputs["data"][path]))
        else:

            def visit(data: dict[str, Any], prefix: str = "") -> None:
                for key, value in data.items():
                    if not key or "." in key:
                        raise SchemaValidationError("Flatten requires nonempty keys without dots.")
                    path = prefix + key
                    path_parts(path)
                    if isinstance(value, dict) and value:
                        visit(value, path + ".")
                    else:
                        result[path] = copy.deepcopy(value)

            visit(inputs["data"])
        return {"data": result}
