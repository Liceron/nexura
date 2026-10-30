# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Explicit JSON projections, bounded table row selection, and log compaction."""

from __future__ import annotations

import json
from typing import Any

from nexura_agents.context.errors import MalformedStructuredDataError

_PROTECTED_KEY_PATTERNS = {
    "id",
    "uuid",
    "run_id",
    "node_id",
    "tool_call_id",
    "approval_id",
    "effect_id",
    "amount",
    "amount_usd",
    "price",
    "cost",
    "total",
    "balance",
    "recipient",
    "sender",
    "status",
    "error",
}


def _is_protected_key(key: str, custom_protected: set[str] | None = None) -> bool:
    k_lower = key.lower()
    if custom_protected and key in custom_protected:
        return True
    if k_lower in _PROTECTED_KEY_PATTERNS:
        return True
    return any(k_lower.endswith(sfx) for sfx in ("_id", "_amount", "_usd", "_total"))


def project_json(
    payload: Any,
    projection_spec: list[str] | None = None,
    protected_keys: set[str] | None = None,
    *,
    allow_malformed: bool = True,
) -> Any:
    """Project structured JSON fields while preserving all protected keys.

    If projection_spec is None, all fields are preserved.
    Malformed structured data is preserved verbatim without crashing when allow_malformed=True.
    """
    if projection_spec is None:
        return payload

    allowed_set = set(projection_spec)

    if isinstance(payload, str):
        payload_stripped = payload.strip()
        if payload_stripped.startswith(("{", "[")):
            try:
                parsed = json.loads(payload_stripped)
                projected = project_json(
                    parsed,
                    projection_spec=projection_spec,
                    protected_keys=protected_keys,
                    allow_malformed=allow_malformed,
                )
                return json.dumps(projected)
            except json.JSONDecodeError as exc:
                if not allow_malformed:
                    raise MalformedStructuredDataError(
                        f"Malformed JSON payload cannot be projected: {exc}"
                    ) from exc
                return payload  # Preserve verbatim on malformed input
        return payload

    if isinstance(payload, dict):
        result: dict[str, Any] = {}
        for k, v in payload.items():
            if str(k) in allowed_set or _is_protected_key(str(k), protected_keys):
                result[str(k)] = v
        return result

    if isinstance(payload, list):
        return [
            project_json(
                item,
                projection_spec=projection_spec,
                protected_keys=protected_keys,
                allow_malformed=allow_malformed,
            )
            for item in payload
        ]

    return payload


def compact_table(content: str | list[dict[str, Any]], max_rows: int = 50) -> tuple[str, int, int]:
    """Bound table rows with an explicit provenance and summary marker.

    Returns (compacted_text, included_row_count, omitted_row_count).
    """
    if isinstance(content, list):
        total = len(content)
        if total <= max_rows:
            return json.dumps(content, indent=2), total, 0
        kept = content[:max_rows]
        omitted = total - max_rows
        marker = {
            "_table_summary": (
                f"[Table compacted: {max_rows} of {total} items included; "
                f"{omitted} omitted by policy]"
            )
        }
        return json.dumps([*kept, marker], indent=2), max_rows, omitted

    text = str(content)
    lines = text.splitlines()
    if len(lines) <= 2:
        return text, len(lines), 0

    # Detect markdown table or CSV/TSV
    is_markdown = any("|" in line for line in lines[:3])
    if is_markdown:
        # Separate header + separator from rows
        header_lines: list[str] = []
        data_lines: list[str] = []
        for idx, line in enumerate(lines):
            if idx < 2 and ("|" in line):
                header_lines.append(line)
            else:
                data_lines.append(line)

        total_data = len(data_lines)
        if total_data <= max_rows:
            return text, total_data, 0

        kept_data = data_lines[:max_rows]
        omitted = total_data - max_rows
        summary_marker = (
            f"[Table compacted: {max_rows} of {total_data} rows included; "
            f"{omitted} omitted by policy]"
        )
        compacted = "\n".join([*header_lines, *kept_data, summary_marker])
        return compacted, max_rows, omitted

    # Fallback line-based table compaction
    header = lines[0]
    data = lines[1:]
    total_data = len(data)
    if total_data <= max_rows:
        return text, total_data, 0

    kept_data = data[:max_rows]
    omitted = total_data - max_rows
    summary_marker = (
        f"[Table compacted: {max_rows} of {total_data} rows included; {omitted} omitted by policy]"
    )
    compacted = "\n".join([header, *kept_data, summary_marker])
    return compacted, max_rows, omitted


def bound_logs(log_text: str, max_lines: int = 100) -> tuple[str, int, int]:
    """Bound log lines to the most recent window with an explicit summary marker.

    Returns (bounded_text, included_line_count, omitted_line_count).
    """
    lines = log_text.splitlines()
    total = len(lines)
    if total <= max_lines:
        return log_text, total, 0

    kept = lines[-max_lines:]
    omitted = total - max_lines
    marker = (
        f"[Logs bounded: latest {max_lines} lines preserved; "
        f"{omitted} earlier lines omitted by policy]"
    )
    bounded = f"{marker}\n" + "\n".join(kept)
    return bounded, max_lines, omitted
