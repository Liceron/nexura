# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Typed errors for data transformations, JSON querying, merging, and routing."""

from __future__ import annotations

from nexura_agents.errors import NexuraError


class DataTransformError(NexuraError):
    """Base class for data transformation errors."""


class JsonSelectError(DataTransformError):
    """Raised when JSON path selection encounters a missing or invalid path segment."""

    def __init__(self, path: str, message: str | None = None) -> None:
        self.path = path
        msg = message or f"JSON path selection failed for path: '{path}'"
        super().__init__(msg)


class ConflictReducerRequiredError(DataTransformError):
    """Raised when dictionary merge encounters a key conflict without a specified reducer."""

    def __init__(self, key: str, val_a: object, val_b: object) -> None:
        self.key = key
        self.val_a = val_a
        self.val_b = val_b
        super().__init__(
            f"Merge conflict detected on key '{key}' between {type(val_a).__name__} and "
            f"{type(val_b).__name__}. An explicit conflict reducer policy is required."
        )


class ClassificationRoutingError(DataTransformError):
    """Raised when classification routing fails to match any rule and no default is provided."""


class SchemaTransformError(DataTransformError):
    """Raised when schema validation or transformation fails."""
