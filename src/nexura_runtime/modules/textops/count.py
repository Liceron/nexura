# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Deterministic text statistics."""

from typing import Any

from nexura_agents.contracts.context import ExecutionContext

from nexura_runtime.modules._component import TEXT, ComponentModule, integer, manifest


class CountModule(ComponentModule):
    manifest = manifest(
        "text",
        "count",
        "Count Unicode characters, whitespace words, splitlines "
        "and ceil(chars/4) approximate tokens.",
        {"text": TEXT},
        ["text"],
        {k: integer(0, 65536) for k in ("chars", "words", "lines", "approximate_tokens")},
        {"text": "hello world"},
    )

    def transform(self, inputs: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        text = inputs["text"]
        return {
            "chars": len(text),
            "words": len(text.split()),
            "lines": len(text.splitlines()),
            "approximate_tokens": (len(text) + 3) // 4,
        }
