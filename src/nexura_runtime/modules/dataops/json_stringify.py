# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Deterministic JSON serialization."""

import json
from typing import Any

from nexura_agents.contracts.context import ExecutionContext

from nexura_runtime.modules._component import BOOLEAN, TEXT, ComponentModule, manifest


class JsonStringifyModule(ComponentModule):
    manifest = manifest(
        "data",
        "json_stringify",
        "Serialize Unicode JSON compactly or with two-space indentation.",
        {"value": {}, "pretty": BOOLEAN, "sort_keys": BOOLEAN},
        ["value"],
        {"text": TEXT},
        {"value": {"answer": 42}, "pretty": True},
    )

    def transform(self, inputs: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        pretty = inputs.get("pretty", False)
        return {
            "text": json.dumps(
                inputs["value"],
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=inputs.get("sort_keys", False),
                indent=2 if pretty else None,
                separators=None if pretty else (",", ":"),
            )
        }
