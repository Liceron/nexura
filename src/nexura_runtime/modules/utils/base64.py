# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Strict Base64 encoding and UTF-8 text decoding."""

import base64
from typing import Any

from nexura_agents.contracts.context import ExecutionContext

from nexura_runtime.modules._component import BOOLEAN, TEXT, ComponentModule, choice, manifest


class Base64Module(ComponentModule):
    manifest = manifest(
        "utils",
        "base64",
        "Encode UTF-8 text or strictly decode padded Base64 to UTF-8; optional URL-safe alphabet.",
        {"text": TEXT, "operation": choice("encode", "decode"), "url_safe": BOOLEAN},
        ["text", "operation"],
        {"text": TEXT},
        {"text": "hello", "operation": "encode"},
    )

    def transform(self, inputs: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        altchars = b"-_" if inputs.get("url_safe", False) else None
        if inputs["operation"] == "encode":
            text = base64.b64encode(inputs["text"].encode("utf-8"), altchars=altchars).decode(
                "ascii"
            )
        else:
            text = base64.b64decode(
                inputs["text"].encode("ascii"), altchars=altchars, validate=True
            ).decode("utf-8")
        return {"text": text}
