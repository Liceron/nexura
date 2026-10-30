# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
from typing import NoReturn

from nexura_runtime._removed import removed
from nexura_runtime.runtime.agents import (
    AgentCoordinator,
    AgentCoordinatorModule,
    AgentDefinition,
    AgentRuntimeError,
    CapabilityEscalationError,
    HandoffFuelExhaustedError,
    HandoffTarget,
    HandoffTargetNotFoundError,
    MaxHandoffDepthExceededError,
    ModelLengthExceededError,
    ModelRefusalError,
    ParentCancelledError,
)

__all__ = [
    "AgentCoordinator",
    "AgentCoordinatorModule",
    "AgentDefinition",
    "HandoffTarget",
    "AgentRuntimeError",
    "MaxHandoffDepthExceededError",
    "HandoffFuelExhaustedError",
    "CapabilityEscalationError",
    "ParentCancelledError",
    "ModelRefusalError",
    "ModelLengthExceededError",
    "HandoffTargetNotFoundError",
]

# The legacy DurableExecutionEngine names (removed by P35 A6 S12) raise an ImportError
# that names the replacement, for one release.
_REMOVED = frozenset(
    {
        "DurableExecutionEngine",
        "DurableExecutionModule",
        "DurableRunState",
        "DurableWorkflowSpec",
        "DurableNodeSpec",
        "DurableExecutionError",
        "FencingConflictError",
        "LeaseExpiredError",
    }
)


def __getattr__(name: str) -> NoReturn:
    removed(__name__, name, _REMOVED)
