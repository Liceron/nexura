# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Tweaks: per-run overrides of node fields, applied to a copy of the imported fields.

Mirrors the V2 refusal rules: code fields, database/query sink fields and secrets are
refused, and every refusal is reported together in one ``TweakRefusedError``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .errors import TweakRefusedError
from .flow import FlowNode, ImportedFlow

#: Field names that can reach a database or execute text; refused for every component.
PROTECTED_FIELDS = frozenset(
    {
        "code",
        "database_url",
        "db_url",
        "connection_string",
        "query",
        "sql",
        "sql_query",
        "python_code",
        "command",
    }
)


@dataclass
class TweakedFields:
    #: node id -> field values after tweaks
    fields: dict[str, dict[str, Any]]
    #: node id -> fields set to a literal (their load_from_db variable reference is dropped)
    literal: dict[str, set[str]]


def _unwrap(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {"value"}:
        return value["value"]
    return value


def apply_tweaks(imported: ImportedFlow, tweaks: Mapping[str, Any] | None) -> TweakedFields:
    """Return the fields of every node with tweaks applied. Raises ``TweakRefusedError``.

    A key matches a node id, else a unique display name. A dict value sets fields of that
    node; any other value is a global override applied to every node that has the field
    (silently ignored where none does).
    """
    result = {nid: dict(n.fields) for nid, n in imported.nodes.items()}
    cleared: dict[str, set[str]] = {nid: set() for nid in imported.nodes}
    if not tweaks:
        return TweakedFields(result, cleared)
    refused: list[tuple[str, str]] = []
    supported = {n.id: n for n in imported.supported_nodes()}

    def check(node: FlowNode, fname: str, label: str) -> bool:
        if fname in PROTECTED_FIELDS or node.field_types.get(fname) == "code":
            refused.append((label, f"field '{fname}' is protected and cannot be tweaked"))
            return False
        if fname in node.secret_fields:
            refused.append(
                (label, f"secret field '{fname}' cannot be tweaked; pass variables= instead")
            )
            return False
        return True

    def set_field(node: FlowNode, fname: str, value: Any) -> None:
        result[node.id][fname] = _unwrap(value)
        cleared[node.id].add(fname)

    for key, value in tweaks.items():
        if isinstance(value, Mapping):
            node = supported.get(key)
            if node is None:
                by_name = [n for n in supported.values() if n.display_name == key]
                if len(by_name) > 1:
                    refused.append((key, "display name matches several nodes; use the node id"))
                    continue
                node = by_name[0] if by_name else None
            if node is None:
                refused.append((key, "no such supported node id or display name"))
                continue
            for fname, fvalue in value.items():
                label = f"{key}.{fname}"
                if fname not in node.fields and fname not in node.field_types:
                    if fname in PROTECTED_FIELDS:
                        refused.append((label, f"field '{fname}' is protected"))
                    else:
                        refused.append((label, "node has no such field"))
                    continue
                if check(node, fname, label):
                    set_field(node, fname, fvalue)
        else:
            if key in PROTECTED_FIELDS:
                refused.append((key, f"global override of protected field '{key}'"))
                continue
            for node in supported.values():
                if (key in node.fields or key in node.field_types) and check(node, key, key):
                    set_field(node, key, value)
    if refused:
        raise TweakRefusedError(refused)
    return TweakedFields(result, cleared)
