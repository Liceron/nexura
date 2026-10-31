# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Group list items by typed JSON field values."""

import json
from typing import Any

from nexura_agents.contracts.context import ExecutionContext

from nexura_runtime.modules._component import (
    OBJECT,
    PATH,
    ComponentModule,
    array,
    get_field,
    integer,
    manifest,
    obj,
)


class GroupByModule(ComponentModule):
    manifest = manifest(
        "data",
        "group_by",
        "Group by a dotted field, in first-seen order; emit typed keys, lists and counts.",
        {"items": array(OBJECT), "field": PATH},
        ["items", "field"],
        {
            "groups": array(
                obj(
                    {"key": {}, "items": array(OBJECT), "count": integer()},
                    ["key", "items", "count"],
                )
            )
        },
        {"items": [{"team": "A"}, {"team": "A"}], "field": "team"},
    )

    def transform(self, inputs: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        groups: dict[str, Any] = {}
        for item in inputs["items"]:
            value = get_field(item, inputs["field"])
            key = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            if key not in groups:
                groups[key] = {"key": value, "items": [], "count": 0}
            groups[key]["items"].append(item)
            groups[key]["count"] += 1
        return {"groups": list(groups.values())}
