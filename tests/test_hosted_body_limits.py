# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Binary OTLP bypasses only JSON scanning, while normal API protections remain."""

from __future__ import annotations

from typing import Any

import pytest

from nexura_runtime.service.api.middleware import MAX_JSON_DEPTH, BodySizeLimitMiddleware


@pytest.mark.parametrize("path", ["/v1/traces", "/api/v1/sdk-traces/otlp", "/api/v1/workflows"])
@pytest.mark.parametrize(
    "content_type,encoding",
    [(b"application/x-protobuf", b"identity"), (b"application/json", b"gzip")],
)
async def test_otlp_body_scanner_is_limited_to_ingestion_paths(
    path: str,
    content_type: bytes,
    encoding: bytes,
) -> None:
    data = b"[" * (MAX_JSON_DEPTH + 1)
    sent = []

    async def receive() -> Any:
        return {"type": "http.request", "body": data, "more_body": False}

    async def send(message: Any) -> None:
        sent.append(message)

    async def app(scope: Any, recv: Any, transmit: Any) -> None:
        await recv()
        await transmit({"type": "http.response.start", "status": 200, "headers": []})
        await transmit({"type": "http.response.body", "body": b""})

    middleware = BodySizeLimitMiddleware(app)
    await middleware(
        {
            "type": "http",
            "path": path,
            "headers": [(b"content-type", content_type), (b"content-encoding", encoding)],
        },
        receive,
        send,
    )
    assert sent[0]["status"] == (422 if path.endswith("workflows") else 200)
