# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""``nexura.agents.agent``: a workflow node that runs an agent saved in the run's workspace.

The node's config names a saved agent (``agent_id``) and the state keys it reads and writes.
At run time the bound :class:`AgentSource` (the service's agent runtime) builds an
``nexura_agents.Agent`` from the stored spec for the run's own workspace, so an id from
another workspace is not found. When the agent calls a tool that needs approval (any module
that can write), the run pauses on the workflow approval path
(:func:`nexura_runtime.workflow.request_approval`) with the exact pending calls; approving
resumes the agent from that point and runs only those calls, rejecting ends the run without
running them.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Protocol

from nexura_agents.agent import Agent
from nexura_agents.contracts.base import BaseModule
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.manifest import ModuleManifest
from nexura_agents.contracts.result import ModuleResult
from nexura_agents.errors import WorkflowError
from nexura_agents.runner import Runner
from nexura_agents.types import RunInterruption, RunResult
from nexura_agents.usage import admission
from nexura_agents.usage.errors import UsageError

from nexura_runtime.agents_spec import AgentSpecError
from nexura_runtime.agents_store import AgentStoreError

AGENT_NODE_MODULE_ID = "nexura.agents.agent"
#: ``details["kind"]`` of the approvals this node requests.
AGENT_APPROVAL_KIND = "agent_tool_calls"
#: Longest prompt (characters) the node passes to the agent.
MAX_AGENT_INPUT_CHARS = 32_000
#: Largest paused-run snapshot (JSON characters) kept with one approval request.
MAX_RESUME_CHARS = 400_000


class _StartGate:
    """Marks the workflow node started once, right before the agent first sends anything."""

    def __init__(self, mark: Any) -> None:
        self._mark = mark
        self.started = False

    def __call__(self) -> None:
        if not self.started:
            self._mark()  # raises (deadline, cancelled) when the step may no longer start
            self.started = True


@dataclass(frozen=True)
class BuiltAgent:
    """An agent ready to run, built from a saved spec."""

    agent: Agent[Any]
    agent_id: str
    revision: int
    name: str
    #: Names of the tools whose calls need approval.
    approval_tools: tuple[str, ...] = ()
    timeout: float = 60.0


class AgentSource(Protocol):
    """Builds saved agents for one workspace (the service agent runtime fits)."""

    def build_agent(
        self, workspace_id: str, agent_id: str, context: ExecutionContext | None
    ) -> BuiltAgent:
        """Build ``agent_id`` of ``workspace_id``; raises when it does not exist there."""
        ...


def _prompt(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text[:MAX_AGENT_INPUT_CHARS]


class SavedAgentModule(BaseModule):
    """Workflow node running a saved workspace agent (see the module docstring)."""

    def __init__(self, source: AgentSource | None = None, *, version: str = "1.0.0") -> None:
        self.agent_source = source
        self._manifest = ModuleManifest(
            schema_version=1,
            module_id=AGENT_NODE_MODULE_ID,
            version=version,
            kind="agent",
            title="Agent",
            description=(
                "Runs an agent saved in this workspace (Agents in Studio). Reads config "
                "input_key from the state and writes the answer to output_key; tool calls "
                "that can write pause the run for approval."
            ),
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            config_schema={
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 64,
                        "description": "Saved agent id (agt_...) of this workspace",
                    },
                    "input_key": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 200,
                        "default": "input",
                        "description": "State key holding the agent's input (default input)",
                    },
                    "output_key": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 200,
                        "default": "output",
                        "description": "State key receiving the answer (default output)",
                    },
                },
                "required": ["agent_id"],
            },
            named_ports=[],
            capabilities={"streaming": False, "network": False},
            # Tool calls may write; a retry could repeat them, so the node is never retried.
            effect_policy="non_idempotent_write",
            min_sdk_version="0.3.0",
            timeout_seconds=330.0,
            output_limit_bytes=2_000_000,
            supported_modalities=["text"],
            supported_locales=["all"],
            source_reference="runtime:agents.saved",
            verification_status="mock_verified",
        )

    @property
    def manifest(self) -> ModuleManifest:
        return self._manifest

    def bind_agent_source(self, source: AgentSource) -> None:
        """Read saved agents from ``source`` (the service binds its agent runtime)."""
        self.agent_source = source

    @staticmethod
    def _refused(code: str, message: str) -> ModuleResult:
        """A validated refusal before Runner or any pending approved tool is invoked.

        Returned before ``_mark_dispatch_started``: the workflow settles the node's intent
        ``not_applied`` only because its start marker is still unset.
        """
        return ModuleResult.failure(code, message)

    async def invoke(
        self,
        context: ExecutionContext,
        inputs: dict[str, Any],
        config: dict[str, Any] | None = None,
    ) -> ModuleResult:
        from nexura_runtime.workflow import (
            _dispatch_already_started,
            _mark_dispatch_started,
            _note_tool_results,
            approved_request,
            request_approval,
        )

        cfg = config or {}
        agent_id = cfg.get("agent_id")
        input_key = str(cfg.get("input_key") or "input")
        output_key = str(cfg.get("output_key") or "output")
        if not isinstance(agent_id, str) or not agent_id:
            return self._refused("AGENT_NOT_CONFIGURED", "Node config needs an agent_id.")
        if self.agent_source is None:
            return self._refused(
                "AGENT_STORE_UNAVAILABLE",
                "This node names a saved agent but no agent store is connected.",
            )
        try:
            built = self.agent_source.build_agent(context.workspace_id, agent_id, context)
        except (AgentStoreError, AgentSpecError) as exc:
            code = getattr(exc, "code", "AGENT_INVALID")
            return self._refused(str(code), str(exc))

        grant = approved_request()
        resuming = (
            grant is not None
            and grant.get("kind") == AGENT_APPROVAL_KIND
            and grant.get("agent_id") == agent_id
            and grant.get("node_id") == context.node_id
        )
        if resuming and grant is not None and grant.get("agent_revision") != built.revision:
            return self._refused(
                "AGENT_CHANGED",
                f"Agent '{agent_id}' was edited after this approval was requested "
                f"(revision {grant.get('agent_revision')} -> {built.revision}); start a new run.",
            )
        if not resuming and input_key not in inputs:
            return self._refused(
                "AGENT_INPUT_MISSING", f"The workflow state has no '{input_key}' for the agent."
            )

        timeout = built.timeout
        remaining = context.remaining_seconds()
        if remaining is not None:
            timeout = max(0.001, min(timeout, remaining))
        runner = Runner(
            budget=context.budget,
            approval_ttl=float(context.attributes.get("approval_ttl", 3600.0)),
        )
        # Every refusal above ran nothing. A resumed run runs approved tool calls first, so it
        # counts as started now (internal: the workflow marker; outside a workflow a no-op).
        # A fresh run under a budget counts as started right before its first model call is
        # sent (after admission) or its first tool runs (dispatch gate): a budget refusal of
        # the first call, raised before anything was sent, is then a known "not dispatched"
        # failure, never an unknown effect (live pass C2). Without a budget a model call is
        # not admitted, so the node counts as started here, as before.
        gate = _StartGate(_mark_dispatch_started)
        if resuming or context.budget is None or _dispatch_already_started():
            # Also a re-run of a key an earlier attempt dispatched (after an interrupt()
            # pause): a refusal now does not prove that nothing was sent.
            gate()
        token = admission.dispatch_gate.set(gate)
        try:
            if resuming and grant is not None:
                interruption = RunInterruption.from_json(str(grant["resume"]))
                # approved_request is supplied only by the workflow after checking its
                # stored identity, TTL and single-use claim. It is fresh authority for
                # these exact calls, even if the SDK snapshot's shorter TTL elapsed.
                # Rebind only the dispatch window, never a client-supplied approval.
                interruption.expires_at = time.time() + timeout
                result: RunResult[Any] = await runner.resume_async(
                    built.agent,
                    interruption,
                    approve={str(c["id"]): str(c["digest"]) for c in grant.get("calls", [])},
                    timeout=timeout,
                )
            else:
                result = await runner.run_async(
                    built.agent,
                    _prompt(inputs[input_key]),
                    timeout=timeout,
                    metadata={"agent_id": agent_id, "workflow_run_id": context.run_id},
                )
        except Exception as exc:
            if not gate.started and isinstance(exc, UsageError):
                return self._refused(
                    "AGENT_BUDGET_REFUSED",
                    f"The run's budget refused the agent's first call; nothing was sent. "
                    f"{type(exc).__name__}: {exc}"[:500],
                )
            return ModuleResult.failure("AGENT_RUN_FAILED", f"{type(exc).__name__}: {exc}"[:500])
        finally:
            admission.dispatch_gate.reset(token)

        if result.status == "paused" and result.interruption is not None:
            # The node's outcome so far: each call its run answered, with how it ended.
            _note_tool_results(_tool_outcomes(result))
            return self._ask_approval(request_approval, built, context, result.interruption)
        if result.status != "completed":
            return ModuleResult.failure(
                "AGENT_RUN_FAILED", f"Agent '{built.name}' ended with status '{result.status}'."
            )
        output = result.output if isinstance(built.agent.output_type, dict) else result.final_output
        metadata: dict[str, Any] = {}
        model_id = _model_id(built.agent.model)
        if model_id is not None:
            metadata["model_id"] = model_id  # traces price the node's usage with it
        return ModuleResult.success(
            outputs={output_key: output},
            usage=result.usage,
            metadata={
                **metadata,
                "agent_id": agent_id,
                "agent_revision": built.revision,
                "turns": result.turns_count,
                "tool_calls": result.tool_calls_count,
            },
        )

    @staticmethod
    def _ask_approval(
        request_approval: Any,
        built: BuiltAgent,
        context: ExecutionContext,
        interruption: RunInterruption,
    ) -> ModuleResult:
        digests = interruption.pending_digests()
        calls: list[dict[str, Any]] = [
            {"id": c.id, "tool": c.name, "arguments": c.arguments, "digest": digests[c.id]}
            for c in interruption.pending
        ]
        resume = interruption.to_json()
        if len(resume) > MAX_RESUME_CHARS:
            return ModuleResult.failure(
                "AGENT_PAUSE_TOO_LARGE",
                f"Agent '{built.name}' paused for approval but its run is larger than "
                f"{MAX_RESUME_CHARS} characters; shorten the input or instructions.",
            )
        tools = ", ".join(sorted({str(c["tool"]) for c in calls}))
        try:
            request_approval(
                {
                    "kind": AGENT_APPROVAL_KIND,
                    "message": f"Agent '{built.name}' wants to run {tools}; approve to run "
                    "exactly these calls, reject to stop the run without running them.",
                    "agent_id": built.agent_id,
                    "agent_name": built.name,
                    "agent_revision": built.revision,
                    "node_id": context.node_id,
                    "calls": calls,
                    "resume": resume,
                }
            )
        except WorkflowError as exc:
            # Outside a workflow (or inside a parallel step) there is no way to ask.
            return ModuleResult.failure(
                "APPROVAL_UNAVAILABLE",
                f"Agent '{built.name}' needs approval to run {tools}, which only a workflow "
                f"run can ask for: {exc}",
            )
        raise AssertionError("request_approval() returned")  # pragma: no cover


