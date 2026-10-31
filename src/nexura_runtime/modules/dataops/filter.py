# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Keep items satisfying a classify-compatible condition."""

from typing import Any

from nexura_agents.contracts.context import ExecutionContext

from nexura_runtime.modules._component import (
    OPERATORS,
    PATH,
    ComponentModule,
    Condition,
    array,
    choice,
    get_field,
    manifest,
)


class FilterModule(ComponentModule):
    manifest = manifest(
        "data",
        "filter",
        "Keep matching items in order; omit field to compare each whole item.",
        {"items": array(), "field": PATH, "operator": choice(*OPERATORS), "value": {}},
        ["items", "operator", "value"],
        {"items": array()},
        {"items": [{"score": 2}, {"score": 7}], "field": "score", "operator": "gte", "value": 5},
    )

    def transform(self, inputs: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        condition = Condition()
        items = [
            item
            for item in inputs["items"]
            if condition.matches(
                get_field(item, inputs["field"]) if "field" in inputs else item,
                inputs["operator"],
                inputs["value"],
            )
        ]
        return {"items": items}
