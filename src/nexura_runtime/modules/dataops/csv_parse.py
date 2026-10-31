# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Parse bounded CSV with an explicit header contract."""

import csv
import io
from typing import Any

from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.errors import SchemaValidationError

from nexura_runtime.modules._component import TEXT, ComponentModule, array, integer, manifest

DELIMITER = {"type": "string", "minLength": 1, "maxLength": 1}


def check_delimiter(delimiter: str) -> None:
    if delimiter in '\r\n\x00"':
        raise SchemaValidationError("CSV delimiter cannot be newline, NUL or quote.")


class CsvParseModule(ComponentModule):
    manifest = manifest(
        "data",
        "csv_parse",
        "Parse a unique nonempty header and equal-width string rows; reject overflow.",
        {"text": TEXT, "delimiter": DELIMITER, "max_rows": integer(1, 1024)},
        ["text"],
        {"items": array({"type": "object", "additionalProperties": {"type": "string"}})},
        {"text": "name,score\nAda,10\n"},
    )

    def transform(self, inputs: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        delimiter = inputs.get("delimiter", ",")
        check_delimiter(delimiter)
        try:
            reader = csv.reader(
                io.StringIO(inputs["text"], newline=""), delimiter=delimiter, strict=True
            )
            header = next(reader, [])
            if not header or any(not h for h in header) or len(set(header)) != len(header):
                raise SchemaValidationError("CSV requires unique, nonempty header names.")
            items: list[dict[str, str]] = []
            for row in reader:
                if len(items) >= inputs.get("max_rows", 1024):
                    raise SchemaValidationError("CSV exceeds max_rows.")
                if len(row) != len(header):
                    raise SchemaValidationError("CSV row width does not match header.")
                items.append(dict(zip(header, row, strict=True)))
        except csv.Error as exc:
            raise SchemaValidationError(f"Invalid CSV: {exc}") from exc
        return {"items": items}
