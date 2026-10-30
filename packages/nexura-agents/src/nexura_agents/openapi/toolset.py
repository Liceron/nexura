# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Give an agent tools for every operation in an OpenAPI 3 spec.

tools = openapi_tools(spec, base_url="https://api.example.com",
                      headers={"Authorization": f"Bearer {token}"},
                      requires_approval="writes")
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import TYPE_CHECKING, Any

import httpx

from nexura_agents.errors import SDKValidationError
from nexura_agents.module_tools import ApprovalPolicy, module_tool, tool_name
from nexura_agents.openapi.module import OpenAPIOperationModule, import_openapi_operations
from nexura_agents.tools import EffectName, EnabledRule, OnTimeout, SchemaTool

if TYPE_CHECKING:
    from nexura_agents.guardrails import GuardrailFn


def _http_result(outputs: dict[str, Any]) -> dict[str, Any]:
    # Status and body are what the model needs; headers and timing stay in module events.
    return {"status_code": outputs.get("status_code"), "body": outputs.get("body")}


def _operation_tags(module: OpenAPIOperationModule, toolset: str) -> tuple[str, ...]:
    """``toolset:<name>``, ``openapi.method:<METHOD>`` and, when the spec's method or its
    ``x-idempotent`` claims it, ``openapi.hint:idempotent=true``. Metadata only."""
    spec = module.op_spec
    tags = [f"toolset:{toolset}", f"openapi.method:{spec.method.upper()}"]
    if spec.is_idempotent:
        tags.append("openapi.hint:idempotent=true")
    return tuple(tags)


def _declared_names(label: str, names: Any) -> frozenset[str]:
    if isinstance(names, (str, bytes)) or not all(isinstance(n, str) for n in names):
        raise SDKValidationError(f"openapi_tools: {label} must be a collection of operationIds.")
    return frozenset(names)


def _declared_effect(
    operation_id: str, pure_ops: frozenset[str], repeat_ops: frozenset[str]
) -> EffectName:
    if operation_id in pure_ops:
        return "pure"
    if operation_id in repeat_ops:
        return "idempotent"
    return "non_idempotent"


def openapi_tools(
    spec: dict[str, Any] | str,
    *,
    base_url: str | None = None,
    headers: dict[str, str] | None = None,
    prefix: str | None = None,
    include: Collection[str] | None = None,
    exclude: Collection[str] = (),
    requires_approval: ApprovalPolicy = False,
    allow_loopback: bool = False,
    allowed_hosts: Collection[str] = (),
    transport: httpx.AsyncBaseTransport | None = None,
    max_result_chars: int | None = 20_000,
    timeout: float | None = None,
    on_timeout: OnTimeout = "tell_model",
    input_guardrails: Sequence[GuardrailFn] = (),
    output_guardrails: Sequence[GuardrailFn] = (),
    enabled: EnabledRule = True,
    toolset: str | None = None,
    read_only: Collection[str] = (),
    idempotent: Collection[str] = (),
) -> list[SchemaTool]:
    """One agent tool per operation, named by ``operationId``.

    ``base_url`` defaults to the spec's first server. ``headers`` (for example an
    ``Authorization`` header) are added to every request and never shown to the model.
    ``requires_approval="writes"`` pauses before any non-GET/HEAD call. Requests go
    through the SDK's safe HTTP client: only the API's own host is allowed, and
    loopback/private addresses are refused unless ``allow_loopback`` is set.
    ``allowed_hosts`` trusts specific extra hosts (e.g. one private address) without
    opening every private range. ``timeout``, ``on_timeout``, ``input_guardrails``,
    ``output_guardrails`` and ``enabled`` apply to every generated tool, as on ``@tool``
    (``ctx.tool_name`` tells a guardrail or rule which operation is being called).
    Each tool's ``origin`` is ``openapi:<METHOD>:<path>`` and it carries the tag
    ``toolset:<toolset>`` (default: ``prefix``, else ``openapi``), so a policy can match
    every operation of this API (``ctx.tool``), plus ``openapi.method:<METHOD>`` and,
    when the method or ``x-idempotent`` claims it, ``openapi.hint:idempotent=true``.

    Every tool is ``non_idempotent`` for durable recovery (a crash during the call pauses
    the run) unless you declare otherwise: ``read_only`` lists operationIds you vouch have
    no side effects (``pure``: repeated after a crash) and ``idempotent`` those that are
    safe to repeat (``idempotent``). The spec's HTTP method and ``x-idempotent`` are the
    API's own claims and never change the class. Unknown operationIds, or one listed in
    both, raise ``SDKValidationError``. These declarations do not change approval:
    ``requires_approval="writes"`` still pauses before every non-GET/HEAD call.
    """
    pure_ops = _declared_names("read_only", read_only)
    repeat_ops = _declared_names("idempotent", idempotent)
    if pure_ops & repeat_ops:
        raise SDKValidationError(
            "openapi_tools: an operation cannot be both read_only and idempotent: "
            f"{', '.join(sorted(pure_ops & repeat_ops))}."
        )
    kwargs: dict[str, Any] = {"transport": transport, "headers": headers}
    if base_url is not None:
        kwargs["base_url"] = base_url
    if prefix is not None:
        kwargs["prefix"] = prefix
    modules = import_openapi_operations(spec, **kwargs)
    config: dict[str, Any] = (
        {"allow_loopback": True, "allow_private_ranges": True} if allow_loopback else {}
    )
    if allowed_hosts:
        config["allowed_hosts"] = list(allowed_hosts)

    unknown = (pure_ops | repeat_ops) - {m.op_spec.operation_id for m in modules}
    if unknown:
        raise SDKValidationError(
            f"openapi_tools: no operation with operationId {', '.join(sorted(unknown))}."
        )

    tools: list[SchemaTool] = []
    for module in modules:
        operation_id = module.op_spec.operation_id
        if include is not None and operation_id not in include:
            continue
        if operation_id in exclude:
            continue
        tools.append(
            module_tool(
                module,
                name=tool_name(operation_id, prefix),
                description=module.op_spec.description or module.op_spec.summary,
                config=config,
                requires_approval=requires_approval,
                max_result_chars=max_result_chars,
                result=_http_result,
                timeout=timeout,
                on_timeout=on_timeout,
                input_guardrails=input_guardrails,
                output_guardrails=output_guardrails,
                enabled=enabled,
                effect=_declared_effect(operation_id, pure_ops, repeat_ops),
                tags=_operation_tags(module, toolset or prefix or "openapi"),
            )
        )
    return tools
