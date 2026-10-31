# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Offline logic builder components and default registrations."""

from nexura_runtime.modules.logic.compare import CompareModule
from nexura_runtime.modules.logic.if_else import IfElseModule
from nexura_runtime.modules.logic.switch import SwitchModule
from nexura_runtime.modules.registry import default_registry

COMPONENT_CLASSES = (
    IfElseModule,
    SwitchModule,
    CompareModule,
)

for _component_class in COMPONENT_CLASSES:
    default_registry.register(_component_class())

__all__ = [
    "IfElseModule",
    "SwitchModule",
    "CompareModule",
    "COMPONENT_CLASSES",
]
