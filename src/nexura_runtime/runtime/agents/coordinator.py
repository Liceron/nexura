# SPDX-License-Identifier: Apache-2.0
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

from nexura_agents import policy
from nexura_agents.contracts.canonical import canonical_digest
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.errors import (
    EventLoopError,
    MaxTurnsExceededError,
    ProviderResponseError,
    SDKValidationError,
    UnknownToolError,
)
from nexura_agents.hooks import AgentHooks
from nexura_agents.run_context import RunContext
from nexura_agents.session import SessionStore
from nexura_agents.tools import Tool
from nexura_agents.types import Message, ModelResponse, ToolCall, Usage
from pydantic import BaseModel

from nexura_runtime.runtime.agents.call_ledger import (
    CoordinatorCallLedger,
    Entry,
    MemoryCallLedger,
)
from nexura_runtime.runtime.agents.definition import (
    AgentDefinition,
    current_agent_context,
)
from nexura_runtime.runtime.agents.errors import (
    AgentApprovalRequiredError,
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
from nexura_runtime.runtime.agents.types import (
    AgentRunSummary,
    CoordinatorRunResult,
    Handoff,
)
from nexura_runtime.runtime.approvals.errors import ApprovalClaimConflictError
from nexura_runtime.runtime.approvals.gate import ApprovalGate


def refuse_duplicate_call_ids(response: ModelResponse) -> None:
    """Refuse a model reply in which two tool calls share an id (two empty ids count), as
    the Runner does (P39 G6): approvals, recorded gates and ledger entries are keyed by the
    call id, so such calls would be released or refused together. Raised before any gate,
    hook, ledger entry or dispatch."""
    seen: set[str] = set()
    for call in response.message.tool_calls or ():
        if call.id in seen:
            raise ProviderResponseError(
                f"Model reply has more than one tool call with id {call.id!r}; "
                "tool call ids must be unique within a reply."
            )
        seen.add(call.id)


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
    The workflow step (``step_key``, else ``attempt_id``) is part of it, so a node visited
    again in a cycle asks afresh (P39 G2 review R2-M1).
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
            _step_identity(context),
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


def _step_identity(context: ExecutionContext) -> str:
    """Which visit of the node this is: the workflow journal's ``step_key`` (stable across
    the approval resume and recovery of one step, different for each visit), else the
    ``attempt_id``."""
    step_key = getattr(context, "step_key", None)
    return f"step:{step_key}" if step_key else f"attempt:{context.attempt_id}"


def _digest_id(prefix: str, *parts: Any) -> str:
    key = json.dumps([str(p) for p in parts], ensure_ascii=False)
    return f"{prefix}_{hashlib.sha256(key.encode('utf-8')).hexdigest()[:32]}"


def _scope_key(context: ExecutionContext) -> str:
    """One run visit: workspace, run, node and step (the ledger's scope)."""
    return _digest_id(
        "coord_scope",
        context.workspace_id,
        context.run_id,
        context.node_id,
        _step_identity(context),
    )


def _coordinator_run_marker(context: ExecutionContext) -> str:
    """The ledger key that records a coordinator run visit as completed."""
    return _digest_id("coord_run", _scope_key(context), "completed")


def _arguments_digest(arguments: Any) -> str:
    try:
        return canonical_digest(arguments)
    except (TypeError, ValueError):
        return "uncanonical"


def _response_to_json(response: ModelResponse) -> str:
    message = response.message
    return json.dumps(
        {
            "content": message.content,
            "tool_calls": [
                {"id": c.id, "name": c.name, "arguments": c.arguments}
                for c in (message.tool_calls or [])
            ],
            "finish_reason": response.finish_reason,
        },
        ensure_ascii=False,
    )


def _response_from_json(data: str) -> ModelResponse:
    raw = json.loads(data)
    calls = [
        ToolCall(id=str(c["id"]), name=str(c["name"]), arguments=dict(c["arguments"]))
        for c in raw.get("tool_calls") or []
    ]
    return ModelResponse(
        message=Message(role="assistant", content=raw.get("content"), tool_calls=calls or None),
        usage=None,  # a replayed response was billed once, when it was made
        finish_reason=raw.get("finish_reason"),
    )


def _audit_replay(
    context: ExecutionContext, what: str, agent_name: str, tool_name: str, entry_id: str
) -> None:
    """Tell observers a step was replayed from the ledger, not executed (R2-m2): a module
    progress event on the context's event sink and a ``nexura.coordinator.replayed`` trace
    event. Neither carries arguments or results."""
    from nexura_agents import tracing
    from nexura_agents.contracts.events import ModuleEventType

    payload = {
        "event": f"coordinator.{what}_replayed",
        "agent": agent_name,
        "tool": tool_name,
        "entry_id": entry_id,
        "executed": False,
    }
    with contextlib.suppress(Exception):
        context.emit_event(
            ModuleEventType.INVOKE_PROGRESS,
            payload,
            module_id="nexura.agents.coordinator",
            version="1.0.0",
        )
    tracing.record_event(
        "nexura.coordinator.replayed",
        {
            "nexura.coordinator.replayed": what,
            "gen_ai.agent.name": agent_name,
            "gen_ai.tool.name": tool_name,
        },
    )


class AgentCoordinator:
    """Orchestrates multi-agent execution, recursive handoffs, turn fuel, and capability limits.

    Tool calls get what a ``Runner`` gives them (P39 G2): the tool's input and output
    guardrails, the hooks' ``before_tool`` / ``after_tool``, and approval when the agent
    has ``approval_required``, the tool has ``requires_approval``, or a hook's
    ``tool_needs_approval`` says so. The hooks are the ones a ``Runner`` would use: those
    inherited from an enclosing run, the process default (``set_default_hooks``; left out
    with ``default_hooks=False``, for tests) and ``hooks``. They stay active while the
    coordinator runs, so runs started by its tools inherit them.

    After a human approval the run is continued by running it again with the same
    ``ExecutionContext``. With an ``approval_gate``, a ledger records what each run visit
    (workspace, run, node, workflow step) did: tool calls by position, with their tool,
    argument digest and result, and the model's responses. It lives in the gate's
    ``ApprovalPolicyManager`` database (``CoordinatorCallLedger``), or in memory for one
    coordinator object when the gate has none (``MemoryCallLedger``). On the re-run:

    - a call that already ran is replayed from its recorded result, never executed again;
      a run with several gated calls completes, claiming each approval once;
    - a call that was dispatched but has no result (the process stopped, the tool raised)
      is sent again only for a ``pure`` or ``idempotent`` tool;
    - if the model now asks for a different call at a position where a call already ran,
      or repeats an ungated, non-idempotent call an earlier attempt already sent, the run
      stops with ``CoordinatorCallOutcomeUnknownError`` instead of sending anything;
    - with ``replay_model_responses=True`` the recorded model responses are replayed
      instead of calling the model again, so the re-run follows the recorded path exactly
      (and is not billed again), then continues live past it.

    A completed run visit's entries are deleted; running it again raises
    ``ApprovalClaimConflictError`` when it used approvals, else
    ``CoordinatorRunCompletedError``. Replayed steps are reported as
    ``coordinator.*_replayed`` progress events and ``nexura.coordinator.replayed`` trace
    events.
    """

    def __init__(
        self,
        agents: Sequence[AgentDefinition],
        *,
        max_handoff_depth: int = 5,
        total_fuel: int = 25,
        approval_gate: ApprovalGate | None = None,
        session_store: SessionStore | None = None,
        hooks: AgentHooks | Sequence[AgentHooks] | None = None,
        default_hooks: bool = True,
        replay_model_responses: bool = False,
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
        if not isinstance(default_hooks, bool):
            raise SDKValidationError("default_hooks must be True or False.")
        policy.check_opt_out(default_hooks)  # refused while the operator's default is locked
        self.hooks = policy.compose_hooks(hooks)
        self.use_default_hooks = default_hooks
        if not isinstance(replay_model_responses, bool):
            raise SDKValidationError("replay_model_responses must be True or False.")
        self.replay_model_responses = replay_model_responses
        # Used when the approval gate has no ApprovalPolicyManager database (R2-m1).
        self._memory_ledger = MemoryCallLedger()

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

    def replace(self, **changes: Any) -> AgentCoordinator:
        """A new coordinator with the same settings (agents, limits, approval gate, session
        store, hooks and ``default_hooks``) and ``changes`` applied; ``agents`` takes a
        sequence of ``AgentDefinition``."""
        settings: dict[str, Any] = {
            "agents": list(self.agents.values()),
            "max_handoff_depth": self.max_handoff_depth,
            "total_fuel": self.total_fuel,
            "approval_gate": self.approval_gate,
            "session_store": self.session_store,
            "hooks": self.hooks,
            "default_hooks": self.use_default_hooks,
            "replay_model_responses": self.replay_model_responses,
        }
        unknown = sorted(set(changes) - set(settings))
        if unknown:
            raise SDKValidationError(f"replace() got unknown arguments: {', '.join(unknown)}.")
        settings.update(changes)
        agents = settings.pop("agents")
        return AgentCoordinator(agents, **settings)

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

        hooks = policy.effective_hooks(self.hooks, use_default=self.use_default_hooks)
        ledger: Any = CoordinatorCallLedger.for_gate(self.approval_gate)
        if ledger is None and self.approval_gate is not None:
            ledger = self._memory_ledger  # no database: this coordinator object only
        workspace = effective_context.workspace_id
        scope = _scope_key(effective_context)
        attempt = uuid.uuid4().hex  # this attempt of the run visit
        run_marker = _coordinator_run_marker(effective_context)
        used_approvals: list[str] = []  # stable ids of the gated calls this run ran under
        if ledger is not None:
            finished = ledger.get(workspace, run_marker)
            if finished is not None and finished.status == "done":
                approvals = json.loads(finished.result or "[]")
                if approvals:
                    # A completed run's approvals are spent: a replay is refused, as when
                    # the approval was claimed twice (nothing is dispatched again).
                    raise ApprovalClaimConflictError(
                        f"Run '{root_run_id}' already completed under its approvals, which "
                        "are single use; nothing was dispatched again. Start a new run.",
                        approval_id=str(approvals[0]),
                        claimed_by=f"{root_run_id}:completed",
                    )
                raise CoordinatorRunCompletedError(root_run_id)
        budget_token = admission.active_budget.set(admission.effective_budget(effective_context))
        token = current_agent_context.set(effective_context)
        hooks_token = policy.active_hooks.set(hooks)
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
                        origin="handoff",
                    )
                    run_tools[h_tool.name] = h_tool
                handoff_names = {f"transfer_to_{h.target_name}" for h in agent.handoffs}

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
                agent_digest = agent.definition_digest()  # an edited agent never replays
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

                    # Invoke the model, or replay the response an earlier attempt of this
                    # run visit recorded at this position (replay_model_responses).
                    model_entry = _digest_id(
                        "coord_model", scope, agent.name, agent_digest, handoffs_count, agent_turns
                    )
                    recorded_response = (
                        ledger.get(workspace, model_entry)
                        if ledger is not None and self.replay_model_responses
                        else None
                    )
                    if recorded_response is not None and recorded_response.result is not None:
                        response = _response_from_json(recorded_response.result)
                        refuse_duplicate_call_ids(response)
                        _audit_replay(effective_context, "model", agent.name, "", model_entry)
                    else:
                        response = await admission.generate(
                            agent.model,
                            messages=copy.deepcopy(agent_messages),
                            tools=tools_schema,
                            context=effective_context,
                        )
                        # Before the ledger records the reply, any gate, hook or dispatch.
                        refuse_duplicate_call_ids(response)
                        if ledger is not None:
                            stored = ledger.record_once(
                                workspace,
                                model_entry,
                                Entry(scope, "model", root_run_id, attempt),
                                _response_to_json(response),
                            )
                            if self.replay_model_responses and stored != _response_to_json(
                                response
                            ):
                                # A concurrent attempt recorded this turn first: follow it.
                                response = _response_from_json(stored)

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

                        if ledger is not None:
                            # The run visit is finished: its entries go, and a marker
                            # refuses running it again (R2-m2 cleanup).
                            ledger.clear_scope(workspace, scope)
                            ledger.finish(
                                workspace,
                                run_marker,
                                Entry(scope, "run", root_run_id, attempt),
                                json.dumps(used_approvals),
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

                        # A handoff is not a tool run (as in the Runner): no tool hooks.
                        is_handoff = tc.name in handoff_names
                        tool_ctx: RunContext[Any] = RunContext(
                            deps=None,
                            run_id=effective_context.run_id,
                            agent_name=agent.name,
                            turn=agent_turns,
                            messages=list(agent_messages),
                            tool_call_id=tc.id,
                            tool_name=tc.name,
                            tool=target_tool.info(),
                        )
                        # The approval request's stable id (run visit, agent, position, tool,
                        # arguments) and the ledger entry of this position.
                        call_id = _coordinator_approval_id(
                            effective_context,
                            agent.name,
                            handoffs_count,
                            agent_turns,
                            call_index,
                            tc.name,
                            tc.arguments,
                        )
                        args_digest = _arguments_digest(tc.arguments)
                        entry_id = _digest_id(
                            "coord_call",
                            scope,
                            agent.name,
                            agent_digest,
                            handoffs_count,
                            agent_turns,
                            call_index,
                        )
                        entry = Entry(scope, "call", root_run_id, attempt, tc.name, args_digest)
                        # Approval: the agent's flag, the tool's own requires_approval, or
                        # a hook gate (P39 G2; any one gates).
                        gated = agent.approval_required or (
                            not is_handoff
                            and policy.tool_call_needs_approval(
                                target_tool, tool_ctx, dict(tc.arguments), hooks
                            )
                        )
                        record = (
                            ledger.get(workspace, entry_id)
                            if ledger is not None and not is_handoff
                            else None
                        )
                        if record is not None and (record.tool_name, record.args_digest) != (
                            tc.name,
                            args_digest,
                        ):
                            # A call already ran at this position and the model now asks
                            # for another: the re-run diverged. Stop before sending (R2-M2).
                            raise CoordinatorCallOutcomeUnknownError(
                                agent.name,
                                tc.name,
                                entry_id,
                                detail=(
                                    f"The re-run of coordinator run '{root_run_id}' diverged: "
                                    f"'{record.tool_name}' already ran at this position and "
                                    f"the model now asks for '{tc.name}' (or other "
                                    "arguments). Nothing was sent; start a new run, or use "
                                    "replay_model_responses=True."
                                ),
                            )
                        if record is not None and record.status == "done":
                            # Ran in an earlier attempt of this run (before an approval
                            # pause): replay its result, never execute it twice (M3).
                            if gated:
                                used_approvals.append(call_id)
                            _audit_replay(effective_context, "tool", agent.name, tc.name, entry_id)
                            agent_tool_calls += 1
                            agent_messages.append(
                                Message(
                                    role="tool",
                                    name=tc.name,
                                    tool_call_id=tc.id,
                                    content=record.result or "",
                                )
                            )
                            continue
                        repeatable = target_tool.effect_class().value in ("pure", "idempotent")
                        rerun_in_doubt = False
                        if record is not None:
                            # Dispatched by an earlier attempt, no result: outcome unknown.
                            if not repeatable:
                                raise CoordinatorCallOutcomeUnknownError(
                                    agent.name, tc.name, entry_id
                                )
                            rerun_in_doubt = True
                        elif (
                            ledger is not None
                            and not is_handoff
                            and not gated
                            and not repeatable
                            and ledger.same_call_elsewhere(
                                workspace, scope, tc.name, args_digest, attempt
                            )
                        ):
                            # An earlier attempt of this run already sent this exact call; a
                            # re-run that asks for it again elsewhere does not resend it.
                            raise CoordinatorCallOutcomeUnknownError(
                                agent.name,
                                tc.name,
                                entry_id,
                                detail=(
                                    f"'{tc.name}' with these arguments was already sent by an "
                                    f"earlier attempt of coordinator run '{root_run_id}'; it "
                                    "is not sent again. Start a new run if it should repeat."
                                ),
                            )
                        if gated:
                            if self.approval_gate is not None:
                                # Re-running the same run after the approval finds and
                                # claims it (like the runner's resume). Bound to the
                                # agent's definition too.
                                self.approval_gate.check_or_request_approval(
                                    effective_context,
                                    action_name=tc.name,
                                    payload=tc.arguments,
                                    action_kind="tool_call",
                                    request_id=call_id,
                                    spec_digest=agent.definition_digest(),
                                )
                                used_approvals.append(call_id)
                            else:
                                raise AgentApprovalRequiredError(agent.name, action_name=tc.name)

                        # Execute tool: guardrails and hooks as in the Runner. A workflow node
                        # counts as started before its first tool call (internal gate).
                        admission.before_dispatch()
                        if is_handoff:
                            tool_res = await target_tool.arun(**tc.arguments)
                        else:
                            dispatching_agent: str = agent.name

                            def on_dispatch(
                                entry_id: str = entry_id,
                                entry: Entry = entry,
                                again: bool = rerun_in_doubt,
                                agent_name: str = dispatching_agent,
                            ) -> None:
                                if ledger is None:
                                    return
                                if not ledger.start(workspace, entry_id, entry, again=again):
                                    # Another attempt of this run recorded it first.
                                    raise CoordinatorCallOutcomeUnknownError(
                                        agent_name, entry.tool_name, entry_id
                                    )

                            tool_res = await _run_coordinated_tool(
                                target_tool, tool_ctx, tc, hooks, on_dispatch=on_dispatch
                            )
                            if ledger is not None:
                                ledger.finish(workspace, entry_id, entry, str(tool_res))
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
            policy.active_hooks.reset(hooks_token)
            current_agent_context.reset(token)
            admission.active_budget.reset(budget_token)


async def _run_coordinated_tool(
    tool: Tool,
    ctx: RunContext[Any],
    call: Any,
    hooks: AgentHooks | None,
    *,
    on_dispatch: Any = None,
) -> str:
    """One coordinator tool call through the tool's guardrails and the hooks
    (``policy.run_tool_call``). A guardrail's rejection is the result the model sees.
    ``on_dispatch()`` runs right before the tool itself (not for a hook's answer or a
    rejected call)."""

    async def execute() -> str:
        if on_dispatch is not None:
            on_dispatch()
        result = await tool.arun(**call.arguments)
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)

    text, _rejected = await policy.run_tool_call(tool, ctx, call, hooks, execute)
    return str(text)
