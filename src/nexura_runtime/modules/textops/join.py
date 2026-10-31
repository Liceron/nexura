# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Combine text items."""

from typing import Any

from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.errors import SchemaValidationError

from nexura_runtime.modules._component import TEXT, ComponentModule, array, manifest


class JoinModule(ComponentModule):
    manifest = manifest(
        "text",
        "join",
        "Combine text items with a separator.",
        {"items": array(TEXT), "separator": TEXT},
        ["items"],
        {"text": TEXT},
        {"items": ["hello", "world"], "separator": " "},
    )

    def transform(self, inputs: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        items = inputs["items"]
        separator = inputs.get("separator", "")
        length = sum(len(item) for item in items) + max(0, len(items) - 1) * len(separator)
        if length > 65536:
            raise SchemaValidationError("Joined text exceeds 65536 characters.")
        return {"text": separator.join(items)}
