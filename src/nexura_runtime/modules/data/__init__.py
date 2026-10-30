# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Data manipulation, JSON query/merge, reductions, and classification routing."""

from nexura_runtime.modules.data.errors import (
    ClassificationRoutingError,
    ConflictReducerRequiredError,
    DataTransformError,
    JsonSelectError,
    SchemaTransformError,
)
from nexura_runtime.modules.data.module import DataTransformModule
from nexura_runtime.modules.data.transform import (
    classify_route,
    json_merge,
    json_select,
    reduce_data,
)
from nexura_runtime.modules.registry import default_registry

# Register with default module registry
default_registry.register(DataTransformModule())

__all__ = [
    "json_select",
    "json_merge",
    "classify_route",
    "reduce_data",
    "DataTransformModule",
    "DataTransformError",
    "JsonSelectError",
    "ConflictReducerRequiredError",
    "ClassificationRoutingError",
    "SchemaTransformError",
]
