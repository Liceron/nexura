# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Offline URL parsing and percent-encoded query construction."""

from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from nexura_agents.contracts.context import ExecutionContext

from nexura_runtime.modules._component import TEXT, ComponentModule, array, choice, manifest, obj

_QUERY = array(obj({"key": TEXT, "value": TEXT}, ["key", "value"]))
_PARTS = {"scheme": TEXT, "netloc": TEXT, "path": TEXT, "query": _QUERY, "fragment": TEXT}


class UrlModule(ComponentModule):
    manifest = manifest(
        "utils",
        "url",
        "Parse URL parts or build with encoded query pairs, preserving duplicate keys and blanks.",
        {"operation": choice("parse", "build"), "url": TEXT, **_PARTS},
        ["operation"],
        {"url": TEXT, "parts": obj(_PARTS, list(_PARTS))},
        {"operation": "parse", "url": "https://example.test/search?q=hello+world"},
    )

    def transform(self, inputs: dict[str, Any], context: ExecutionContext) -> dict[str, Any]:
        if inputs["operation"] == "build":
            query = urlencode([(p["key"], p["value"]) for p in inputs.get("query", [])])
            url = urlunsplit(
                (
                    inputs.get("scheme", ""),
                    inputs.get("netloc", ""),
                    inputs.get("path", ""),
                    query,
                    inputs.get("fragment", ""),
                )
            )
        else:
            if "url" not in inputs:
                raise ValueError("URL parse requires url.")
            url = inputs["url"]
        parts = urlsplit(url)
        # Force validation of malformed/out-of-range ports.
        _ = parts.port
        query_pairs = parse_qsl(
            parts.query,
            keep_blank_values=True,
            max_num_fields=1024,
            encoding="utf-8",
            errors="strict",
        )
        return {
            "url": url,
            "parts": {
                "scheme": parts.scheme,
                "netloc": parts.netloc,
                "path": parts.path,
                "query": [{"key": k, "value": v} for k, v in query_pairs],
                "fragment": parts.fragment,
            },
        }
