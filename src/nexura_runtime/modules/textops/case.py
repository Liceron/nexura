# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Text case conversion."""

import re
from typing import Any

from nexura_agents.contracts.context import ExecutionContext

from nexura_runtime.modules._component import TEXT, ComponentModule, choice, manifest


class CaseModule(ComponentModule):
    manifest = manifest(
        "text",
        "case",
        "Convert text case, splitting camel case and acronym boundaries for identifiers.",
        {"text": TEXT, "mode": choice("lower", "upper", "title", "snake", "kebab", "camel")},
        ["text", "mode"],
        {"text": TEXT},
        {"text": "Hello World", "mode": "snake"},
    )

    def transform(self, inputs: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        text, mode = inputs["text"], inputs["mode"]
        if mode in {"lower", "upper", "title"}:
            return {"text": getattr(text, mode)()}
        # Zero-width lookarounds test each position once, so long capital runs stay linear.
        text = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", text)
        text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
        words = re.findall(r"[^\W_]+", text, re.UNICODE)
        words = [w.lower() for w in words]
        if mode == "camel":
            result = words[0] + "".join(w.title() for w in words[1:]) if words else ""
        else:
            result = ("_" if mode == "snake" else "-").join(words)
        return {"text": result}
