# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Plain-text extraction for a documented subset of Markdown."""

import re
from typing import Any

from nexura_agents.contracts.context import ExecutionContext

from nexura_runtime.modules._component import TEXT, ComponentModule, manifest
from nexura_runtime.modules.textops.html_to_text import HtmlToTextModule


class MarkdownToTextModule(ComponentModule):
    manifest = manifest(
        "text",
        "markdown_to_text",
        "Remove common Markdown markers; keep link labels, image alt text and code contents.",
        {"text": TEXT},
        ["text"],
        {"text": TEXT},
        {"text": "# Hello **world**"},
    )

    def transform(self, inputs: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        text = inputs["text"]
        # Each pattern is linear in the input: indentation never spans lines, link parts
        # cannot contain their own opening bracket, and emphasis spans are bounded.
        text = re.sub(r"(?m)^[ \t]*(```|~~~)[^\n]*$", "", text)
        text = re.sub(r"!?\[([^\[\]\n]*)\]\([^()\n]*\)", r"\1", text)
        text = re.sub(r"(?m)^[ \t]{0,3}(?:#{1,6}[ \t]+|>[ \t]?|[-+*][ \t]+|\d+\.[ \t]+)", "", text)
        text = re.sub(r"(?m)^[ \t]*[-=_]{3,}[ \t]*$", "", text)
        text = re.sub(r"(`{1,3}|\*{1,3}|_{1,3}|~~)([^\n]{1,200}?)\1", r"\2", text)
        return HtmlToTextModule().transform({"text": text}, context)
