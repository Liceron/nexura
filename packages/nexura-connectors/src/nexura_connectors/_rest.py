# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Base class for REST connectors built on the bounded ConnectorHttp helper."""

from __future__ import annotations

from typing import Any, ClassVar

from nexura_connectors._core import (
    Call,
    ConnectorModule,
    HttpResult,
    Op,
    bearer,
)


class RestConnector(ConnectorModule):
    """Connector whose operations are ``op_<name>`` coroutines calling a JSON REST API."""

    EXTRA_HEADERS: ClassVar[dict[str, str]] = {}

    def auth_headers(self, secret: str) -> dict[str, str]:
        """Headers carrying the credential (bearer token by default)."""
        return bearer(secret)

    async def api(
        self,
        call: Call,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        write: bool = False,
        url: str | None = None,
    ) -> HttpResult:
        """Call ``path`` under the base URL (or an absolute same-origin ``url``)."""
        target = url or call.base_url(self.DEFAULT_BASE_URL) + path
        merged = {
            "Accept": "application/json",
            **self.EXTRA_HEADERS,
            **self.auth_headers(call.secret),
            **(headers or {}),
        }
        return await call.http.request(
            method,
            target,
            headers=merged,
            params=params,
            json=json,
            content=content,
            write=write,
        )

    async def run(self, op: Op, params: Any, call: Call) -> dict[str, Any]:
        """Dispatch to the ``op_<name>`` coroutine."""
        handler = getattr(self, f"op_{op.name}")
        result: dict[str, Any] = await handler(params, call)
        return result
