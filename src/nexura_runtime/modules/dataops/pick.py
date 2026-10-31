# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Select fields while preserving nested structure."""

import copy
from typing import Any

from nexura_agents.contracts.context import ExecutionContext

from nexura_runtime.modules._component import (
    OBJECT,
    PATH,
    ComponentModule,
    array,
    get_field,
    manifest,
    set_field,
)


class PickModule(ComponentModule):
    manifest = manifest(
        "data",
        "pick",
        "Keep dotted fields as a nested object; missing fields become null.",
        {"data": OBJECT, "fields": array(PATH)},
        ["data", "fields"],
        {"data": OBJECT},
        {"data": {"user": {"name": "Ada", "age": 20}}, "fields": ["user.name"]},
    )

    def transform(self, inputs: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for path in inputs["fields"]:
            set_field(result, path, copy.deepcopy(get_field(inputs["data"], path)))
        return {"data": result}
