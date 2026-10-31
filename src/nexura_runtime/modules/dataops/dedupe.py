# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Stable deduplication using canonical JSON keys."""

import json
from typing import Any

from nexura_agents.contracts.context import ExecutionContext

from nexura_runtime.modules._component import PATH, ComponentModule, array, get_field, manifest


class DedupeModule(ComponentModule):
    manifest = manifest(
        "data",
        "dedupe",
        "Keep first item per field (or whole value); compare canonical JSON including value types.",
        {"items": array(), "field": PATH},
        ["items"],
        {"items": array()},
        {"items": [{"id": 1, "name": "Ada"}, {"id": 1, "name": "Other"}], "field": "id"},
    )

    def transform(self, inputs: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        seen = set()
        items = []
        for item in inputs["items"]:
            value = get_field(item, inputs["field"]) if "field" in inputs else item
            key = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            if key not in seen:
                seen.add(key)
                items.append(item)
        return {"items": items}
