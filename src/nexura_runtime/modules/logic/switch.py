# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""First-match ordered branch selection."""

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
    obj,
)


class SwitchModule(ComponentModule):
    routes = True
    manifest = manifest(
        "logic",
        "switch",
        "Choose first matching case (whole data or dotted field), otherwise default_route.",
        {
            "data": {},
            "cases": array(
                obj(
                    {"field": PATH, "operator": choice(*OPERATORS), "value": {}, "route": PATH},
                    ["operator", "value", "route"],
                )
            ),
            "default_route": PATH,
        },
        ["data", "cases"],
        {"route": PATH},
        {"data": 5, "cases": [{"operator": "gt", "value": 3, "route": "large"}]},
    )

    def transform(self, inputs: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        condition = Condition()
        for case in inputs["cases"]:
            actual = get_field(inputs["data"], case["field"]) if "field" in case else inputs["data"]
            if condition.matches(actual, case["operator"], case["value"]):
                return {"route": case["route"]}
        return {"route": inputs.get("default_route", "default")}
