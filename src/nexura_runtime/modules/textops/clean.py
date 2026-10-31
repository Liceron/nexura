# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Unicode and whitespace cleaning."""

import unicodedata
from typing import Any

from nexura_agents.contracts.context import ExecutionContext

from nexura_runtime.modules._component import BOOLEAN, TEXT, ComponentModule, manifest


class CleanModule(ComponentModule):
    manifest = manifest(
        "text",
        "clean",
        "NFC normalization, control removal (preserving tabs/newlines), "
        "whitespace collapse and trim.",
        {
            "text": TEXT,
            "trim": BOOLEAN,
            "collapse_whitespace": BOOLEAN,
            "strip_controls": BOOLEAN,
            "normalize_unicode": BOOLEAN,
        },
        ["text"],
        {"text": TEXT},
        {"text": "  Hello   world  "},
    )

    def transform(self, inputs: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        text = inputs["text"]
        if inputs.get("normalize_unicode", True):
            text = unicodedata.normalize("NFC", text)
        if inputs.get("strip_controls", True):
            text = "".join(c for c in text if unicodedata.category(c) != "Cc" or c in "\n\r\t")
        if inputs.get("collapse_whitespace", True):
            # Preserve edge whitespace when trim is explicitly disabled.
            import re

            text = re.sub(r"\s+", " ", text)
        if inputs.get("trim", True):
            text = text.strip()
        return {"text": text}
