# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Bounded, secure HTTP request module conforming to B00/B01 contracts."""

from __future__ import annotations

import asyncio
import ipaddress
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

import httpx
from nexura_agents.contracts.base import BaseModule
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.manifest import ModuleManifest
from nexura_agents.contracts.result import ModuleResult
from nexura_agents.network.client import SafeHttpClient
from nexura_agents.network.policy import DestinationPolicy
from nexura_agents.network.redaction import redact_headers as redact_headers

from nexura_runtime.integrations.http.errors import (
    HttpMaxResponseSizeExceededError,
    NonIdempotentRetryForbiddenError,
)


def _default_http_mock_transport() -> httpx.AsyncBaseTransport:
    """Offline mock transport for safe keyless module execution."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "status": "ok",
                "service": "offline_http_gateway",
                "method": request.method,
                "url": str(request.url),
                "path": request.url.path,
            },
        )

    return httpx.MockTransport(handler)


#: Most retries one call may ask for (``retries`` input).
MAX_HTTP_RETRIES = 5
#: Responses worth sending again: rate limited or a server/gateway failure.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_RETRY_ERRORS = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)
_KEYED_METHODS = ("POST", "PATCH")


def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
    """Seconds before retry ``attempt`` (0-based): a numeric Retry-After, else backoff."""
    if response is not None:
        try:
            return max(0.0, min(float(response.headers.get("retry-after", "")), 10.0))
        except ValueError:
            pass
    return float(min(0.25 * (2**attempt), 4.0))


class HttpRequestModule(BaseModule):
    """B00/B01 module for issuing policy-bounded, SSRF-safe HTTP requests.

    ``retries`` (at most ``MAX_HTTP_RETRIES``) sends the request again after a transport
    error or a 429/5xx answer. POST and PATCH are retried only with an idempotency key: the
    ``idempotency_key`` input, an ``Idempotency-Key`` header, or (for a write inside a
    workflow step) the step's key from ``ExecutionContext.idempotency_key()``, which is the
    same on every try and after a crash recovery. Without any key a POST/PATCH retry is
    refused (``NonIdempotentRetryForbiddenError``). Writes send the key as
    ``Idempotency-Key``.
    """

    manifest = ModuleManifest(
        schema_version=1,
        module_id="nexura.http.request",
        version="1.0.0",
        kind="integration",
        title="Safe HTTP Request",
        description=(
            "Policy-bounded, SSRF-safe HTTP client with redirect inspection and sensitive header "
            "redaction."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "method": {
                    "type": "string",
                    "enum": ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
                },
                "url": {"type": "string"},
                "headers": {"type": "object"},
                "params": {"type": "object"},
                "json": {},
                "data": {},
                "timeout": {"type": "number"},
                "idempotency_key": {"type": "string"},
                "retries": {"type": "integer"},
            },
            "required": ["method", "url"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "status_code": {"type": "integer"},
                "headers": {"type": "object"},
                "body": {},
                "elapsed_seconds": {"type": "number"},
            },
        },
        config_schema={
            "type": "object",
            "properties": {
                "allowed_hosts": {"type": "array"},
                "allow_loopback": {"type": "boolean"},
                "allow_private_ranges": {"type": "boolean"},
                "max_response_size_bytes": {"type": "integer"},
            },
        },
        named_ports=[],
        capabilities={"streaming": False, "network": True},
        effect_policy="non_idempotent_write",
        write_operations=["POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
        write_operation_key="method",
        dependency_extra=None,
        min_sdk_version="0.3.0",
        timeout_seconds=30.0,
        output_limit_bytes=5_000_000,
        required_credential_types=[],
        supported_modalities=["text"],
        supported_locales=["all"],
        source_reference="src/nexura_runtime/integrations/http/",
        verification_status="mock_verified",
    )

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        sleep: Callable[[float], Awaitable[Any]] | None = None,
    ) -> None:
        self.transport = transport
        self._sleep = sleep or asyncio.sleep

    async def invoke(
        self,
        context: ExecutionContext,
        inputs: dict[str, Any],
        config: dict[str, Any] | None = None,
    ) -> ModuleResult:
        cfg = config or {}
        method = inputs["method"].upper()
        url = inputs["url"]
        if self.manifest.write_gate(method):
            context.require_write_approval(f"HTTP {method} {url}")
        headers = dict(inputs.get("headers") or {})
        params = inputs.get("params")
        json_data = inputs.get("json")
        data = inputs.get("data")
        timeout = float(inputs.get("timeout") or 30.0)
        retries = min(max(int(inputs.get("retries") or 0), 0), MAX_HTTP_RETRIES)
        header_key = next(
            (str(v) for k, v in headers.items() if k.lower() == "idempotency-key" and v), None
        )
        # An explicit key wins; a write inside a workflow step defaults to the step's key
        # (stable across retries and crash recovery, Execution Contract v1).
        idempotency_key = inputs.get("idempotency_key") or header_key
        if not idempotency_key and self.manifest.write_gate(method):
            idempotency_key = context.idempotency_key()

        # 1. Enforce Non-Idempotent Retry Policy
        if retries > 0 and method in _KEYED_METHODS and not idempotency_key:
            raise NonIdempotentRetryForbiddenError(method, url)

        if idempotency_key and header_key is None:
            headers["Idempotency-Key"] = str(idempotency_key)

        # 2. Build DestinationPolicy from config
        allowed_hosts = list(cfg.get("allowed_hosts") or [])
        if self.transport is not None:
            parsed_host = urlsplit(url).hostname
            if parsed_host:
                try:
                    ipaddress.ip_address(parsed_host)
                except ValueError:
                    if parsed_host not in allowed_hosts:
                        allowed_hosts.append(parsed_host)

        policy = DestinationPolicy(
            allow_loopback=cfg.get("allow_loopback", False),
            allow_private_ranges=cfg.get("allow_private_ranges", False),
            allowed_hosts=allowed_hosts,
        )

        max_size = cfg.get("max_response_size_bytes") or 5_000_000

        # 3. Dispatch request via SafeHttpClient (again on a transient failure, see retries)
        start_time = time.monotonic()
        attempt = 0
        while True:
            client = SafeHttpClient(policy=policy, transport=self.transport, timeout=timeout)
            try:
                resp = await client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    params=params,
                    json=json_data,
                    data=data,
                )
            except _RETRY_ERRORS:
                if attempt >= retries:
                    raise
                await self._sleep(_retry_delay(None, attempt))
                attempt += 1
                continue
            finally:
                await client.close()
            if resp.status_code in RETRY_STATUSES and attempt < retries:
                await self._sleep(_retry_delay(resp, attempt))
                attempt += 1
                continue
            break

        elapsed = round(time.monotonic() - start_time, 4)

        # 4. Check response size bound
        content_len = len(resp.content)
        if content_len > max_size:
            raise HttpMaxResponseSizeExceededError(content_len, max_size)

        # 5. Parse response body
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
                "headers": safe_resp_headers,
                "body": body_val,
                "elapsed_seconds": elapsed,
            },
            metadata={"attempts": attempt + 1},
        )
