# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Offline dataops builder components and default registrations."""

from nexura_runtime.modules.dataops.csv_build import CsvBuildModule
from nexura_runtime.modules.dataops.csv_parse import CsvParseModule
from nexura_runtime.modules.dataops.dedupe import DedupeModule
from nexura_runtime.modules.dataops.filter import FilterModule
from nexura_runtime.modules.dataops.flatten import FlattenModule
from nexura_runtime.modules.dataops.group_by import GroupByModule
from nexura_runtime.modules.dataops.json_parse import JsonParseModule
from nexura_runtime.modules.dataops.json_stringify import JsonStringifyModule
from nexura_runtime.modules.dataops.pick import PickModule
from nexura_runtime.modules.dataops.set_field import SetFieldModule
from nexura_runtime.modules.dataops.sort import SortModule
from nexura_runtime.modules.registry import default_registry

COMPONENT_CLASSES = (
    JsonParseModule,
    JsonStringifyModule,
    CsvParseModule,
    CsvBuildModule,
    FilterModule,
    SortModule,
    PickModule,
    SetFieldModule,
    FlattenModule,
    DedupeModule,
    GroupByModule,
)

for _component_class in COMPONENT_CLASSES:
    default_registry.register(_component_class())

__all__ = [
    "JsonParseModule",
    "JsonStringifyModule",
    "CsvParseModule",
    "CsvBuildModule",
    "FilterModule",
    "SortModule",
    "PickModule",
    "SetFieldModule",
    "FlattenModule",
    "DedupeModule",
    "GroupByModule",
    "COMPONENT_CLASSES",
]
