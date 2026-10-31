# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Safe native filter projections; the original JSON is authoritative."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def field(key: str, value: Any) -> str | None:
    # Typed hashes prevent path injection, reserved-name collisions and conflicting
    # field mappings. Booleans share numeric equality semantics with Python.
    kind = "s" if isinstance(value, str) else "n" if isinstance(value, (int, float)) else None
    return "m" + hashlib.sha256(key.encode()).hexdigest()[:24] + kind if kind else None


def projections(metadata: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in metadata.items():
        name = field(key, value)
        if name:
            result[name] = float(value) if isinstance(value, (int, float)) else value
    return result


def fully_native(filters: dict[str, Any], dialect: str) -> bool:
    """Whether the compiled expression exactly matches the runtime predicate."""
    for key, value in filters.items():
        if key in ("$and", "$or"):
            if key == "$or" and not value:
                return False
            if not all(fully_native(child, dialect) for child in value):
                return False
            continue
        for op, target in (value if isinstance(value, dict) else {"$eq": value}).items():
            if op == "$contains" or (op in ("$ne", "$nin") and dialect != "elastic"):
                return False
            values = list(target) if op in ("$in", "$nin") else [target]
            if not values or any(field(key, item) is None for item in values):
                return False
            if op in ("$gt", "$gte", "$lt", "$lte") and not isinstance(target, (int, float)):
                return False
    return True


def native_filter(filters: dict[str, Any], dialect: str) -> Any:
    """Compile a necessary condition, never an approximation that loses matches.

    $contains on both strings and lists has no common exact native equivalent.
    Chroma/Pinecone/Milvus/Weaviate negative filters also differ for missing/null
    fields, so these use over-fetch plus matches_filter. Elasticsearch's must_not
    preserves the runtime's missing-field semantics and can be pushed down.
    """

    def join(parts: list[Any], logical: str) -> Any:
        if not parts:
            return None
        if len(parts) == 1:
            return parts[0]
        if dialect in ("chroma", "pinecone"):
            return {"$" + logical: parts}
        if dialect == "weaviate":
            return {"operator": "And" if logical == "and" else "Or", "operands": parts}
        if dialect == "milvus":
            return "(" + (" and " if logical == "and" else " or ").join(parts) + ")"
        return {
            "bool": {
                "must" if logical == "and" else "should": parts,
                **({"minimum_should_match": 1} if logical == "or" else {}),
            }
        }

    def atom(key: str, op: str, value: Any) -> Any:
        name = field(key, value)
        if name is None or op == "$contains":
            return None
        if op in ("$ne", "$nin") and dialect != "elastic":
            return None
        if isinstance(value, bool):
            value = float(value)
        if dialect in ("chroma", "pinecone"):
            return {name: {op: value}}
        if dialect == "milvus":
            operator = {"$eq": "==", "$gt": ">", "$gte": ">=", "$lt": "<", "$lte": "<="}[op]
            return name + " " + operator + " " + json.dumps(value)
        if dialect == "weaviate":
            # Range on tokenized text differs from Python's string ordering.
            if isinstance(value, str) and op != "$eq":
                return None
            operator = {
                "$eq": "Equal",
                "$gt": "GreaterThan",
                "$gte": "GreaterThanEqual",
                "$lt": "LessThan",
                "$lte": "LessThanEqual",
            }[op]
            return {
                "path": [name],
                "operator": operator,
                "valueText" if isinstance(value, str) else "valueNumber": value,
            }
        if op == "$eq":
            return {"term": {name: value}}
        if op == "$ne":
            return {"bool": {"must_not": [{"term": {name: value}}]}}
        return {"range": {name: {op[1:]: value}}}

    parts: list[Any] = []
    for key, value in filters.items():
        if key in ("$and", "$or"):
            children = [native_filter(child, dialect) for child in value]
            # An OR containing a fallback branch has no restrictive necessary condition.
            if key == "$or" and (not children or any(c is None for c in children)):
                continue
            compiled = join([c for c in children if c is not None], key[1:])
            if compiled is not None:
                parts.append(compiled)
            continue
        operations = value if isinstance(value, dict) else {"$eq": value}
        for op, target in operations.items():
            if op in ("$in", "$nin"):
                if op == "$nin" and dialect != "elastic":
                    continue
                children = [atom(key, "$eq", item) for item in target]
                if not children or any(c is None for c in children):
                    continue
                compiled = join(children, "or")
                if op == "$nin":
                    compiled = {"bool": {"must_not": [compiled]}}
            else:
                # String range ordering/collation and null/JSON comparisons differ.
                if op in ("$gt", "$gte", "$lt", "$lte") and not isinstance(target, (int, float)):
                    continue
                compiled = atom(key, op, target)
            if compiled is not None:
                parts.append(compiled)
    return join(parts, "and")
