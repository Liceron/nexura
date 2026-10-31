# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Strict JSON text parsing."""

import json
from typing import Any

from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.errors import SchemaValidationError

from nexura_runtime.modules._component import TEXT, ComponentModule, manifest


def _invalid_constant(value: str) -> None:
    raise SchemaValidationError(f"Invalid JSON constant: {value}")


class JsonParseModule(ComponentModule):
    manifest = manifest(
        "data",
        "json_parse",
        "Parse strict JSON text to a JSON value; reject NaN and Infinity.",
        {"text": TEXT},
        ["text"],
        {"value": {}},
        {"text": '{"answer": 42}'},
    )

    def transform(self, inputs: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        try:
            value = json.loads(inputs["text"], parse_constant=_invalid_constant)
        except json.JSONDecodeError as exc:
            raise SchemaValidationError(
                f"Invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
            ) from exc
        except RecursionError as exc:
            raise SchemaValidationError("JSON nesting exceeds parser limits.") from exc
        return {"value": value}
