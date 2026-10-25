# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Reusable agent definitions, subagent delegation, and capability boundaries."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import math
import uuid
from collections.abc import Callable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from nexura_agents.agent import Agent
from nexura_agents.contracts.canonical import canonical_digest
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.manifest import EffectPolicy
from nexura_agents.errors import SDKValidationError, UnknownToolError
from nexura_agents.models.base import ModelProtocol
from nexura_agents.tools import Tool
from nexura_agents.types import Message
from pydantic import BaseModel

from nexura_runtime.runtime.agents.errors import (
    AgentApprovalRequiredError,
    CapabilityEscalationError,
    ModelLengthExceededError,
    ModelRefusalError,
    ParentCancelledError,
)
from nexura_runtime.runtime.approvals.errors import ApprovalRequiredError
from nexura_runtime.runtime.approvals.gate import ApprovalGate

# Ambient execution context for propagating tracing, cancellation, and policies across subagents
current_agent_context: ContextVar[ExecutionContext | None] = ContextVar(
    "current_agent_context", default=None
)


@dataclass(frozen=True)
class HandoffTarget:
    """Specification of an allowed handoff target agent."""

    target: str | AgentDefinition
    description: str = ""
    allowed_tools: Sequence[str] | None = None
    transfer_history: bool = False

    @property
    def target_name(self) -> str:
        if isinstance(self.target, str):
            return self.target
        return self.target.name


