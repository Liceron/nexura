"""Regression tests for MCP server hardening: auth headers, bad params, size, sessions."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from nexura_agents import AgentMCPServer, tool
from nexura_agents.mcp.server import McpServerSession


@tool
def echo(text: str) -> str:
    """Echo."""
    return text


def _client(app: Any) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://t")


_INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}


async def test_non_ascii_authorization_header_is_401() -> None:
    app = AgentMCPServer("d", tools=[echo]).http_app(token="s3cret")
    async with _client(app) as c:
        r = await c.post(
            "/mcp", headers={b"authorization": "Bearer café".encode("latin-1")}, json={}
        )
        assert r.status_code == 401
        r = await c.post("/mcp", headers={b"authorization": b"Bearer \xff\xfe"}, json={})
        assert r.status_code == 401


async def test_tools_call_with_bad_params_returns_invalid_params() -> None:
    app = AgentMCPServer("d", tools=[echo]).http_app()
    async with _client(app) as c:
        sid = (await c.post("/mcp", json=_INIT)).headers["mcp-session-id"]
        for params in ("x", {"name": ["a"]}, {"name": "echo", "arguments": "x"}):
            r = await c.post(
                "/mcp",
                headers={"mcp-session-id": sid},
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": params},
            )
            assert r.status_code == 200
            assert r.json()["error"]["code"] == -32602


async def test_unexpected_handler_failure_is_generic_internal_error() -> None:
    session = McpServerSession(transport=None)  # type: ignore[arg-type]

    session._tools = None  # type: ignore[assignment]  # force an internal failure
    reply = await session.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert reply is not None
    assert reply["error"]["code"] == -32603
    assert "NoneType" not in json.dumps(reply)


async def test_stdio_replies_to_bad_params_instead_of_hanging() -> None:
    server = AgentMCPServer("d", tools=[echo])
    lines = [
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": "x"}) + "\n",
        "",
    ]
    out: list[str] = []

    async def read() -> str:
        return lines.pop(0)

    async def write(line: str) -> None:
        out.append(line)

    await asyncio.wait_for(server.serve_stdio(read, write), timeout=5)
    assert json.loads(out[0])["error"]["code"] == -32602


async def test_oversized_body_is_rejected_with_413() -> None:
    app = AgentMCPServer("d", tools=[echo]).http_app(max_body_bytes=1024)
    async with _client(app) as c:
        big = b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":"' + b"a" * 4096 + b'"}}'
        assert (await c.post("/mcp", content=big)).status_code == 413
        small = await c.post("/mcp", json=_INIT)
        assert small.status_code == 200


async def test_sessions_are_capped_lru_and_expire_when_idle() -> None:
    app = AgentMCPServer("d", tools=[echo]).http_app(max_sessions=2)
    ping = {"jsonrpc": "2.0", "id": 2, "method": "ping"}
    async with _client(app) as c:
        a = (await c.post("/mcp", json=_INIT)).headers["mcp-session-id"]
        b = (await c.post("/mcp", json=_INIT)).headers["mcp-session-id"]
        assert (await c.post("/mcp", headers={"mcp-session-id": a}, json=ping)).status_code == 200
        new = (await c.post("/mcp", json=_INIT)).headers["mcp-session-id"]
        # b was least recently used, so it is evicted; a and the new one survive.
        assert (await c.post("/mcp", headers={"mcp-session-id": b}, json=ping)).status_code == 404
        for sid in (a, new):
            assert (
                await c.post("/mcp", headers={"mcp-session-id": sid}, json=ping)
            ).status_code == 200

    expiring = AgentMCPServer("d", tools=[echo]).http_app(session_ttl_seconds=0)
    async with _client(expiring) as c:
        sid = (await c.post("/mcp", json=_INIT)).headers["mcp-session-id"]
        # Longer than one Windows clock tick (~15.6 ms), so the idle time is never read as 0.
        await asyncio.sleep(0.05)
        r = await c.post("/mcp", headers={"mcp-session-id": sid}, json=ping)
        assert r.status_code == 404
