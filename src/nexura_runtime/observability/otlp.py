# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""OpenTelemetry OTLP trace payload formatting and adapter utilities."""

from __future__ import annotations

from importlib import metadata
from typing import Any

from nexura_runtime.observability.models import SpanRecord


def _scope_version() -> str:
    try:
        return metadata.version("nexura-runtime")
    except metadata.PackageNotFoundError:
        from nexura_runtime import __version__

        return __version__


def span_to_otlp_dict(span: SpanRecord) -> dict[str, Any]:
    """Convert an internal SpanRecord to OpenTelemetry standard OTLP JSON format."""
    start_nano = int(span.start_time * 1_000_000_000)
    end_nano = int((span.end_time or span.start_time) * 1_000_000_000)

    # Status mapping
    if span.status == "ok":
        status_code = "STATUS_CODE_OK"
    elif span.status == "error":
        status_code = "STATUS_CODE_ERROR"
    else:
        status_code = "STATUS_CODE_UNSET"

    # Attributes conversion
    otlp_attributes = [
        {"key": k, "value": {"stringValue": str(v)}} for k, v in span.attributes.items()
    ]

    return {
        "traceId": span.trace_id,
        "spanId": span.span_id,
        "parentSpanId": span.parent_span_id or "",
        "name": span.name,
        "kind": "SPAN_KIND_INTERNAL",
        "startTimeUnixNano": str(start_nano),
        "endTimeUnixNano": str(end_nano),
        "attributes": otlp_attributes,
        "status": {
            "code": status_code,
            "message": span.error or "",
        },
    }


def export_spans_otlp_payload(
    spans: list[SpanRecord], service_name: str = "nexura"
) -> dict[str, Any]:
    """Render a batch of spans into a standard OTLP ResourceSpans dictionary."""
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {
                            "key": "service.name",
                            "value": {"stringValue": service_name},
                        }
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {
                            "name": "nexura-runtime",
                            "version": _scope_version(),
                        },
                        "spans": [span_to_otlp_dict(s) for s in spans],
                    }
                ],
            }
        ]
    }