class AgentDefinition:
    """Rich, reusable agent definition with explicit limits, handoffs, and capability bounding."""

    def __init__(
        self,
        name: str,
        *,
        instructions: str | Callable[[ExecutionContext | None], str] = "",
        model: ModelProtocol | Any,
        description: str = "",
        tools: Sequence[Tool] | None = None,
        handoffs: Sequence[HandoffTarget | AgentDefinition] | None = None,
        max_turns: int = 10,
        max_tool_calls: int = 20,
        timeout: float | None = None,
        allowed_tools: Sequence[str] | None = None,
        output_schema: type[BaseModel] | dict[str, Any] | None = None,
        approval_required: bool = False,
        effect_policy: EffectPolicy = "non_idempotent_write",
    ) -> None:
        if not isinstance(name, str) or not name.strip():
            raise SDKValidationError("Agent name must be a non-empty string.")
        self.name = name.strip()

        self.description = description
        self.instructions = instructions

        if model is None or not hasattr(model, "generate"):
            raise SDKValidationError(
                "Agent model must provide a generate() method (ModelProtocol or BaseProvider)."
            )
        self.model = model

        # Validate tools
        tool_seq = tools or []
        seen_tools: set[str] = set()
        validated_tools: list[Tool] = []
        for t in tool_seq:
            if not isinstance(t, Tool):
                raise SDKValidationError(f"Expected Tool instance in tools list, got {type(t)}")
            if t.name in seen_tools:
                raise SDKValidationError(
                    f"Duplicate tool name registered in agent '{self.name}': '{t.name}'"
                )
            seen_tools.add(t.name)
            validated_tools.append(t)
        self.tools: tuple[Tool, ...] = tuple(validated_tools)
        self.tools_by_name: dict[str, Tool] = {t.name: t for t in self.tools}

        # Capability restriction
        self.allowed_tools: tuple[str, ...] | None = (
            tuple(allowed_tools) if allowed_tools is not None else None
        )
        if self.allowed_tools is not None:
            allowed_set = set(self.allowed_tools)
            for t_name in self.tools_by_name:
                if t_name not in allowed_set:
                    raise CapabilityEscalationError(
                        agent_name=self.name,
                        attempted_tool=t_name,
                        allowed_tools=list(self.allowed_tools),
                    )

        # Handoff targets
        handoff_seq = handoffs or []
        validated_handoffs: list[HandoffTarget] = []
        for h in handoff_seq:
            if isinstance(h, HandoffTarget):
                validated_handoffs.append(h)
            elif isinstance(h, AgentDefinition):
                validated_handoffs.append(
                    HandoffTarget(
                        target=h,
                        description=h.description or f"Transfer to agent {h.name}",
                        allowed_tools=h.allowed_tools,
                    )
                )
            else:
                raise SDKValidationError(f"Invalid handoff specification: {h!r}")
        self.handoffs: tuple[HandoffTarget, ...] = tuple(validated_handoffs)

        # Execution limits
        if not isinstance(max_turns, int) or max_turns < 1:
            raise SDKValidationError(f"max_turns must be an integer >= 1, got {max_turns!r}.")
        self.max_turns = max_turns

        if not isinstance(max_tool_calls, int) or max_tool_calls < 0:
            raise SDKValidationError(
                f"max_tool_calls must be an integer >= 0, got {max_tool_calls!r}."
            )
        self.max_tool_calls = max_tool_calls

        if timeout is not None:
            if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
                raise SDKValidationError(
                    f"timeout must be a positive finite number, got {timeout!r}."
                )
            self.timeout: float | None = float(timeout)
        else:
            self.timeout = None

        self.output_schema = output_schema
        self.approval_required = approval_required
        self.effect_policy = effect_policy

    def definition_digest(self) -> str:
        """SHA-256 of what this agent is: the spec digest its approvals are bound to.

        Covers the name, description, instructions (for callable instructions, the
        callable's qualified name), the model's class and model name, every tool's schema,
        the allowed tools, handoff targets, limits, output schema, approval flag and effect
        policy. Editing any of these between an approval request and its dispatch makes the
        approval refuse with ``ApprovalIdentityMismatchError``.
        """

        def _callable_name(fn: Any) -> str:
            return f"{getattr(fn, '__module__', '')}.{getattr(fn, '__qualname__', repr(fn))}"

        def _tool_spec(tool: Tool) -> Any:
            try:
                schema = tool.to_schema()
                canonical_digest(schema)
                return schema
            except (TypeError, ValueError):
                return {"name": tool.name, "description": getattr(tool, "description", "")}

        output_schema: Any
        if isinstance(self.output_schema, type):
            output_schema = _callable_name(self.output_schema)
        else:
            output_schema = self.output_schema
        model = self.model
        spec = {
            "name": self.name,
            "description": self.description,
            "instructions": (
                f"callable:{_callable_name(self.instructions)}"
                if callable(self.instructions)
                else str(self.instructions)
            ),
            "model": {
                "class": _callable_name(type(model)),
                "model": str(getattr(model, "model", getattr(model, "model_name", "")) or ""),
            },
            "tools": [_tool_spec(t) for t in self.tools],
            "allowed_tools": list(self.allowed_tools) if self.allowed_tools is not None else None,
            "handoffs": [
                {
                    "target": h.target_name,
                    "allowed_tools": list(h.allowed_tools) if h.allowed_tools is not None else None,
                    "transfer_history": h.transfer_history,
                }
                for h in self.handoffs
            ],
            "max_turns": self.max_turns,
            "max_tool_calls": self.max_tool_calls,
            "timeout": self.timeout,
            "output_schema": output_schema,
            "approval_required": self.approval_required,
            "effect_policy": self.effect_policy,
        }
        try:
            return canonical_digest(spec)
        except (TypeError, ValueError):
            spec["output_schema"] = repr(output_schema)
            return canonical_digest(spec)

    def get_instructions(self, context: ExecutionContext | None = None) -> str:
        """Resolve instructions text dynamically or statically."""
        if callable(self.instructions):
            try:
                return self.instructions(context)
            except TypeError:
                return self.instructions()  # type: ignore[call-arg]
        return str(self.instructions)

    def as_tool(
        self,
        *,
        name: str | None = None,
        description: str | None = None,
        approval_required: bool | None = None,
        allowed_tools: Sequence[str] | None = None,
        approval_gate: ApprovalGate | None = None,
    ) -> Tool:
        """Expose this agent as an explicitly bounded subagent callable tool.

        Guarantees:
        - Parent cancellation propagates and halts child execution.
        - Cannot bypass approval gating if approval is configured.
        - History isolation: child message turns are kept separate from parent.
        - Capability escalation is strictly prevented.
        """
        tool_name = name or f"delegate_to_{self.name}"
        tool_desc = (
            description
            or self.description
            or f"Delegate inquiry or subtask to specialized subagent '{self.name}'."
        )
        is_approval_required = (
            self.approval_required if approval_required is None else approval_required
        )
        effective_allowed: set[str] | None = None
        if allowed_tools is not None:
            effective_allowed = set(allowed_tools)
        elif self.allowed_tools is not None:
            effective_allowed = set(self.allowed_tools)

        async def _delegate(query: str, context_data: dict[str, Any] | None = None) -> str:
            from nexura_agents import policy

            # P39 G6: this loop calls models without the before_model/after_model hooks and
            # the agent input/output guardrails; under a policy with those stages it is
            # refused here, before any model call, instead of running unchecked.
            policy.check_model_stages(False)
            parent_ctx = current_agent_context.get()

            # 1. Check parent cancellation
            if parent_ctx and parent_ctx.is_cancelled:
                raise ParentCancelledError(
                    parent_run_id=parent_ctx.run_id,
                    child_agent_name=self.name,
                    reason="Parent execution context was cancelled prior to subagent invocation",
                )

            # 2. Check approval gating
            if is_approval_required:
                clean_context = {
                    k: v for k, v in (context_data or {}).items() if k != "approval_id"
                }
                payload = {"query": query, "context_data": clean_context}
                appr_id = (context_data or {}).get("approval_id")
                if appr_id is None and parent_ctx is not None:
                    appr_id = parent_ctx.attributes.get("approval_id")

                if approval_gate is not None and parent_ctx is not None:
                    try:
                        approval_gate.check_or_request_approval(
                            parent_ctx,
                            action_name=tool_name,
                            payload=payload,
                            action_kind="subagent_call",
                            approval_id=appr_id,
                            # Bound to this agent's definition as it is now: editing the
                            # agent after the request invalidates the approval.
                            spec_digest=self.definition_digest(),
                        )
                    except ApprovalRequiredError:
                        raise
                else:
                    raise AgentApprovalRequiredError(self.name, action_name=tool_name)

            # 3. Create inherited child ExecutionContext
            child_run_id = f"subagent_{self.name}_{uuid.uuid4().hex[:8]}"
            child_deadline = parent_ctx.deadline if parent_ctx else None
            if self.timeout is not None:
                calc_deadline = asyncio.get_running_loop().time() + self.timeout
                child_deadline = (
                    min(child_deadline, calc_deadline)
                    if child_deadline is not None
                    else calc_deadline
                )

            child_ctx = ExecutionContext(
                workspace_id=parent_ctx.workspace_id if parent_ctx else "default",
                principal_id=parent_ctx.principal_id if parent_ctx else "anonymous",
                run_id=child_run_id,
                node_id=f"node_{self.name}",
                attempt_id="att_1",
                deadline=child_deadline,
                budget=parent_ctx.budget if parent_ctx else None,
                event_sink=parent_ctx.event_sink if parent_ctx else None,
                cancellation_checker=parent_ctx._cancellation_checker if parent_ctx else None,
                is_cancelled=parent_ctx.is_cancelled if parent_ctx else False,
                attributes={
                    # The parent's approval_id authorised this delegation only; a nested
                    # subagent that needs approval asks for its own.
                    **{
                        k: v
                        for k, v in (parent_ctx.attributes if parent_ctx else {}).items()
                        if k != "approval_id"
                    },
                    "parent_run_id": parent_ctx.run_id if parent_ctx else None,
                    "agent_name": self.name,
                },
            )

            # 4. Execute child agent loop with isolated message history
            from nexura_agents.usage import admission

            budget_token = admission.active_budget.set(admission.effective_budget(child_ctx))
            token = current_agent_context.set(child_ctx)
            try:
                system_text = self.get_instructions(child_ctx)
                child_messages: list[Message] = []
                if system_text:
                    child_messages.append(Message(role="system", content=system_text))

                user_content = query
                if context_data:
                    user_content += f"\nContext: {json.dumps(context_data, ensure_ascii=False)}"
                child_messages.append(Message(role="user", content=user_content))

                turn = 0
                while turn < self.max_turns:
                    # Check parent cancellation before turn
                    if (parent_ctx and parent_ctx.is_cancelled) or child_ctx.is_cancelled:
                        raise ParentCancelledError(
                            parent_run_id=parent_ctx.run_id if parent_ctx else "unknown",
                            child_agent_name=self.name,
                            reason="Execution was cancelled during subagent turn processing",
                        )

                    turn += 1
                    tools_schema = [t.to_schema() for t in self.tools] if self.tools else None

                    # Invoke model
                    response = await admission.generate(
                        self.model,
                        messages=copy.deepcopy(child_messages),
                        tools=tools_schema,
                        context=child_ctx,
                    )

                    # Check model refusal and length bounds
                    if response.finish_reason in ("refusal", "content_filter"):
                        raise ModelRefusalError(
                            agent_name=self.name,
                            refusal_reason=response.message.content
                            or "Model refused generation due to safety/policy constraint",
                        )
                    if response.finish_reason == "length":
                        raise ModelLengthExceededError(
                            agent_name=self.name,
                            tokens_used=response.usage.total_tokens if response.usage else None,
                        )

                    # Duplicate tool-call ids fail closed before any gate, hook or dispatch.
                    from nexura_runtime.runtime.agents.coordinator import (
                        refuse_duplicate_call_ids,
                    )

                    refuse_duplicate_call_ids(response)
                    child_messages.append(response.message)

                    # Final answer if no tool calls
                    if not response.message.tool_calls:
                        final_text = response.message.content or ""
                        if (
                            self.output_schema is not None
                            and isinstance(self.output_schema, type)
                            and issubclass(self.output_schema, BaseModel)
                        ):
                            with contextlib.suppress(Exception):
                                validated = self.output_schema.model_validate_json(final_text)
                                return validated.model_dump_json()
                        return final_text

                    # Execute requested tool calls
                    for tc in response.message.tool_calls:
                        # Check cancellation before dispatching tool call
                        if (parent_ctx and parent_ctx.is_cancelled) or child_ctx.is_cancelled:
                            raise ParentCancelledError(
                                parent_run_id=parent_ctx.run_id if parent_ctx else "unknown",
                                child_agent_name=self.name,
                                reason="Parent cancelled execution before child tool dispatch",
                            )

                        # Enforce capability restrictions
                        if effective_allowed is not None and tc.name not in effective_allowed:
                            raise CapabilityEscalationError(
                                agent_name=self.name,
                                attempted_tool=tc.name,
                                allowed_tools=sorted(list(effective_allowed)),
                            )

                        tool_inst = self.tools_by_name.get(tc.name)
                        if tool_inst is None:
                            raise UnknownToolError(
                                tc.name, permitted_tools=list(self.tools_by_name.keys())
                            )

                        # P39 G2: the tool's guardrails, the inherited and default hooks,
                        # and its approval gates apply here as in a Runner. A subagent
                        # cannot wait for a person inside the delegation: a gated call
                        # fails closed before anything runs.
                        from nexura_agents.run_context import RunContext

                        hooks = policy.effective_hooks()
                        tool_ctx: RunContext[Any] = RunContext(
                            deps=None,
                            run_id=child_run_id,
                            agent_name=self.name,
                            turn=turn,
                            messages=list(child_messages),
                            tool_call_id=tc.id,
                            tool_name=tc.name,
                            tool=tool_inst.info(),
                        )
                        if policy.tool_call_needs_approval(
                            tool_inst, tool_ctx, dict(tc.arguments), hooks
                        ):
                            raise AgentApprovalRequiredError(self.name, action_name=tc.name)
                        from nexura_runtime.runtime.agents.coordinator import (
                            _run_coordinated_tool,
                        )

                        res_str = await _run_coordinated_tool(tool_inst, tool_ctx, tc, hooks)
                        child_messages.append(
                            Message(
                                role="tool",
                                name=tc.name,
                                tool_call_id=tc.id,
                                content=res_str,
                            )
                        )

                return child_messages[-1].content or ""
            finally:
                current_agent_context.reset(token)
                admission.active_budget.reset(budget_token)

        return Tool(
            name=tool_name,
            fn=_delegate,
            description=tool_desc,
            origin=f"agent:{self.name}",
        )

    def to_agent(self) -> Agent:
        """Convert to classic Nexura Agent configuration."""
        return Agent(
            name=self.name,
            instructions=self.get_instructions(),
            model=self.model,
            tools=self.tools,
            max_turns=self.max_turns,
            max_tool_calls=self.max_tool_calls,
            timeout=self.timeout,
        )

    @classmethod
    def from_agent(
        cls,
        agent: Agent,
        *,
        description: str = "",
        handoffs: Sequence[HandoffTarget | AgentDefinition] | None = None,
        allowed_tools: Sequence[str] | None = None,
        approval_required: bool = False,
        output_schema: type[BaseModel] | dict[str, Any] | None = None,
        effect_policy: EffectPolicy = "non_idempotent_write",
    ) -> AgentDefinition:
        """Create AgentDefinition wrapping an existing Agent."""
        if callable(agent.instructions):
            raise ValueError(
                "AgentDefinition.from_agent needs static instructions; dynamic Agent "
                "instructions take a RunContext and run only under Runner."
            )
        return cls(
            name=agent.name,
            instructions=agent.instructions,
            model=agent.model,
            description=description,
            tools=agent.tools,
            handoffs=handoffs,
            max_turns=agent.max_turns,
            max_tool_calls=agent.max_tool_calls,
            timeout=agent.timeout,
            allowed_tools=allowed_tools,
            approval_required=approval_required,
            output_schema=output_schema,
            effect_policy=effect_policy,
        )
