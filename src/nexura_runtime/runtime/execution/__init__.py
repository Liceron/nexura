# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Removed: the legacy DurableExecutionEngine (P35 A6 S12).

Importing a name from here raises ``RemovedAPIError`` naming the replacement
(``nexura_runtime.workflow.WorkflowEngine`` on the execution journal).
"""

from __future__ import annotations

from typing import NoReturn

from nexura_runtime._removed import removed

_REMOVED = frozenset(
    {
        "DurableDefinitionMismatchError",
        "DurableExecutionEngine",
        "DurableExecutionError",
        "DurableExecutionModule",
        "DurableNodeSpec",
        "DurableRunState",
        "DurableWorkflowSpec",
        "EffectPolicy",
        "FencingConflictError",
        "LeaseExpiredError",
        "NodeIntent",
        "WorkerLease",
    }
)

__all__: list[str] = []


def __getattr__(name: str) -> NoReturn:
    removed(__name__, name, _REMOVED)
