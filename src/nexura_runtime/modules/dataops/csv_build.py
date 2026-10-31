# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Build CSV with deterministic column order."""

import csv
import io
from typing import Any

from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.errors import SchemaValidationError

from nexura_runtime.modules._component import OBJECT, TEXT, ComponentModule, array, manifest
from nexura_runtime.modules.dataops.csv_parse import DELIMITER, check_delimiter


class CsvBuildModule(ComponentModule):
    manifest = manifest(
        "data",
        "csv_build",
        "Build CSV with supplied columns or sorted union of row keys; missing cells are empty.",
        {"items": array(OBJECT), "delimiter": DELIMITER, "fields": array(TEXT)},
        ["items"],
        {"text": TEXT},
        {"items": [{"name": "Ada", "score": 10}]},
    )

    def transform(self, inputs: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        delimiter = inputs.get("delimiter", ",")
        check_delimiter(delimiter)
        items = inputs["items"]
        fields = inputs.get("fields", sorted({k for row in items for k in row}))
        if len(set(fields)) != len(fields) or any(not f for f in fields) or len(fields) > 1024:
            raise SchemaValidationError("CSV fields must be unique, nonempty and at most 1024.")
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(
            stream,
            fieldnames=fields,
            delimiter=delimiter,
            lineterminator="\n",
            extrasaction="ignore",
        )
        if fields:
            writer.writeheader()
        for row in items:
            if any(isinstance(row.get(k), (dict, list)) for k in fields):
                raise SchemaValidationError("CSV cells must be scalar JSON values.")
            writer.writerow(row)
            if stream.tell() > 65536:
                raise SchemaValidationError("CSV output exceeds 65536 characters.")
        return {"text": stream.getvalue()}
