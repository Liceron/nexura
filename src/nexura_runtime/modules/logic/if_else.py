# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Route a single condition through the engine's branch contract."""

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


class IfElseModule(ComponentModule):
    routes = True
    manifest = manifest(
        "logic",
        "if_else",
        "Evaluate a condition and select the true or false workflow branch.",
        {"left": {}, "right": {}, "operator": choice(*OPERATORS)},
        ["left", "right", "operator"],
        {"result": BOOLEAN, "route": choice("true", "false")},
        {"left": 5, "right": 3, "operator": "gt"},
    )

    def transform(self, inputs: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        matched = Condition().matches(inputs["left"], inputs["operator"], inputs["right"])
        return {"result": matched, "route": "true" if matched else "false"}
