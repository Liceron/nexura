# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Compare JSON values using the classification operator vocabulary."""

from typing import Any

from nexura_agents.contracts.context import ExecutionContext

from nexura_runtime.modules._component import (
    BOOLEAN,
    OPERATORS,
    ComponentModule,
    Condition,
    choice,
    manifest,
)


class CompareModule(ComponentModule):
    manifest = manifest(
        "logic",
        "compare",
        "Compare left and right with a classify operator, including guarded regex.",
        {"left": {}, "right": {}, "operator": choice(*OPERATORS)},
        ["left", "right", "operator"],
        {"result": BOOLEAN},
        {"left": 5, "right": 3, "operator": "gt"},
    )

    def transform(self, inputs: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        return {"result": Condition().matches(inputs["left"], inputs["operator"], inputs["right"])}
