# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Extract regex matches or named groups with a hard matching budget."""

from typing import Any

from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.errors import SchemaValidationError

from nexura_runtime.modules._component import (
    TEXT,
    ComponentModule,
    array,
    choice,
    compile_regex,
    integer,
    manifest,
)


class RegexExtractModule(ComponentModule):
    manifest = manifest(
        "text",
        "regex_extract",
        "Extract whole matches or named group objects (unmatched groups are null).",
        {
            "text": TEXT,
            "pattern": {"type": "string", "maxLength": 512},
            "mode": choice("all", "named"),
            "max_matches": integer(1, 1024),
        },
        ["text", "pattern"],
        {"matches": array({"type": ["string", "object"]})},
        {"text": "order 42", "pattern": "[0-9]+"},
    )

    def transform(self, inputs: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        pattern = compile_regex(inputs["pattern"])
        named = inputs.get("mode", "all") == "named"
        if named and not pattern.groupindex:
            raise SchemaValidationError("Named mode requires at least one named group.")
        matches: list[Any] = []
        try:
            for match in pattern.finditer(inputs["text"], timeout=0.05):
                if len(matches) >= inputs.get("max_matches", 1024):
                    raise SchemaValidationError("Regex match count exceeds max_matches.")
                matches.append(match.groupdict() if named else match.group(0))
        except TimeoutError as exc:
            raise SchemaValidationError("Regex extraction exceeded 50ms.") from exc
        return {"matches": matches}
