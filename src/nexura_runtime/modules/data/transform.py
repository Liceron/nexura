# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Deterministic, safe data transformations, JSON querying, merging, and routing."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from nexura_runtime.modules.data.errors import (
    ClassificationRoutingError,
    ConflictReducerRequiredError,
    JsonSelectError,
)

_PATH_TOKEN_REGEX = re.compile(r"([a-zA-Z0-9_-]+|\[\d+\]|\[\*\])")


def json_select(
    data: Any,
    path: str,
    *,
    default: Any = None,
    strict: bool = False,
) -> Any:
    """Select nested elements from JSON-like dicts and lists using dot/index paths.

    Supports:
    - Dot navigation: 'order.customer.email'
    - Array indexing: 'items[0].title'
    - Array wildcard mapping: 'items[*].price' -> [100, 200, 300]
    """
    clean_path = path.strip().lstrip(".")
    if not clean_path:
        return data

    tokens = _PATH_TOKEN_REGEX.findall(clean_path)
    if not tokens:
        if strict:
            raise JsonSelectError(path, f"Invalid JSON path syntax: '{path}'")
        return default

    current: Any = data
    for i, token in enumerate(tokens):
        if current is None:
            if strict:
                raise JsonSelectError(path, f"Path segment '{token}' accessed on null/None.")
            return default

        if token == "[*]":
            # Wildcard projection over remaining tokens
            if not isinstance(current, (list, tuple)):
                if strict:
                    raise JsonSelectError(path, "Wildcard '[*]' applied to non-sequence.")
                return default
            remaining_tokens = tokens[i + 1 :]
            if not remaining_tokens:
                return list(current)
            remaining_subpath = ".".join(remaining_tokens)
            results: list[Any] = []
            for item in current:
                res = json_select(item, remaining_subpath, default=default, strict=strict)
                results.append(res)
            return results

        if token.startswith("[") and token.endswith("]"):
            idx = int(token[1:-1])
            if isinstance(current, (list, tuple)):
                if 0 <= idx < len(current):
                    current = current[idx]
                else:
                    if strict:
                        msg = f"Index {idx} out of range (length {len(current)})."
                        raise JsonSelectError(path, msg)
                    return default
            else:
                if strict:
                    raise JsonSelectError(path, f"Cannot index non-sequence with {token}.")
                return default
        else:
            if isinstance(current, dict):
                if token in current:
                    current = current[token]
                else:
                    if strict:
                        raise JsonSelectError(path, f"Key '{token}' not found in dictionary.")
                    return default
            elif hasattr(current, token):
                current = getattr(current, token)
            else:
                if strict:
                    raise JsonSelectError(path, f"Field '{token}' not found on object.")
                return default

    return current


def json_merge(
    base: dict[str, Any],
    override: dict[str, Any],
    *,
    conflict_policy: str = "error",
    custom_reducer: Callable[[str, Any, Any], Any] | None = None,
) -> dict[str, Any]:
    """Deep merge two dictionaries with strict conflict reducer guarantees.

    When non-dictionary keys collide:
    - 'error': Eagerly raises ConflictReducerRequiredError.
    - 'overwrite': override value replaces base value.
    - 'keep': base value is preserved.
    - 'concat': lists are concatenated, scalars become lists.
    - custom_reducer: function (key, val_base, val_override) -> merged_val.
    """
    merged: dict[str, Any] = dict(base)

    for k, override_val in override.items():
        if k not in merged:
            merged[k] = override_val
            continue

        base_val = merged[k]

        # Recursive merge for nested dictionaries
        if isinstance(base_val, dict) and isinstance(override_val, dict):
            merged[k] = json_merge(
                base_val,
                override_val,
                conflict_policy=conflict_policy,
                custom_reducer=custom_reducer,
            )
            continue

        # Key conflict resolution
        if custom_reducer is not None:
            merged[k] = custom_reducer(k, base_val, override_val)
        elif conflict_policy == "error":
            raise ConflictReducerRequiredError(k, base_val, override_val)
        elif conflict_policy == "overwrite":
            merged[k] = override_val
        elif conflict_policy == "keep":
            merged[k] = base_val
        elif conflict_policy == "concat":
            if isinstance(base_val, list) and isinstance(override_val, list):
                merged[k] = base_val + override_val
            elif isinstance(base_val, list):
                merged[k] = base_val + [override_val]
            elif isinstance(override_val, list):
                merged[k] = [base_val] + override_val
            else:
                merged[k] = [base_val, override_val]
        else:
            raise ValueError(f"Unknown merge conflict policy: '{conflict_policy}'")

    return merged


def classify_route(
    data: dict[str, Any],
    rules: list[dict[str, Any]],
    *,
    default_route: str | None = "default",
) -> str:
    """Deterministically route input payload to target branch using ordered rules.

    Each rule structure:
    {
        "field": "order.total",
        "operator": "gte" | "gt" | "lte" | "lt" | "eq" | "ne" | "in" | "contains" | "regex",
        "value": 1000,
        "route": "vip_approval"
    }
    """
    for rule in rules:
        field_path = rule.get("field", "")
        op = rule.get("operator", "eq").lower()
        target_val = rule.get("value")
        route_name = rule.get("route")

        if not route_name:
            continue

        actual_val = json_select(data, field_path) if field_path else data

        matched = _evaluate_rule_condition(actual_val, op, target_val)
        if matched:
            return str(route_name)

    if default_route is not None:
        return default_route

    raise ClassificationRoutingError(
        "No classification rule matched and no default route was configured."
    )


def _evaluate_rule_condition(actual: Any, op: str, target: Any) -> bool:
    """Evaluate comparison operator between actual field value and rule target."""
    if op == "eq":
        return bool(actual == target)
    if op == "ne":
        return bool(actual != target)
    if op == "gt":
        return actual is not None and target is not None and bool(actual > target)
    if op == "gte":
        return actual is not None and target is not None and bool(actual >= target)
    if op == "lt":
        return actual is not None and target is not None and bool(actual < target)
    if op == "lte":
        return actual is not None and target is not None and bool(actual <= target)
    if op == "contains":
        if isinstance(actual, (str, list, tuple, set, dict)) and target is not None:
            return target in actual
        return False
    if op == "in":
        if isinstance(target, (list, tuple, set, dict, str)) and actual is not None:
            return actual in target
        return False
    if op == "regex":
        if isinstance(actual, str) and isinstance(target, str):
            return bool(re.search(target, actual))
        return False
    return False


def reduce_data(
    items: list[Any],
    operation: str,
    *,
    field: str | None = None,
) -> Any:
    """Apply safe deterministic reducers to an array of items."""
    extracted = [json_select(item, field) if field else item for item in items]

    if operation == "count":
        return len(extracted)

    if operation == "sum":
        nums = [x for x in extracted if isinstance(x, (int, float))]
        return sum(nums)

    if operation == "avg":
        nums = [x for x in extracted if isinstance(x, (int, float))]
        return sum(nums) / len(nums) if nums else 0.0

    if operation == "min":
        nums = [x for x in extracted if isinstance(x, (int, float))]
        return min(nums) if nums else None

    if operation == "max":
        nums = [x for x in extracted if isinstance(x, (int, float))]
        return max(nums) if nums else None

    if operation == "dedup":
        seen: set[Any] = set()
        deduped: list[Any] = []
        for x in extracted:
            key = repr(x) if isinstance(x, (dict, list)) else x
            if key not in seen:
                seen.add(key)
                deduped.append(x)
        return deduped

    raise ValueError(f"Unsupported reduction operation: '{operation}'")
