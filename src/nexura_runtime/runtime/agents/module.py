# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""B00/B01 module contract adapter for agent coordinator and multi-agent workflows."""

from __future__ import annotations

import copy
from collections.abc import Sequence
from typing import Any, Protocol

from nexura_agents.contracts.base import BaseModule
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.manifest import EffectPolicy, ModuleManifest
from nexura_agents.contracts.result import ModuleResult
from nexura_agents.providers.fake import StreamingFakeProvider
from nexura_agents.skills import SkillError, SkillLibrary
from nexura_agents.tools import tool

from nexura_runtime.runtime.agents.coordinator import AgentCoordinator
from nexura_runtime.runtime.agents.definition import AgentDefinition


class SkillSource(Protocol):
    """Where an agent node reads skills from (the service ``SkillStore`` fits)."""

    def library_for(self, workspace_id: str, names: Sequence[str], label: str) -> SkillLibrary:
        """A library holding just ``names`` at ``label`` for one workspace."""
        ...


def _with_skills(
    coordinator: AgentCoordinator, library: SkillLibrary, label: str
) -> AgentCoordinator:
    """A copy of ``coordinator`` whose agents carry the skill catalog and ``load_skill``."""
    agents = []
    for original in coordinator.agents.values():
        agent = copy.copy(original)
        have = [t.name for t in original.tools]
        base = original.instructions
        catalog = library.catalog(label=label)

        def instructions(context: Any = None, _base: Any = base, _catalog: str = catalog) -> str:
            text = _base(context) if callable(_base) else str(_base)
            return f"{text}\n\n{_catalog}" if text else _catalog

        loader = library.loader_tool(label=label, available_tools=have)
        agent.instructions = instructions
        agent.tools = (*original.tools, loader)
        agent.tools_by_name = {t.name: t for t in agent.tools}
        if original.allowed_tools is not None:
            agent.allowed_tools = (*original.allowed_tools, loader.name)
        agents.append(agent)
    # Every other setting (limits, approval gate, session store, hooks, default_hooks)
    # carries over: a new field cannot be forgotten here (P39 G2 review M2).
    return coordinator.replace(agents=agents)


class AgentCoordinatorModule(BaseModule):
    """B00/B01 module wrapping an AgentCoordinator execution."""

    def __init__(
        self,
        coordinator: AgentCoordinator,
        *,
        default_agent_name: str | None = None,
        module_id: str = "nexura.agents.coordinator",
        version: str = "1.0.0",
        # The coordinator's agents may call any tool: undeclared effects are non_idempotent.
        effect_policy: EffectPolicy = "non_idempotent_write",
        skill_source: SkillSource | None = None,
    ) -> None:
        self.coordinator = coordinator
        self.skill_source = skill_source
        self.default_agent_name = default_agent_name or next(iter(coordinator.agents.keys()))

        self._manifest = ModuleManifest(
            schema_version=1,
            module_id=module_id,
            version=version,
            kind="agent",
            title="Multi-Agent Coordinator",
            description=(
                "Offline demo: a canned support agent (no model, no keys). Build your own "
                "agent in Studio and run it with the nexura.agents.agent node."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "User query or task prompt"},
                    "initial_agent": {
                        "type": "string",
                        "description": "Starting agent name",
                    },
                    "shared_memory": {
                        "type": "object",
                        "description": "Optional shared memory dictionary",
                    },
                },
                "required": ["prompt"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "final_output": {"type": "string"},
                    "final_agent": {"type": "string"},
                    "handoffs_count": {"type": "integer"},
                    "total_fuel_consumed": {"type": "integer"},
                    "traces": {"type": "array"},
                },
                "required": ["final_output", "final_agent"],
            },
            config_schema={
                "type": "object",
                "properties": {
                    "skills": {
                        "type": "array",
                        "items": {"type": "string", "maxLength": 64},
                        "maxItems": 20,
                        "description": "Workspace skill names added to the agents' catalog",
                    },
                    "skills_label": {
                        "type": "string",
                        "default": "production",
                        "description": "Skill label to read (default production)",
                    },
                },
            },
            named_ports=[],
            capabilities={"streaming": False, "network": False},
            effect_policy=effect_policy,
            dependency_extra=None,
            min_sdk_version="0.3.0",
            timeout_seconds=120.0,
            output_limit_bytes=2_000_000,
            required_credential_types=[],
            supported_modalities=["text"],
            supported_locales=["all"],
            source_reference="runtime:agents",
            verification_status="mock_verified",
        )

    @property
    def manifest(self) -> ModuleManifest:
        return self._manifest

    def bind_skill_source(self, source: SkillSource) -> None:
        """Let this node attach workspace skills named in its ``skills`` config."""
        self.skill_source = source

    async def invoke(
        self,
        context: ExecutionContext,
        inputs: dict[str, Any],
        config: dict[str, Any] | None = None,
    ) -> ModuleResult:
        prompt = str(inputs.get("prompt", ""))
        agent_name = str(inputs.get("initial_agent", self.default_agent_name))
        shared_memory = inputs.get("shared_memory")

        coordinator = self.coordinator
        names = (config or {}).get("skills") or []
        if names:
            label = str((config or {}).get("skills_label") or "production")
            try:
                if self.skill_source is None:
                    raise SkillError("This node lists skills but no skill store is connected.")
                if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
                    raise SkillError("Node config 'skills' must be a list of skill names.")
                names = list(dict.fromkeys(names))  # a name listed twice is attached once
                library = self.skill_source.library_for(context.workspace_id, names, label)
                coordinator = _with_skills(coordinator, library, label)
            except SkillError as exc:
                # Before _mark_dispatch_started: nothing ran (the workflow's marker decides).
                return ModuleResult.failure("SKILL_INVALID", str(exc))

        from nexura_runtime.workflow import _mark_dispatch_started

        _mark_dispatch_started()  # internal: the coordinator's models and tools run from here
        result = await coordinator.run_async(
            initial_agent_name=agent_name,
            prompt=prompt,
            context=context,
            shared_memory=shared_memory,
        )

        traces_data = [
            {
                "agent_name": t.agent_name,
                "run_id": t.run_id,
                "parent_run_id": t.parent_run_id,
                "turns": t.turns,
                "tool_calls_count": t.tool_calls_count,
                "handoff_to": t.handoff_to,
                "handoff_reason": t.handoff_reason,
            }
            for t in result.traces
        ]

        return ModuleResult.success(
            outputs={
                "final_output": result.final_output,
                "final_agent": result.final_agent_name,
                "handoffs_count": result.handoffs_count,
                "total_fuel_consumed": result.total_fuel_consumed,
                "traces": traces_data,
            }
        )


def create_default_coordinator_module() -> AgentCoordinatorModule:
    """Create a default offline coordinator module registered for CLI invocation."""

    @tool(description="Look up factual support documentation.")
    def search_kb(query: str) -> str:
        return f"KB match for '{query}': Issue resolved in knowledge base article 402."

    provider = StreamingFakeProvider(
        canned_responses=[
            "Support coordinator: Verified request and found resolution in KB article 402."
        ]
    )

    support_agent = AgentDefinition(
        name="support_agent",
        instructions="You are a tier-1 customer support agent.",
        model=provider,
        tools=[search_kb],
    )

    coordinator = AgentCoordinator(
        agents=[support_agent],
        max_handoff_depth=3,
        total_fuel=10,
    )

    return AgentCoordinatorModule(
        coordinator=coordinator,
        module_id="nexura.agents.coordinator",
    )
