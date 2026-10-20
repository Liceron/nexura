# SPDX-License-Identifier: MIT
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

from nexura_agents.module_tools import ApprovalPolicy, module_tool, tool_name
from nexura_agents.openapi.module import import_openapi_operations
from nexura_agents.tools import EnabledRule, OnTimeout, SchemaTool

if TYPE_CHECKING:
    from nexura_agents.guardrails import GuardrailFn


def _http_result(outputs: dict[str, Any]) -> dict[str, Any]:
    # Status and body are what the model needs; headers and timing stay in module events.
    return {"status_code": outputs.get("status_code"), "body": outputs.get("body")}


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
    """
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
            )
        )
    return tools
