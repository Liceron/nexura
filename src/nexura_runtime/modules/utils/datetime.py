# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Replayable ISO date/time operations with explicit clock input."""

import re
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.errors import SchemaValidationError

from nexura_runtime.modules._component import TEXT, ComponentModule, choice, manifest, obj

_DURATION = obj(
    {
        key: {"type": "number", "minimum": -36600, "maximum": 36600}
        for key in ("days", "hours", "minutes", "seconds")
    },
    [],
)


class DatetimeModule(ComponentModule):
    manifest = manifest(
        "utils",
        "datetime",
        "Parse/format ISO dates, apply durations and convert zones; "
        "now requires explicit or context ISO time.",
        {
            "operation": choice("parse", "format", "add", "subtract", "timezone", "now"),
            "text": {"type": "string", "maxLength": 128},
            "format": {"type": "string", "maxLength": 256},
            "timezone": {"type": "string", "maxLength": 128},
            "duration": _DURATION,
        },
        ["operation"],
        {"text": TEXT, "iso": TEXT},
        {"operation": "add", "text": "2026-01-01T00:00:00Z", "duration": {"days": 1}},
    )

    def transform(self, inputs: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        operation = inputs["operation"]
        raw = inputs.get("text")
        if operation == "now" and raw is None:
            raw = context.attributes.get("now")
        if not isinstance(raw, str) or len(raw) > 128:
            raise SchemaValidationError(
                "Datetime requires ISO text; now accepts context.attributes['now']."
            )
        try:
            value = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise SchemaValidationError("Invalid ISO-8601 datetime.") from exc
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        if operation in {"add", "subtract"}:
            if "duration" not in inputs:
                raise SchemaValidationError("Add/subtract requires duration.")
            delta = timedelta(**inputs["duration"])
            value = value + delta if operation == "add" else value - delta
        if operation == "timezone" and "timezone" not in inputs:
            raise SchemaValidationError("Timezone operation requires timezone.")
        if "timezone" in inputs:
            zone = inputs["timezone"]
            if zone.startswith(("/", ".")) or ".." in zone.split("/"):
                raise SchemaValidationError("Timezone must be an IANA zone name.")
            value = value.astimezone(ZoneInfo(zone))
        text = value.isoformat()
        if operation == "format":
            fmt = inputs.get("format", "%Y-%m-%dT%H:%M:%S%z")
            # Locale-independent directives only; no modifiers or platform-specific extensions.
            if re.search(r"%(?![YmdHMSfzjwuVG%])", fmt.replace("%%", "")):
                raise SchemaValidationError("Unsupported strftime directive.")
            text = value.strftime(fmt)
        return {"text": text, "iso": value.isoformat()}
