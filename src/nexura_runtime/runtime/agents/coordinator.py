# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Multi-agent coordinator managing recursive handoffs, turn fuel, and isolation."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import json
import uuid
from collections.abc import Sequence
from typing import Any

from nexura_agents.contracts.canonical import canonical_digest
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.errors import (
    EventLoopError,
    MaxTurnsExceededError,
    SDKValidationError,
    UnknownToolError,
)
from nexura_agents.session import SessionStore
from nexura_agents.tools import Tool
from nexura_agents.types import Message, Usage
from pydantic import BaseModel

from nexura_runtime.runtime.agents.definition import (
    AgentDefinition,
    current_agent_context,
)
from nexura_runtime.runtime.agents.errors import (
    AgentApprovalRequiredError,
    CapabilityEscalationError,
    HandoffFuelExhaustedError,
    HandoffTargetNotFoundError,
    MaxHandoffDepthExceededError,
    ModelLengthExceededError,
    ModelRefusalError,
    ParentCancelledError,
)
from nexura_runtime.runtime.agents.types import (
    AgentRunSummary,
    CoordinatorRunResult,
    Handoff,
)
from nexura_runtime.runtime.approvals.gate import ApprovalGate


def _coordinator_approval_id(
    context: ExecutionContext,
    agent_name: str,
    handoffs: int,
    turn: int,
    call_index: int,
    tool_name: str,
    arguments: Any,
) -> str:
    """Stable approval id for one coordinator tool call: run, node, agent, turn and call.

    The call is identified by its position (handoff, turn, index), tool name and argument
    digest, not by the provider's tool-call id: the coordinator re-runs from the start after
    an approval, and real providers mint a new call id on every response. The same call on
    the re-run finds and claims the request; a different call asks for a fresh approval.
    """
    try:
        arguments_digest = canonical_digest(arguments)
    except (TypeError, ValueError):
        arguments_digest = ""  # the gate refuses uncanonical arguments before using the id
    key = json.dumps(
        [
            context.workspace_id,
            context.run_id,
            context.node_id,
            agent_name,
            handoffs,
            turn,
            call_index,
            tool_name,
            arguments_digest,
        ],
        ensure_ascii=False,
    )
    return f"appr_coord_{hashlib.sha256(key.encode('utf-8')).hexdigest()[:24]}"


