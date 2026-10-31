# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Truncate text by characters or whitespace-delimited words."""

from typing import Any

from nexura_agents.contracts.context import ExecutionContext

from nexura_runtime.modules._component import (
    BOOLEAN,
    TEXT,
    ComponentModule,
    choice,
    integer,
    manifest,
)


class TruncateModule(ComponentModule):
    manifest = manifest(
        "text",
        "truncate",
        "Keep limit characters or words and append ellipsis only if truncated.",
        {
            "text": TEXT,
            "mode": choice("chars", "words"),
            "limit": integer(0, 65536),
            "ellipsis": {"type": "string", "maxLength": 128},
        },
        ["text", "limit"],
        {"text": TEXT, "truncated": BOOLEAN},
        {"text": "hello world", "limit": 5},
    )

    def transform(self, inputs: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        text, limit = inputs["text"], inputs["limit"]
        units = text.split() if inputs.get("mode", "chars") == "words" else text
        truncated = len(units) > limit
        result = " ".join(units[:limit]) if isinstance(units, list) else units[:limit]
        if not truncated:
            result = text
        return {
            "text": result + inputs.get("ellipsis", "…") if truncated else result,
            "truncated": truncated,
        }
