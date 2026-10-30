# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Removed: the legacy effect models (P35 A6 S12); see ``nexura_runtime._removed``."""

from __future__ import annotations

from typing import NoReturn

from nexura_runtime._removed import removed

_REMOVED = frozenset(
    {
        "EffectRecord",
        "EffectStatus",
    }
)

__all__: list[str] = []


def __getattr__(name: str) -> NoReturn:
    removed(__name__, name, _REMOVED)
