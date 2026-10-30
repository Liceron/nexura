# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Removed: the ``EffectReconciler`` wrapper (P35 A6 S12).

Effects live in the execution journal (``nexura_agents.execution_store``). Importing a
name from here raises ``RemovedAPIError`` naming the replacement.
"""

from __future__ import annotations

from typing import NoReturn

from nexura_runtime._removed import removed

_REMOVED = frozenset(
    {
        "EffectError",
        "EffectIndeterminateError",
        "EffectReconciler",
        "EffectRecord",
        "EffectStatus",
        "ReconciliationError",
    }
)

__all__: list[str] = []


def __getattr__(name: str) -> NoReturn:
    removed(__name__, name, _REMOVED)
