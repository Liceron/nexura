# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Safe, intentionally limited YAML front matter; no YAML runtime dependency."""

from __future__ import annotations

import json
import re
from typing import Any


def _scalar(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith('"'):
        value, end = json.JSONDecoder().raw_decode(raw)
        if not isinstance(value, str) or (
            raw[end:].strip() and not raw[end:].lstrip().startswith("#")
        ):
            raise ValueError("Expected a quoted string.")
        return value
    if raw.startswith("'"):
        match = re.fullmatch(r"'((?:[^']|'')*)'\s*(?:#.*)?", raw)
        if not match:
            raise ValueError("Unterminated single-quoted string.")
        return match[1].replace("''", "'")
    if raw.startswith(("!", "&", "*", "[", "{", "@", "`", "%")):
        raise ValueError("YAML tags, anchors, aliases and flow collections are unsupported.")
    return re.split(r"\s+#", raw, maxsplit=1)[0].rstrip()


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Strings, folded/literal blocks and a one-level metadata map; reject ambiguity."""
    lines = text.lstrip("\ufeff").splitlines()
    if not lines or lines[0] != "---":
        raise ValueError("Agent Skills require opening YAML '---' front matter.")
    try:
        end = lines.index("---", 1)
    except ValueError:
        raise ValueError("Front matter has no closing '---'.") from None
    front: dict[str, Any] = {}
    index = 1
    while index < end:
        line = lines[index]
        index += 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"([a-z][a-z0-9-]*):\s*(.*)", line)
        if not match or match[1] in front:
            raise ValueError("Invalid or duplicate front matter key.")
        key, raw = match.groups()
        if key == "metadata":
            if raw and not raw.startswith("#"):
                raise ValueError("metadata must be an indented string mapping.")
            values: dict[str, str] = {}
            while index < end and lines[index].startswith("  "):
                item = re.fullmatch(r"  ([A-Za-z0-9_.-]+):\s*(.*)", lines[index])
                if not item or item[1] in values:
                    raise ValueError("Invalid or duplicate metadata entry.")
                values[item[1]] = _scalar(item[2])
                index += 1
            front[key] = values
        elif raw in (">", ">-", ">+", "|", "|-", "|+"):
            block: list[str] = []
            while index < end and (not lines[index].strip() or lines[index].startswith("  ")):
                block.append(lines[index][2:] if lines[index] else "")
                index += 1
            front[key] = (" " if raw.startswith(">") else "\n").join(block).strip()
        else:
            front[key] = _scalar(raw)
    return front, "\n".join(lines[end + 1 :]).strip()
