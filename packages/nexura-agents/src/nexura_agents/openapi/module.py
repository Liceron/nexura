# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""OpenAPI operation module executing imported endpoints under B00/B01 contracts."""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from nexura_agents.contracts.base import BaseModule
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.manifest import ModuleManifest
from nexura_agents.contracts.result import ModuleResult
from nexura_agents.network.client import SafeHttpClient
from nexura_agents.network.policy import DestinationPolicy
from nexura_agents.network.redaction import redact_headers
from nexura_agents.openapi.errors import (
    HttpMaxResponseSizeExceededError,
    NonIdempotentRetryForbiddenError,
)
from nexura_agents.openapi.parser import (
    OpenAPIOperationSpec,
    OpenAPISpecParser,
)

# Control inputs consumed by the module itself; never forwarded as request body fields.
_INTERNAL_INPUT_KEYS = {"idempotency_key", "retries"}


class OpenAPIOperationModule(BaseModule):
    """B00/B01 module wrapping an individual OpenAPI operation."""

    def __init__(
        self,
        op_spec: OpenAPIOperationSpec,
        *,
        base_url: str = "https://api.example.com",
        transport: httpx.AsyncBaseTransport | None = None,
        prefix: str = "api",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.op_spec = op_spec
        self.base_url = base_url.rstrip("/")
        self.transport = transport
        self.prefix = prefix
        # Operator-supplied headers (e.g. Authorization) sent on every call; never shown
        # to the model and redacted from results.
        self.default_headers = dict(headers or {})

        clean_op_id = re.sub(r"[^a-z0-9_-]", "_", op_spec.operation_id.lower()).strip("_")
        clean_prefix = re.sub(r"[^a-z0-9_-]", "_", prefix.lower()).strip("_")
        mod_id = f"openapi.{clean_prefix}.{clean_op_id}"
        self._manifest = ModuleManifest(
            schema_version=1,
            module_id=mod_id,
            version="1.0.0",
            kind="integration",
            title=op_spec.summary,
            description=op_spec.description,
            input_schema=op_spec.input_schema,
            output_schema=op_spec.output_schema,
            config_schema={
                "type": "object",
                "properties": {
                    "base_url": {"type": "string"},
                    "allow_loopback": {"type": "boolean"},
                    "allow_private_ranges": {"type": "boolean"},
                    "allowed_hosts": {"type": "array"},
                    "retries": {"type": "integer"},
                },
            },
            named_ports=[],
            capabilities={"streaming": False, "network": True, "idempotent": op_spec.is_idempotent},
            effect_policy=op_spec.effect_policy,
            always_write=op_spec.method.upper() not in ("GET", "HEAD"),
            dependency_extra=None,
            min_sdk_version="0.3.0",
            timeout_seconds=30.0,
            output_limit_bytes=5_000_000,
            required_credential_types=[],
            supported_modalities=["text"],
            supported_locales=["all"],
            source_reference=f"openapi:{op_spec.method.upper()}:{op_spec.path}",
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
        if self.manifest.always_write:
            context.require_write_approval(
                f"OpenAPI {self.op_spec.method.upper()} {self.op_spec.path}"
            )
        cfg = config or {}
        effective_base_url = (cfg.get("base_url") or self.base_url).rstrip("/")

        # 1. Substitute path variables
        formatted_path = self.op_spec.path
        for p_name in self.op_spec.path_params:
            val = inputs.get(p_name, "")
            formatted_path = formatted_path.replace(f"{{{p_name}}}", quote(str(val), safe=""))

        target_url = f"{effective_base_url}{formatted_path}"

        # 2. Extract query and header parameters
        query_params: dict[str, Any] = {}
        for q_name in self.op_spec.query_params:
            if q_name in inputs and inputs[q_name] is not None:
                query_params[q_name] = inputs[q_name]

        headers: dict[str, str] = dict(self.default_headers)
        for h_name in self.op_spec.header_params:
            if h_name in inputs and inputs[h_name] is not None:
                headers[h_name] = str(inputs[h_name])

        # 3. Extract request body
        json_body = inputs.get("body")
        if json_body is None and self.op_spec.body_schema:
            # If body not under 'body' key, check if inputs contains body fields directly
            non_param_keys = (
                set(inputs.keys())
                - set(self.op_spec.path_params)
                - set(self.op_spec.query_params)
                - set(self.op_spec.header_params)
                - _INTERNAL_INPUT_KEYS
            )
            if non_param_keys:
                json_body = {k: inputs[k] for k in non_param_keys}

        # 4. Check Non-Idempotent Retry Policy
        retries = int(cfg.get("retries") or inputs.get("retries") or 0)
        idempotency_key = headers.get("Idempotency-Key") or inputs.get("idempotency_key")
        if idempotency_key:
            headers["Idempotency-Key"] = str(idempotency_key)

        if retries > 0 and not self.op_spec.is_idempotent and not idempotency_key:
            raise NonIdempotentRetryForbiddenError(self.op_spec.method, target_url)

        # 5. Build DestinationPolicy
        # Operator-configured hosts bypass the policy. The spec's own host is NOT trusted: it
        # goes through the same checks as any destination, at check time and again when the
        # pinned connection resolves it, so a name that rebinds to loopback or a private
        # range is refused.
        allowed_hosts = list(cfg.get("allowed_hosts") or [])
        if self.transport is not None:
            # An injected transport (tests) never opens a socket, so there is nothing to
            # rebind; an offline spec host that does not resolve is let through.
            base_host = urlsplit(effective_base_url).hostname
            if (
                base_host
                and base_host not in allowed_hosts
                and not await asyncio.to_thread(DestinationPolicy.resolve, base_host, 443)
            ):
                allowed_hosts.append(base_host)

        policy = DestinationPolicy(
            allow_loopback=cfg.get("allow_loopback", False),
            allow_private_ranges=cfg.get("allow_private_ranges", False),
            allowed_hosts=allowed_hosts,
        )

        max_size = cfg.get("max_response_size_bytes") or 5_000_000

        # 6. Execute request via SafeHttpClient
        client = SafeHttpClient(policy=policy, transport=self.transport)
        start_time = time.monotonic()
        try:
            resp = await client.request(
                method=self.op_spec.method,
                url=target_url,
                headers=headers,
                params=query_params or None,
                json=json_body,
            )
        finally:
            await client.close()

        elapsed = round(time.monotonic() - start_time, 4)

        # 7. Check max response size
        content_len = len(resp.content)
        if content_len > max_size:
            raise HttpMaxResponseSizeExceededError(content_len, max_size)

        # 8. Parse response body
        content_type = resp.headers.get("content-type", "")
        if "application/json" in content_type:
            try:
                body_val = resp.json()
            except Exception:
                body_val = resp.text
        else:
            body_val = resp.text

        safe_resp_headers = redact_headers(resp.headers)

        return ModuleResult.success(
            outputs={
                "status_code": resp.status_code,
                "body": body_val,
                "headers": safe_resp_headers,
                "elapsed_seconds": elapsed,
            }
        )


def import_openapi_operations(
    spec: dict[str, Any] | str,
    *,
    base_url: str = "https://api.example.com",
    transport: httpx.AsyncBaseTransport | None = None,
    prefix: str = "api",
    headers: dict[str, str] | None = None,
) -> list[OpenAPIOperationModule]:
    """Parse an OpenAPI 3.x spec and return configured OpenAPIOperationModule instances."""
    parser = OpenAPISpecParser(spec)
    effective_base_url = base_url
    if parser.servers and base_url == "https://api.example.com":
        effective_base_url = parser.servers[0]

    op_specs = parser.parse_operations()
    modules: list[OpenAPIOperationModule] = []
    for op in op_specs:
        mod = OpenAPIOperationModule(
            op,
            base_url=effective_base_url,
            transport=transport,
            prefix=prefix,
            headers=headers,
        )
        modules.append(mod)

    return modules
