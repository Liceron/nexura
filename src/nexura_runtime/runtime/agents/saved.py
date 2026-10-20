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

from nexura_runtime.agents_spec import AgentSpecError
from nexura_runtime.agents_store import AgentStoreError

AGENT_NODE_MODULE_ID = "nexura.agents.agent"
#: ``details["kind"]`` of the approvals this node requests.
AGENT_APPROVAL_KIND = "agent_tool_calls"
#: Longest prompt (characters) the node passes to the agent.
MAX_AGENT_INPUT_CHARS = 32_000
#: Largest paused-run snapshot (JSON characters) kept with one approval request.
MAX_RESUME_CHARS = 400_000


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

    async def invoke(
        self,
        context: ExecutionContext,
        inputs: dict[str, Any],
        config: dict[str, Any] | None = None,
    ) -> ModuleResult:
        from nexura_runtime.workflow import approved_request, request_approval

        cfg = config or {}
        agent_id = cfg.get("agent_id")
        input_key = str(cfg.get("input_key") or "input")
        output_key = str(cfg.get("output_key") or "output")
        if not isinstance(agent_id, str) or not agent_id:
            return ModuleResult.failure("AGENT_NOT_CONFIGURED", "Node config needs an agent_id.")
        if self.agent_source is None:
            return ModuleResult.failure(
                "AGENT_STORE_UNAVAILABLE",
                "This node names a saved agent but no agent store is connected.",
            )
        try:
            built = self.agent_source.build_agent(context.workspace_id, agent_id, context)
        except (AgentStoreError, AgentSpecError) as exc:
            code = getattr(exc, "code", "AGENT_INVALID")
            return ModuleResult.failure(str(code), str(exc))

        grant = approved_request()
        resuming = (
            grant is not None
            and grant.get("kind") == AGENT_APPROVAL_KIND
            and grant.get("agent_id") == agent_id
            and grant.get("node_id") == context.node_id
        )
        if resuming and grant is not None and grant.get("agent_revision") != built.revision:
            return ModuleResult.failure(
                "AGENT_CHANGED",
                f"Agent '{agent_id}' was edited after this approval was requested "
                f"(revision {grant.get('agent_revision')} -> {built.revision}); start a new run.",
            )
        if not resuming and input_key not in inputs:
            return ModuleResult.failure(
                "AGENT_INPUT_MISSING", f"The workflow state has no '{input_key}' for the agent."
            )

        timeout = built.timeout
        remaining = context.remaining_seconds()
        if remaining is not None:
            timeout = max(0.001, min(timeout, remaining))
        runner = Runner()
        try:
            if resuming and grant is not None:
                interruption = RunInterruption.from_json(str(grant["resume"]))
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
            return ModuleResult.failure("AGENT_RUN_FAILED", f"{type(exc).__name__}: {exc}"[:500])

        if result.status == "paused" and result.interruption is not None:
            return self._ask_approval(request_approval, built, context, result.interruption)
        if result.status != "completed":
            return ModuleResult.failure(
                "AGENT_RUN_FAILED", f"Agent '{built.name}' ended with status '{result.status}'."
            )
        output = result.output if isinstance(built.agent.output_type, dict) else result.final_output
        return ModuleResult.success(
            outputs={output_key: output},
            usage=result.usage,
            metadata={
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
