# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Agent definitions, bounded subagents, recursive handoffs, and multi-agent coordination."""

from __future__ import annotations

from nexura_runtime.modules.registry import default_registry
from nexura_runtime.runtime.agents.coordinator import AgentCoordinator
from nexura_runtime.runtime.agents.definition import (
    AgentDefinition,
    HandoffTarget,
    current_agent_context,
)
from nexura_runtime.runtime.agents.errors import (
    AgentApprovalRequiredError,
    AgentRuntimeError,
    CapabilityEscalationError,
    CoordinatorCallOutcomeUnknownError,
    CoordinatorRunCompletedError,
    HandoffFuelExhaustedError,
    HandoffTargetNotFoundError,
    MaxHandoffDepthExceededError,
    ModelLengthExceededError,
    ModelRefusalError,
    ParentCancelledError,
)
from nexura_runtime.runtime.agents.module import (
    AgentCoordinatorModule,
    create_default_coordinator_module,
)
from nexura_runtime.runtime.agents.saved import (
    AGENT_NODE_MODULE_ID,
    BuiltAgent,
    SavedAgentModule,
    create_saved_agent_module,
)
from nexura_runtime.runtime.agents.types import (
    AgentRunSummary,
    CoordinatorRunResult,
    Handoff,
)

# Register default offline coordinator module in default registry
_default_mod = create_default_coordinator_module()
if not default_registry.has(_default_mod.manifest.module_id):
    default_registry.register(_default_mod)
# The saved-agent node; each service app binds its own copy to its agent store.
_saved_agent_mod = create_saved_agent_module()
if not default_registry.has(_saved_agent_mod.manifest.module_id):
    default_registry.register(_saved_agent_mod)

__all__ = [
    "AgentDefinition",
    "HandoffTarget",
    "AgentCoordinator",
    "AgentCoordinatorModule",
    "create_default_coordinator_module",
    "AGENT_NODE_MODULE_ID",
    "BuiltAgent",
    "SavedAgentModule",
    "create_saved_agent_module",
    "current_agent_context",
    "Handoff",
    "AgentRunSummary",
    "CoordinatorRunResult",
    "AgentRuntimeError",
    "MaxHandoffDepthExceededError",
    "HandoffFuelExhaustedError",
    "CapabilityEscalationError",
    "ParentCancelledError",
    "ModelRefusalError",
    "ModelLengthExceededError",
    "HandoffTargetNotFoundError",
    "AgentApprovalRequiredError",
    "CoordinatorCallOutcomeUnknownError",
    "CoordinatorRunCompletedError",
]