def _model_id(model: Any) -> str | None:
    """The id a model reports to budgets (``model_id``, ``model`` or ``model_name``)."""
    for attr in ("model_id", "model", "model_name"):
        value = getattr(model, attr, None)
        if isinstance(value, str) and value:
            return value
    return None


#: How a tool call of the agent run ended, from the run's events (S12 round 7, L6-4).
_RETRY_STATUS = {
    "tool_timeout": "no_result",  # dispatched; no result before its timeout
    "outcome_unknown": "no_result",  # dispatched by an earlier attempt; outcome unknown
    "model_retry": "raised",  # the tool ran and raised ModelRetry
    "rejected": "not_run",  # a person rejected it
    "invalid_arguments": "not_run",  # refused before it ran
    "guardrail_rejected": "guardrail",  # refused before it ran, or its result withheld
}


def _tool_outcomes(result: RunResult[Any]) -> list[dict[str, Any]]:
    """``[{id, tool, status}]`` for every tool message of the run.

    ``status``: ``succeeded`` (returned a result), ``raised``, ``no_result``, ``not_run``,
    ``guardrail`` (refused or its result withheld: known either way) or ``earlier`` (the
    result was replayed from an earlier attempt of this node; the workflow fills in what
    that attempt recorded).
    """
    from nexura_agents.events import RunEventType

    reasons: dict[str, str] = {}
    completed: set[str] = set()
    for event in result.events:
        call_id = (event.data or {}).get("tool_call_id")
        if not isinstance(call_id, str):
            continue
        if event.type == RunEventType.RETRY:
            reasons[call_id] = str(event.data.get("reason"))
        elif event.type == RunEventType.TOOL_COMPLETED:
            completed.add(call_id)
    outcomes = []
    for message in result.messages:
        if message.role != "tool":
            continue
        call_id = str(message.tool_call_id)
        reason = reasons.get(call_id)
        if reason is not None:
            status = _RETRY_STATUS.get(reason, "no_result")
        elif call_id in completed:
            status = "succeeded"
        else:
            status = "earlier"
        outcomes.append({"id": call_id, "tool": message.name, "status": status})
    return outcomes


def create_saved_agent_module() -> SavedAgentModule:
    """The unbound ``nexura.agents.agent`` module (the service binds an agent source)."""
    return SavedAgentModule()


__all__ = [
    "AGENT_APPROVAL_KIND",
    "AGENT_NODE_MODULE_ID",
    "AgentSource",
    "BuiltAgent",
    "SavedAgentModule",
    "create_saved_agent_module",
]
