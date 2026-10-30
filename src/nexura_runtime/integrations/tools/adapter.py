# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Adapter extending @tool definitions into the B00/B01 module contract."""

from __future__ import annotations

from typing import Any

from nexura_agents.contracts.base import BaseModule
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.manifest import EffectPolicy, ModuleManifest
from nexura_agents.contracts.result import ModuleResult
from nexura_agents.tools import Tool


def tool_effect_policy(tool: Tool) -> EffectPolicy:
    """The manifest policy for a tool's own effect class: ``pure`` -> ``pure``,
    ``idempotent`` -> ``idempotent_write``, anything else (an undeclared tool, a
    reconcilable one whose ``reconcile`` a module cannot run) -> ``non_idempotent_write``."""
    effect = tool.effect_class().value
    if effect == "pure":
        return "pure"
    if effect == "idempotent":
        return "idempotent_write"
    return "non_idempotent_write"


class ToolModule(BaseModule):
    """B00/B01 module wrapping a typed @tool or Tool instance.

    ``effect_policy`` defaults to the tool's own declared class (``tool_effect_policy``);
    an undeclared ``@tool`` is ``non_idempotent_write``. An explicit value is recorded as
    given, but ``module_tool`` still applies the stricter of it and the tool's class.

    The tool's own ``requires_approval`` (flag or rule) applies wherever the module runs
    (P39 G2): a call it gates runs only with an approval a trusted caller granted
    (``ExecutionContext.approval_granted``). As a workflow node without one, the node
    pauses for approval (``request_approval``, bound to the arguments) and runs once a
    person approves; anywhere else the call is refused with ``APPROVAL_REQUIRED``.
    """

    def __init__(
        self,
        tool: Tool,
        *,
        effect_policy: EffectPolicy | None = None,
        module_id: str | None = None,
        version: str = "1.0.0",
    ) -> None:
        self.tool = tool
        if effect_policy is None:
            effect_policy = tool_effect_policy(tool)
        mod_id = module_id or f"nexura.tools.{tool.name}"

        # Clean parameter schema for module contract input validation
        input_schema = tool.args_schema.model_json_schema()
        input_schema.pop("title", None)

        self._manifest = ModuleManifest(
            schema_version=1,
            module_id=mod_id,
            version=version,
            kind="tool",
            title=f"Tool: {tool.name}",
            description=tool.description,
            input_schema=input_schema,
            output_schema={
                "type": "object",
                "properties": {
                    "result": {},
                },
            },
            config_schema={"type": "object"},
            named_ports=[],
            capabilities={"streaming": False, "network": False},
            effect_policy=effect_policy,
            dependency_extra=None,
            min_sdk_version="0.3.0",
            timeout_seconds=30.0,
            output_limit_bytes=2_000_000,
            required_credential_types=[],
            supported_modalities=["text"],
            supported_locales=["all"],
            source_reference=f"tool:{tool.name}",
            verification_status="mock_verified",
        )

    @property
    def manifest(self) -> ModuleManifest:
        """Module manifest defining metadata, schemas, and policies."""
        return self._manifest

    async def invoke(
        self,
        context: ExecutionContext,
        inputs: dict[str, Any],
        config: dict[str, Any] | None = None,
    ) -> ModuleResult:
        # Schema validation has already succeeded in BaseModule.execute()
        # prior to calling this method, guaranteeing zero tool effects on invalid args.
        _check_tool_approval(self.tool, context, inputs)
        result = await self.tool.arun(**inputs)
        return ModuleResult.success(outputs={"result": result})


def _check_tool_approval(tool: Tool, context: ExecutionContext, inputs: dict[str, Any]) -> None:
    """Refuse, or pause the workflow node, when the tool requires approval for ``inputs``
    and no trusted caller granted it (fail closed)."""
    from nexura_agents.contracts.canonical import canonical_digest
    from nexura_agents.module_tools import calling_run_context
    from nexura_agents.run_context import RunContext

    from nexura_runtime import workflow

    request = {"tool_requires_approval": tool.name, "arguments": inputs}
    if context.approval_granted:
        granted = workflow.approved_request()
        if granted is None or granted.get("tool_requires_approval") is None:
            return  # the node itself was approved (or a runner approved the call)
        if granted.get("tool_requires_approval") == tool.name and _same_arguments(
            granted.get("arguments"), inputs, canonical_digest
        ):
            return  # this tool's own approval request, for these exact arguments
    # Through module_tool: the agent run's context (its deps), as the runner's gate saw it.
    # As a workflow node there are no deps (a rule reading ctx.deps sees None).
    run_ctx: RunContext[Any] = calling_run_context() or RunContext(
        deps=None, run_id=context.run_id, agent_name=context.node_id, tool_name=tool.name
    )
    if not tool.needs_approval(run_ctx, dict(inputs)):
        return
    if workflow._INTERRUPT_STATE.get() is not None:
        # A workflow node: pause on the approval path; the node runs again once approved.
        workflow.request_approval(request)
    from nexura_agents.contracts.errors import ContractError

    raise ContractError(
        f"Tool '{tool.name}' requires human approval for this call, and none was granted; "
        "nothing was executed.",
        code="APPROVAL_REQUIRED",
    )


def _same_arguments(approved: Any, inputs: dict[str, Any], digest: Any) -> bool:
    """True when the approved arguments and ``inputs`` have the same canonical digest
    (``1``, ``1.0`` and ``True`` differ; a list and a tuple with the same items match)."""
    try:
        return bool(digest(approved) == digest(inputs))
    except (TypeError, ValueError):
        return False


def tool_to_module(
    tool: Tool,
    *,
    effect_policy: EffectPolicy | None = None,
    module_id: str | None = None,
) -> ToolModule:
    """Convert an existing @tool or Tool instance into a B00/B01 BaseModule.

    The module's ``effect_policy`` defaults to the tool's own declared class (an undeclared
    tool is ``non_idempotent_write``: a durable run pauses instead of repeating it)."""
    return ToolModule(tool, effect_policy=effect_policy, module_id=module_id)
