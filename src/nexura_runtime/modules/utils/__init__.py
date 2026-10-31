# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Offline utils builder components and default registrations."""

from nexura_runtime.modules.registry import default_registry
from nexura_runtime.modules.utils.base64 import Base64Module
from nexura_runtime.modules.utils.datetime import DatetimeModule
from nexura_runtime.modules.utils.hash import HashModule
from nexura_runtime.modules.utils.math import MathModule
from nexura_runtime.modules.utils.url import UrlModule
from nexura_runtime.modules.utils.uuid import UuidModule

COMPONENT_CLASSES = (
    MathModule,
    DatetimeModule,
    HashModule,
    Base64Module,
    UrlModule,
    UuidModule,
)

for _component_class in COMPONENT_CLASSES:
    default_registry.register(_component_class())

__all__ = [
    "MathModule",
    "DatetimeModule",
    "HashModule",
    "Base64Module",
    "UrlModule",
    "UuidModule",
    "COMPONENT_CLASSES",
]
