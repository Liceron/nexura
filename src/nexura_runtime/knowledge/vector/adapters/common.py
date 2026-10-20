# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Shared validation and normalization utilities for vector store adapters."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from nexura_runtime.knowledge.vector.errors import (
    TenantIsolationViolationError,
    VectorDimensionMismatchError,
    VectorStoreError,
)
from nexura_runtime.knowledge.vector.models import (
    VectorRecord,
    VectorSearchQuery,
)

SUPPORTED_LOGICAL_OPS = {"$and", "$or"}
SUPPORTED_COMPARISON_OPS = {
    "$eq",
    "$ne",
    "$in",
    "$nin",
    "$gt",
    "$gte",
    "$lt",
    "$lte",
    "$contains",
}


def validate_filter_expression(filter_expr: Any) -> None:
    """Validate structured filter expressions or explicitly reject unsupported forms."""
    if filter_expr is None:
        return
    if not isinstance(filter_expr, dict):
        raise VectorStoreError(
            f"Filter expression must be a dictionary, got {type(filter_expr).__name__}."
        )

    for key, condition in filter_expr.items():
        if key in SUPPORTED_LOGICAL_OPS:
            if not isinstance(condition, list):
                raise VectorStoreError(
                    f"Logical operator '{key}' requires a list of conditions, "
                    f"got {type(condition).__name__}."
                )
            for sub in condition:
                validate_filter_expression(sub)
        elif key.startswith("$"):
            raise VectorStoreError(f"Unsupported filter operator '{key}'.")
        else:
            if isinstance(condition, dict):
                for op, target in condition.items():
                    if op not in SUPPORTED_COMPARISON_OPS:
                        raise VectorStoreError(
                            f"Unsupported filter operator '{op}' for field '{key}'."
                        )
                    if op in ("$in", "$nin") and not isinstance(target, (list, tuple, set)):
                        raise VectorStoreError(
                            f"Operator '{op}' requires an iterable container, "
                            f"got {type(target).__name__}."
                        )


def validate_upsert_records(
    records: Sequence[VectorRecord],
    workspace_id: str,
    collection_id: str,
    target_gen_id: str,
    expected_dimensions: int,
) -> None:
    """Validate records for tenant scoping, collection matching, and vector dimensions."""
    for r in records:
        if r.workspace_id and r.workspace_id != workspace_id:
            raise TenantIsolationViolationError(
                attempted_workspace=r.workspace_id,
                actual_workspace=workspace_id,
            )
        if r.collection_id and r.collection_id != collection_id:
            raise VectorStoreError(
                f"Record collection '{r.collection_id}' does not match "
                f"target collection '{collection_id}'."
            )
        if r.generation_id and r.generation_id != target_gen_id:
            raise VectorStoreError(
                f"Record generation '{r.generation_id}' does not match "
                f"target generation '{target_gen_id}'."
            )
        if len(r.vector) != expected_dimensions:
            raise VectorDimensionMismatchError(
                expected=expected_dimensions,
                actual=len(r.vector),
            )


def sanitize_and_validate_search_query(
    query: VectorSearchQuery,
    workspace_id: str,
) -> dict[str, Any]:
    """Validate query tenant scoping, sanitize external tenant filters, and validate expressions."""
    if query.workspace_id and query.workspace_id != workspace_id:
        raise TenantIsolationViolationError(
            attempted_workspace=query.workspace_id,
            actual_workspace=workspace_id,
        )

    sanitized_filter = dict(query.filter) if query.filter else {}
    if "workspace_id" in sanitized_filter:
        attempted = sanitized_filter.pop("workspace_id")
        if attempted != workspace_id:
            raise TenantIsolationViolationError(
                attempted_workspace=str(attempted),
                actual_workspace=workspace_id,
            )

    validate_filter_expression(sanitized_filter)
    return sanitized_filter
