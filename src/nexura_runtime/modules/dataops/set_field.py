# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Set a nested field without mutating the input."""

import copy
from typing import Any

from nexura_agents.contracts.context import ExecutionContext

from nexura_runtime.modules._component import OBJECT, PATH, ComponentModule, manifest, set_field


class SetFieldModule(ComponentModule):
    manifest = manifest(
        "data",
        "set_field",
        "Copy an object and set a dotted path, creating intermediate objects.",
        {"data": OBJECT, "field": PATH, "value": {}},
        ["data", "field", "value"],
        {"data": OBJECT},
        {"data": {}, "field": "user.name", "value": "Ada"},
    )

    def transform(self, inputs: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        data = copy.deepcopy(inputs["data"])
        set_field(data, inputs["field"], copy.deepcopy(inputs["value"]))
        return {"data": data}