class AgentCoordinator:
    """Orchestrates multi-agent execution, recursive handoffs, turn fuel, and capability limits."""

    def __init__(
        self,
        agents: Sequence[AgentDefinition],
        *,
        max_handoff_depth: int = 5,
        total_fuel: int = 25,
        approval_gate: ApprovalGate | None = None,
        session_store: SessionStore | None = None,
    ) -> None:
        if not agents:
            raise SDKValidationError("AgentCoordinator requires at least one AgentDefinition.")
        if max_handoff_depth < 0:
            raise SDKValidationError(f"max_handoff_depth must be >= 0, got {max_handoff_depth}.")
        if total_fuel < 1:
            raise SDKValidationError(f"total_fuel must be >= 1, got {total_fuel}.")

        self.max_handoff_depth = max_handoff_depth
        self.total_fuel = total_fuel
        self.approval_gate = approval_gate
        self.session_store = session_store

        seen_names: set[str] = set()
        agent_map: dict[str, AgentDefinition] = {}
        for a in agents:
            if not isinstance(a, AgentDefinition):
                raise SDKValidationError(f"Expected AgentDefinition instance, got {type(a)}")
            if a.name in seen_names:
                raise SDKValidationError(f"Duplicate agent registered in coordinator: '{a.name}'")
            seen_names.add(a.name)
            agent_map[a.name] = a

        self.agents: dict[str, AgentDefinition] = agent_map

    def run(
        self,
        initial_agent_name: str,
        prompt: str | list[Message],
        *,
        context: ExecutionContext | None = None,
        session_id: str | None = None,
        shared_memory: dict[str, Any] | None = None,
    ) -> CoordinatorRunResult:
        """Synchronously execute coordinated multi-agent workflow."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None and loop.is_running():
            raise EventLoopError(
                "Cannot call coordinator.run() synchronously from an active asyncio event loop. "
                "Use 'await coordinator.run_async(...)' instead."
            )

        return asyncio.run(
            self.run_async(
                initial_agent_name,
                prompt,
                context=context,
                session_id=session_id,
                shared_memory=shared_memory,
            )
        )

    async def run_async(
        self,
        initial_agent_name: str,
        prompt: str | list[Message],
        *,
        context: ExecutionContext | None = None,
        session_id: str | None = None,
        shared_memory: dict[str, Any] | None = None,
    ) -> CoordinatorRunResult:
        """Asynchronously execute coordinated multi-agent workflow."""
        if initial_agent_name not in self.agents:
            raise HandoffTargetNotFoundError(initial_agent_name, list(self.agents.keys()))

        root_run_id = context.run_id if context else f"coord_run_{uuid.uuid4().hex[:10]}"
        effective_context = context or ExecutionContext(
            run_id=root_run_id,
            node_id="coordinator_root",
        )

        # Check cancellation at entry
        if effective_context.is_cancelled:
            raise ParentCancelledError(
                parent_run_id=effective_context.run_id,
                child_agent_name=initial_agent_name,
                reason="Parent execution context was cancelled before coordinator run started",
            )

        shared_mem: dict[str, Any] = dict(shared_memory or {})
        effective_context.attributes["shared_memory"] = shared_mem
        current_agent_name = initial_agent_name
        current_depth = 0
        remaining_fuel = self.total_fuel
        traces: list[AgentRunSummary] = []
        total_usage = Usage()
        handoffs_count = 0
        current_prompt: str | list[Message] = prompt

        from nexura_agents.usage import admission

        budget_token = admission.active_budget.set(admission.effective_budget(effective_context))
        token = current_agent_context.set(effective_context)
        try:
            while True:
                # 1. Retrieve current agent
                agent = self.agents.get(current_agent_name)
                if agent is None:
                    raise HandoffTargetNotFoundError(current_agent_name, list(self.agents.keys()))

                # 2. Check depth limits
                if current_depth > self.max_handoff_depth:
                    raise MaxHandoffDepthExceededError(
                        current_depth=current_depth,
                        max_depth=self.max_handoff_depth,
                        agent_name=current_agent_name,
                    )

                # 3. Check cancellation
                if effective_context.is_cancelled:
                    raise ParentCancelledError(
                        parent_run_id=effective_context.run_id,
                        child_agent_name=current_agent_name,
                    )

                # 4. Construct tools including dynamic typed handoff tools
                run_tools: dict[str, Tool] = dict(agent.tools_by_name)
                for h_target in agent.handoffs:
                    target_name = h_target.target_name

                    def _build_handoff_fn(t_name: str) -> Any:
                        async def _handoff_call(
                            reason: str, context: dict[str, Any] | None = None
                        ) -> dict[str, Any]:
                            return {
                                "__nexura_handoff__": True,
                                "target_agent": t_name,
                                "reason": reason,
                                "context": context or {},
                            }

                        return _handoff_call

                    h_tool = Tool(
                        name=f"transfer_to_{target_name}",
                        fn=_build_handoff_fn(target_name),
                        description=(
                            h_target.description
                            or f"Transfer execution and control to agent '{target_name}'."
                        ),
                    )
                    run_tools[h_tool.name] = h_tool

                # 5. Build isolated message history for current agent
                inst = agent.get_instructions(effective_context)
                agent_messages: list[Message] = []
                if inst:
                    agent_messages.append(Message(role="system", content=inst))

                if isinstance(current_prompt, str):
                    agent_messages.append(Message(role="user", content=current_prompt))
                else:
                    agent_messages.extend(copy.deepcopy(current_prompt))

                # 6. Execute turns for this agent
                agent_turns = 0
                agent_tool_calls = 0
                handoff_occurred: Handoff | None = None
                agent_run_id = f"run_{agent.name}_{uuid.uuid4().hex[:8]}"

                while agent_turns < agent.max_turns:
                    # Enforce global turn fuel
                    if remaining_fuel <= 0:
                        raise HandoffFuelExhaustedError(
                            consumed_fuel=self.total_fuel - remaining_fuel,
                            fuel_budget=self.total_fuel,
                            agent_name=agent.name,
                        )

                    # Enforce cancellation before turn
                    if effective_context.is_cancelled:
                        raise ParentCancelledError(
                            parent_run_id=effective_context.run_id,
                            child_agent_name=agent.name,
                            reason="Cancellation received before turn dispatch",
                        )

                    remaining_fuel -= 1
                    agent_turns += 1

                    tools_schema = (
                        [t.to_schema() for t in run_tools.values()] if run_tools else None
                    )

                    # Invoke model
                    response = await admission.generate(
                        agent.model,
                        messages=copy.deepcopy(agent_messages),
                        tools=tools_schema,
                        context=effective_context,
                    )

                    # Check model refusal and length bounds
                    if response.finish_reason in ("refusal", "content_filter"):
                        raise ModelRefusalError(
                            agent_name=agent.name,
                            refusal_reason=response.message.content
                            or "Model refused generation due to safety/content policy",
                        )
                    if response.finish_reason == "length":
                        raise ModelLengthExceededError(
                            agent_name=agent.name,
                            tokens_used=response.usage.total_tokens if response.usage else None,
                        )

                    if response.usage:
                        total_usage = total_usage.add(response.usage)

                    agent_messages.append(response.message)

                    # Check for final output (no tool calls)
                    if not response.message.tool_calls:
                        final_text = response.message.content or ""
                        structured = None
                        if (
                            agent.output_schema is not None
                            and isinstance(agent.output_schema, type)
                            and issubclass(agent.output_schema, BaseModel)
                        ):
                            with contextlib.suppress(Exception):
                                structured = agent.output_schema.model_validate_json(final_text)

                        traces.append(
                            AgentRunSummary(
                                agent_name=agent.name,
                                run_id=agent_run_id,
                                parent_run_id=root_run_id
                                if agent.name != initial_agent_name
                                else None,
                                turns=agent_turns,
                                tool_calls_count=agent_tool_calls,
                                handoff_to=None,
                                usage=response.usage or Usage(),
                            )
                        )

                        return CoordinatorRunResult(
                            root_run_id=root_run_id,
                            final_agent_name=agent.name,
                            final_output=final_text,
                            structured_output=structured,
                            messages=agent_messages,
                            traces=traces,
                            total_fuel_consumed=self.total_fuel - remaining_fuel,
                            handoffs_count=handoffs_count,
                            total_usage=total_usage,
                            status="completed",
                        )

                    # Execute requested tool calls
                    for call_index, tc in enumerate(response.message.tool_calls):
                        # Enforce cancellation before tool execution
                        if effective_context.is_cancelled:
                            raise ParentCancelledError(
                                parent_run_id=effective_context.run_id,
                                child_agent_name=agent.name,
                                reason="Cancellation received before tool execution",
                            )

                        # Check capability escalation
                        if agent.allowed_tools is not None:
                            is_handoff_tool = any(
                                tc.name == f"transfer_to_{h.target_name}" for h in agent.handoffs
                            )
                            if not is_handoff_tool and tc.name not in agent.allowed_tools:
                                raise CapabilityEscalationError(
                                    agent_name=agent.name,
                                    attempted_tool=tc.name,
                                    allowed_tools=list(agent.allowed_tools),
                                )

                        target_tool = run_tools.get(tc.name)
                        if target_tool is None:
                            raise UnknownToolError(tc.name, permitted_tools=list(run_tools.keys()))

                        # Approval gating verification
                        if agent.approval_required:
                            if self.approval_gate is not None:
                                # A stable id for this exact call, so re-running the same
                                # run after the approval finds and claims it (like the
                                # runner's resume). Bound to the agent's definition too.
                                self.approval_gate.check_or_request_approval(
                                    effective_context,
                                    action_name=tc.name,
                                    payload=tc.arguments,
                                    action_kind="tool_call",
                                    request_id=_coordinator_approval_id(
                                        effective_context,
                                        agent.name,
                                        handoffs_count,
                                        agent_turns,
                                        call_index,
                                        tc.name,
                                        tc.arguments,
                                    ),
                                    spec_digest=agent.definition_digest(),
                                )
                            else:
                                raise AgentApprovalRequiredError(agent.name, action_name=tc.name)

                        # Execute tool
                        tool_res = await target_tool.arun(**tc.arguments)
                        agent_tool_calls += 1

                        # Intercept handoff call
                        if isinstance(tool_res, dict) and tool_res.get("__nexura_handoff__"):
                            handoff_occurred = Handoff(
                                target_agent=tool_res["target_agent"],
                                reason=tool_res.get("reason", ""),
                                context=tool_res.get("context", {}),
                            )
                            agent_messages.append(
                                Message(
                                    role="tool",
                                    name=tc.name,
                                    tool_call_id=tc.id,
                                    content=f"Handoff initiated to {handoff_occurred.target_agent}",
                                )
                            )
                            break
                        else:
                            res_str = (
                                tool_res
                                if isinstance(tool_res, str)
                                else json.dumps(tool_res, ensure_ascii=False)
                            )
                            agent_messages.append(
                                Message(
                                    role="tool",
                                    name=tc.name,
                                    tool_call_id=tc.id,
                                    content=res_str,
                                )
                            )

                    if handoff_occurred:
                        break

                if handoff_occurred:
                    handoffs_count += 1
                    current_depth += 1

                    # Check depth limit immediately on transition
                    if current_depth > self.max_handoff_depth:
                        raise MaxHandoffDepthExceededError(
                            current_depth=current_depth,
                            max_depth=self.max_handoff_depth,
                            agent_name=agent.name,
                            target_agent=handoff_occurred.target_agent,
                        )

                    # Ensure target exists
                    target_def = self.agents.get(handoff_occurred.target_agent)
                    if target_def is None:
                        raise HandoffTargetNotFoundError(
                            handoff_occurred.target_agent, list(self.agents.keys())
                        )

                    # Record trace summary
                    traces.append(
                        AgentRunSummary(
                            agent_name=agent.name,
                            run_id=agent_run_id,
                            parent_run_id=root_run_id if agent.name != initial_agent_name else None,
                            turns=agent_turns,
                            tool_calls_count=agent_tool_calls,
                            handoff_to=handoff_occurred.target_agent,
                            handoff_reason=handoff_occurred.reason,
                            usage=Usage(),
                        )
                    )

                    # Prepare context and prompt for target agent
                    current_agent_name = handoff_occurred.target_agent
                    current_prompt = (
                        f"[Handoff from agent '{agent.name}']\n"
                        f"Reason: {handoff_occurred.reason}\n"
                        f"Context: {json.dumps(handoff_occurred.context, ensure_ascii=False)}"
                    )
                    continue
                else:
                    raise MaxTurnsExceededError(agent_turns, agent.max_turns)
        finally:
            current_agent_context.reset(token)
            admission.active_budget.reset(budget_token)
