# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Bounded text splitting."""

import re
from typing import Any

from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.errors import SchemaValidationError

from nexura_runtime.modules._component import (
    TEXT,
    ComponentModule,
    array,
    choice,
    integer,
    manifest,
)


class SplitModule(ComponentModule):
    manifest = manifest(
        "text",
        "split",
        "Split by separator, line, sentence or overlapping fixed character windows.",
        {
            "text": TEXT,
            "mode": choice("separator", "line", "sentence", "fixed"),
            "separator": TEXT,
            "size": integer(1, 65536),
            "overlap": integer(0, 65535),
        },
        ["text"],
        {"items": array(TEXT)},
        {"text": "a,b", "separator": ","},
    )

    def transform(self, inputs: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        text = inputs["text"]
        mode = inputs.get("mode", "separator")
        if mode == "line":
            parts = text.splitlines()
        elif mode == "sentence":
            parts = re.split(r"(?<=[.!?])\s+", text.strip()) if text.strip() else []
        elif mode == "fixed":
            size, overlap = inputs.get("size", 1000), inputs.get("overlap", 0)
            if overlap >= size:
                raise SchemaValidationError("Overlap must be smaller than size.")
            parts = []
            for start in range(0, len(text), size - overlap):
                parts.append(text[start : start + size])
                if len(parts) > 1024:
                    raise SchemaValidationError("Split exceeds 1024 chunks.")
                if start + size >= len(text):
                    break
        else:
            separator = inputs.get("separator", ",")
            if not separator:
                raise SchemaValidationError("Separator must not be empty.")
            parts = text.split(separator)
        return {"items": parts}
