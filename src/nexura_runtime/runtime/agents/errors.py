# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Typed exception hierarchy for agent coordination, subagents, and handoffs."""

from __future__ import annotations

from typing import Any

from nexura_agents.errors import NexuraError


class AgentRuntimeError(NexuraError):
    """Base exception for all agent runtime and multi-agent coordination failures."""


class MaxHandoffDepthExceededError(AgentRuntimeError):
    """Raised when recursive handoffs between agents exceed configured depth bounds."""

    def __init__(
        self,
        current_depth: int,
        max_depth: int,
        agent_name: str,
        target_agent: str | None = None,
    ) -> None:
        target_str = f" to '{target_agent}'" if target_agent else ""
        super().__init__(
            f"Agent handoff depth exceeded limit ({current_depth} > {max_depth}) "
            f"initiating from '{agent_name}'{target_str}."
        )
        self.current_depth = current_depth
        self.max_depth = max_depth
        self.agent_name = agent_name
        self.target_agent = target_agent


class HandoffFuelExhaustedError(AgentRuntimeError):
    """Raised when global turn budget (fuel) across coordinated agents is exhausted."""

    def __init__(
        self,
        consumed_fuel: int,
        fuel_budget: int,
        agent_name: str,
    ) -> None:
        super().__init__(
            f"Agent coordination fuel exhausted: consumed {consumed_fuel} >= "
            f"budget {fuel_budget} at agent '{agent_name}'."
        )
        self.consumed_fuel = consumed_fuel
        self.fuel_budget = fuel_budget
        self.agent_name = agent_name


class CapabilityEscalationError(AgentRuntimeError):
    """Raised when a restricted agent attempts to invoke or delegate unauthorized capabilities."""

    def __init__(
        self,
        agent_name: str,
        attempted_tool: str,
        allowed_tools: list[str],
    ) -> None:
        super().__init__(
            f"Capability escalation blocked: agent '{agent_name}' attempted to invoke "
            f"forbidden tool '{attempted_tool}'. Permitted tools: {allowed_tools}."
        )
        self.agent_name = agent_name
        self.attempted_tool = attempted_tool
        self.allowed_tools = allowed_tools


class ParentCancelledError(AgentRuntimeError):
    """Raised when parent execution was cancelled, preventing subsequent child tool runs."""

    def __init__(
        self,
        parent_run_id: str,
        child_agent_name: str,
        reason: str = "Parent execution context was cancelled",
    ) -> None:
        super().__init__(
            f"Child agent '{child_agent_name}' aborted execution: {reason} "
            f"(parent run id: '{parent_run_id}')."
        )
        self.parent_run_id = parent_run_id
        self.child_agent_name = child_agent_name
        self.reason = reason


class ModelRefusalError(AgentRuntimeError):
    """Raised when the underlying model explicitly refuses generation (safety or policy refusal)."""

    def __init__(
        self,
        agent_name: str,
        refusal_reason: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            f"Model generation refused by safety/policy in agent '{agent_name}': {refusal_reason}"
        )
        self.agent_name = agent_name
        self.refusal_reason = refusal_reason
        self.details = details or {}


class ModelLengthExceededError(AgentRuntimeError):
    """Raised when model generation terminates prematurely due to context length or max tokens."""

    def __init__(
        self,
        agent_name: str,
        tokens_used: int | None = None,
    ) -> None:
        msg = f"Model generation exceeded context length or max tokens in agent '{agent_name}'."
        if tokens_used is not None:
            msg += f" Tokens used: {tokens_used}."
        super().__init__(msg)
        self.agent_name = agent_name
        self.tokens_used = tokens_used


class HandoffTargetNotFoundError(AgentRuntimeError):
    """Raised when a handoff target agent is not registered in the coordinator."""

    def __init__(
        self,
        target_name: str,
        available_agents: list[str],
    ) -> None:
        super().__init__(
            f"Handoff target agent '{target_name}' not found. Available agents: {available_agents}."
        )
        self.target_name = target_name
        self.available_agents = available_agents


class AgentApprovalRequiredError(AgentRuntimeError):
    """Raised when an agent-as-tool requires human-in-the-loop approval before running."""

    def __init__(
        self,
        agent_name: str,
        approval_id: str | None = None,
        action_name: str = "agent_call",
    ) -> None:
        super().__init__(
            f"Execution of agent '{agent_name}' requires approval (approval_id: {approval_id})."
        )
        self.agent_name = agent_name
        self.approval_id = approval_id
        self.action_name = action_name
