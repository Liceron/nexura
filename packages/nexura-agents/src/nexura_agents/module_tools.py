# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Turn any ``BaseModule`` (MCP tool, OpenAPI operation, HTTP, SQL...) into an agent tool."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Collection, Sequence
from typing import TYPE_CHECKING, Any, Literal

from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.tools import EnabledRule, OnTimeout, SchemaTool

if TYPE_CHECKING:
    from nexura_agents.contracts.base import BaseModule
    from nexura_agents.guardrails import GuardrailFn
    from nexura_agents.run_context import RunContext

# "writes": any module whose manifest does not declare it read-only ("pure"/"read").
ApprovalPolicy = bool | Literal["writes"] | Collection[str] | Callable[[str, dict[str, Any]], bool]

_NAME = re.compile(r"[^a-zA-Z0-9_-]")


def tool_name(name: str, prefix: str | None = None) -> str:
    """A model-safe tool name (``^[a-zA-Z0-9_-]{1,64}$``), optionally prefixed."""
    clean = _NAME.sub("_", name).strip("_") or "tool"
    if prefix:
        clean = f"{_NAME.sub('_', prefix).strip('_')}_{clean}"
    return clean[:64]


def needs_approval(
    policy: ApprovalPolicy, module: BaseModule, name: str, arguments: dict[str, Any]
) -> bool:
    if isinstance(policy, bool):
        return policy
    if policy == "writes":
        return module.manifest.effect_policy not in ("pure", "read")
    if callable(policy):
        return bool(policy(name, arguments))
    return name in policy


def _write_gated(module: BaseModule, arguments: dict[str, Any]) -> bool:
    """True when the manifest classifies this call as (or possibly as) a write."""
    gate = getattr(module.manifest, "write_gate_inputs", None)
    return bool(gate(arguments)) if callable(gate) else False


def _truncate(text: str, limit: int | None) -> str:
    if limit is None or len(text) <= limit:
        return text
    return text[:limit] + f"\n[truncated: {len(text) - limit} more characters not shown]"


def module_tool(
    module: BaseModule,
    *,
    name: str | None = None,
    description: str | None = None,
    config: dict[str, Any] | None = None,
    requires_approval: ApprovalPolicy = False,
    max_result_chars: int | None = 20_000,
    result: Callable[[dict[str, Any]], Any] | None = None,
    timeout: float | None = None,
    on_timeout: OnTimeout = "tell_model",
    input_guardrails: Sequence[GuardrailFn] = (),
    output_guardrails: Sequence[GuardrailFn] = (),
    enabled: EnabledRule = True,
    context: ExecutionContext | None = None,
) -> SchemaTool:
    """Expose ``module`` to an agent.

    The model sees the module's input schema. Each call runs ``module.execute`` (which
    validates inputs again and emits module events) with an ``ExecutionContext`` tied
    to the agent run. A module failure is returned to the model as an error result
    instead of ending the run; ``max_result_chars`` keeps large results from flooding
    the context window. ``timeout``, ``on_timeout``, ``input_guardrails``,
    ``output_guardrails`` and ``enabled`` work as on ``@tool``.

    ``context`` ties every call to an enclosing module run (an agent node inside a
    workflow): each call's ``ExecutionContext`` takes its workspace, principal, run id,
    deadline, cancellation, budget, event sink, artifact store and credential resolver.
    Its approval is never inherited: only a call the approval gate let through is granted.
    """
    manifest = module.manifest
    final_name = name or tool_name(manifest.module_id.split(".")[-1])
    shape = result or (lambda outputs: outputs)

    async def call(ctx: RunContext[Any], arguments: dict[str, Any]) -> str:
        # The runner only calls this after the approval gate below let the call through
        # (a human approved it), so a gated write carries that decision into the module.
        if context is None:
            exec_ctx = ExecutionContext(
                run_id=ctx.run_id,
                node_id=f"{ctx.agent_name}:{final_name}",
                attributes={"tool_call_id": ctx.tool_call_id or ""},
                approval_granted=_write_gated(module, arguments),
            )
        else:
            parent = context
            exec_ctx = ExecutionContext(
                workspace_id=parent.workspace_id,
                principal_id=parent.principal_id,
                run_id=parent.run_id,
                node_id=f"{parent.node_id}/{ctx.agent_name}:{final_name}",
                attempt_id=parent.attempt_id,
                deadline=parent.deadline,
                cancellation_checker=lambda: parent.is_cancelled,
                budget=parent.budget,
                event_sink=parent.event_sink,
                artifact_store=parent.artifact_store,
                credential_resolver=parent.credential_resolver,
                attributes={
                    **parent.attributes,
                    "tool_call_id": ctx.tool_call_id or "",
                    "agent_run_id": ctx.run_id,
                },
                approval_granted=_write_gated(module, arguments),
            )
        outcome = await module.execute(exec_ctx, arguments, dict(config or {}))
        if outcome.status != "success":
            payload: Any = {
                "error": outcome.error_code or "MODULE_ERROR",
                "message": outcome.error_message or "The tool reported an error.",
            }
        else:
            payload = shape(outcome.outputs)
        if outcome.usage is not None:
            ctx.add_usage(outcome.usage)
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return _truncate(text, max_result_chars)

    return SchemaTool(
        final_name,
        manifest.input_schema,
        call,
        description=description or manifest.description or manifest.title,
        requires_approval=lambda ctx, args: (
            needs_approval(requires_approval, module, final_name, args)
            or _write_gated(module, args)
        ),
        timeout=timeout,
        on_timeout=on_timeout,
        input_guardrails=input_guardrails,
        output_guardrails=output_guardrails,
        enabled=enabled,
    )
