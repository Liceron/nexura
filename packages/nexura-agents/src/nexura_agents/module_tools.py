# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Turn any ``BaseModule`` (MCP tool, OpenAPI operation, HTTP, SQL...) into an agent tool."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Collection, Iterable, Sequence
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Literal

from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.tools import EffectName, EnabledRule, OnTimeout, SchemaTool, Tool

if TYPE_CHECKING:
    from nexura_agents.contracts.base import BaseModule
    from nexura_agents.guardrails import GuardrailFn
    from nexura_agents.run_context import RunContext

# "writes": any module whose manifest does not declare it read-only ("pure"/"read").
ApprovalPolicy = bool | Literal["writes"] | Collection[str] | Callable[[str, dict[str, Any]], bool]

_NAME = re.compile(r"[^a-zA-Z0-9_-]")

# The agent run's tool context while ``module_tool`` executes its module (so a wrapped
# tool's approval rule sees the same ``deps`` the runner's gate saw).
_CALLER: ContextVar[RunContext[Any] | None] = ContextVar("nexura_module_tool_caller", default=None)


def calling_run_context() -> RunContext[Any] | None:
    """The ``RunContext`` of the agent tool call running the current module through
    ``module_tool``, or ``None`` (a workflow node, a direct call)."""
    return _CALLER.get()


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


def module_effect(module: BaseModule) -> EffectName:
    """The effect class a module tool runs under, from what its manifest declares.

    ``effect_policy`` maps as everywhere else (``pure``/``read`` to ``pure``,
    ``idempotent_write`` to ``idempotent``, ``non_idempotent_write`` to ``non_idempotent``),
    but only when the manifest sets it explicitly and does not contradict it: an unset
    policy (the manifest default), an unknown spelling, or a ``pure``/``read`` policy on a
    manifest that also declares writes stays ``non_idempotent``, as before. MCP tools are
    ``read`` only when the operator lists them in ``MCPServer(read_only=...)``; the
    server's own hints never change this. OpenAPI operations always stay
    ``non_idempotent`` here: their policy comes from the spec's HTTP method and
    ``x-idempotent``, a third-party claim (operators declare classes with
    ``openapi_tools(read_only=..., idempotent=...)`` or ``module_tool(effect=...)``).

    A module that wraps a ``Tool`` (``module.tool``, as ``tool_to_module`` does) runs under
    the stricter of the manifest's class and the tool's own class, so a wrapper's policy can
    never make an undeclared tool repeatable after a crash.
    """
    declared = _manifest_effect(module)
    wrapped = getattr(module, "tool", None)
    if isinstance(wrapped, Tool):
        own: EffectName = wrapped.effect_class().value
        if own == "reconcilable":
            own = "non_idempotent"  # a module tool cannot run the tool's reconcile
        return max(declared, own, key=_STRICTNESS.index)
    return declared


# Least to most strict (recovery repeats freely ... never repeats silently).
_STRICTNESS: tuple[EffectName, ...] = ("pure", "idempotent", "reconcilable", "non_idempotent")


def _manifest_effect(module: BaseModule) -> EffectName:
    """The class the manifest itself explicitly declares (see ``module_effect``)."""
    from nexura_agents.contracts.execution import effect_class_from_policy

    manifest = module.manifest
    reference = getattr(manifest, "source_reference", None)
    if isinstance(reference, str) and reference.startswith("openapi:"):
        return "non_idempotent"  # the spec's method is the API's claim, not the operator's
    fields_set = getattr(manifest, "model_fields_set", None)
    if fields_set is not None and "effect_policy" not in fields_set:
        return "non_idempotent"
    try:
        effect = effect_class_from_policy(getattr(manifest, "effect_policy", None))
    except ValueError:
        return "non_idempotent"
    writes = getattr(manifest, "always_write", False) or getattr(manifest, "write_operations", None)
    if writes and effect.value == "pure":
        return "non_idempotent"
    return effect.value


def module_origin(module: BaseModule) -> str:
    """``mcp:<server>:<tool>`` / ``openapi:<METHOD>:<path>`` from the manifest's
    ``source_reference``, otherwise ``module:<module_id>``."""
    manifest = module.manifest
    reference = getattr(manifest, "source_reference", None)
    if isinstance(reference, str) and reference.startswith(("mcp:", "openapi:")):
        return reference
    return f"module:{manifest.module_id}"


def _wrapped_tool_gated(
    module: BaseModule, ctx: RunContext[Any], arguments: dict[str, Any]
) -> bool:
    """True when the module wraps a ``Tool`` (``module.tool``, as ``tool_to_module`` does)
    whose own ``requires_approval`` flag or rule requires approval for these arguments."""
    wrapped = getattr(module, "tool", None)
    return isinstance(wrapped, Tool) and wrapped.needs_approval(ctx, arguments)


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
    effect: EffectName | None = None,
    tags: Iterable[str] = (),
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

    The tool keeps the module's identity: ``origin`` is ``mcp:<server>:<tool>``,
    ``openapi:<METHOD>:<path>`` or ``module:<module_id>``, the tags include
    ``module:<module_id>`` plus ``tags``, and its effect class comes from the manifest's
    ``effect_policy`` (see ``module_effect``). Pass ``effect=`` to override it.

    A call needs approval when any gate says so: ``requires_approval``, the manifest's
    write gate, or, for a module that wraps a ``Tool`` (``tool_to_module``), that tool's
    own ``requires_approval``. Wrapping a tool never removes its approval gate.
    """
    manifest = module.manifest
    final_name = name or tool_name(manifest.module_id.split(".")[-1])
    shape = result or (lambda outputs: outputs)

    async def call(ctx: RunContext[Any], arguments: dict[str, Any]) -> str:
        # The runner only calls this after the approval gate below let the call through
        # (a human approved it), so a gated write, or a call the wrapped tool's own gate
        # required approval for, carries that decision into the module.
        granted = _write_gated(module, arguments) or _wrapped_tool_gated(module, ctx, arguments)
        if context is None:
            exec_ctx = ExecutionContext(
                run_id=ctx.run_id,
                node_id=f"{ctx.agent_name}:{final_name}",
                attributes={"tool_call_id": ctx.tool_call_id or ""},
                approval_granted=granted,
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
                approval_granted=granted,
            )
        token = _CALLER.set(ctx)
        try:
            outcome = await module.execute(exec_ctx, arguments, dict(config or {}))
        finally:
            _CALLER.reset(token)
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
            or _wrapped_tool_gated(module, ctx, args)
        ),
        timeout=timeout,
        on_timeout=on_timeout,
        input_guardrails=input_guardrails,
        output_guardrails=output_guardrails,
        enabled=enabled,
        effect=effect if effect is not None else module_effect(module),
        origin=module_origin(module),
        tags={f"module:{manifest.module_id}", *tags},
    )
