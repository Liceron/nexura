# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Literal and timeout-protected regular expression replacement."""

import re
from typing import Any

from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.errors import SchemaValidationError

from nexura_runtime.modules._component import (
    TEXT,
    ComponentModule,
    choice,
    compile_regex,
    integer,
    manifest,
)

_REFERENCE = re.compile(r"\\(?:g<([^>]+)>|([1-9][0-9]*)|([nrt\\]))")


def _expand(match: Any, replacement: str) -> str:
    """Expand numeric/named references incrementally, bounding even repeated large groups."""
    pieces = []
    size = 0
    index = 0
    while index < len(replacement):
        if replacement[index] != "\\":
            part = replacement[index]
            index += 1
        else:
            token = _REFERENCE.match(replacement, index)
            if token is None:
                raise SchemaValidationError("Invalid replacement escape or group reference.")
            named, numeric, escape = token.groups()
            if named is not None or numeric is not None:
                group = named if named is not None else numeric
                try:
                    part = match.group(int(group) if group.isdigit() else group) or ""
                except (IndexError, KeyError) as exc:
                    raise SchemaValidationError("Unknown replacement group.") from exc
            else:
                part = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\"}[escape]
            index = token.end()
        size += len(part)
        if size > 65536:
            raise SchemaValidationError("Replacement expansion exceeds 65536 characters.")
        pieces.append(part)
    return "".join(pieces)


class ReplaceModule(ComponentModule):
    manifest = manifest(
        "text",
        "replace",
        "Replace at most max_replacements occurrences; regex replacements support groups.",
        {
            "text": TEXT,
            "pattern": TEXT,
            "replacement": TEXT,
            "mode": choice("literal", "regex"),
            "max_replacements": integer(1, 1024),
        },
        ["text", "pattern", "replacement"],
        {"text": TEXT},
        {"text": "hello world", "pattern": "world", "replacement": "Nexura"},
    )

    def transform(self, inputs: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        count = inputs.get("max_replacements", 1024)
        source, replacement = inputs["text"], inputs["replacement"]
        if inputs.get("mode", "literal") == "literal":
            occurrences = min(count, source.count(inputs["pattern"]))
            length = len(source) + occurrences * (len(replacement) - len(inputs["pattern"]))
            if length > 65536:
                raise SchemaValidationError("Replacement output exceeds 65536 characters.")
            text = source.replace(inputs["pattern"], replacement, count)
        else:
            pieces = []
            size = 0
            end = 0
            try:
                for index, match in enumerate(
                    compile_regex(inputs["pattern"]).finditer(source, timeout=0.05)
                ):
                    if index >= count:
                        break
                    chunk = source[end : match.start()] + _expand(match, replacement)
                    size += len(chunk)
                    if size > 65536:
                        raise SchemaValidationError("Replacement output exceeds 65536 characters.")
                    pieces.append(chunk)
                    end = match.end()
            except TimeoutError as exc:
                raise SchemaValidationError("Regex replacement exceeded 50ms.") from exc
            if size + len(source) - end > 65536:
                raise SchemaValidationError("Replacement output exceeds 65536 characters.")
            text = "".join(pieces) + source[end:]
        return {"text": text}
