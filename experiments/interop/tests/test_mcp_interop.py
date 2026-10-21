"""Nexura MCP against the official MCP Python SDK, both directions, offline.

* ``mcp`` 2.3.0 (dual-era: 2026-07-28 plus the handshake revisions), in this project.
* ``mcp`` 1.30.0 (handshake revisions only, newest 2025-11-25), in ``legacy/``.

stdio servers run as subprocesses; HTTP servers run with uvicorn on 127.0.0.1.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

import httpx
import httpx2
import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from nexura_agents import Agent, FakeModel, MCPServer, ToolCall
from nexura_agents.mcp.errors import McpProtocolVersionMismatchError

from tests.conftest import LEGACY_PYTHON, LEGACY_SERVER, ROOT, SERVERS

TOKEN = "interop-token"
NEXURA_STDIO = StdioServerParameters(
    command=sys.executable, args=[str(SERVERS / "nexura_mcp_server.py"), "stdio"]
)


def _text(result: Any) -> str:
    return str(result.content[0].text)


# ------------------------------------------------- official client -> Nexura server


async def _official_round_trip(client: Client, expected_version: str, mode: str) -> None:
    assert client.protocol_version == expected_version
    if mode != "2026-07-28":
        # A version-pinned official client skips server/discover, so it has no server info.
        assert client.server_info is not None and client.server_info.name == "nexura-interop"
    tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {"helper", "add", "delete_order"}
    assert tools["add"].input_schema["required"] == ["a", "b"]

    added = await client.call_tool("add", {"a": 2, "b": 40})
    assert not added.is_error and _text(added) == "42"
    # The agent runs on a labelled local fake model.
    assert _text(await client.call_tool("helper", {"request": "hi"})) == "fake-model answer"
    # A tool that needs approval is refused over MCP and never runs.
    refused = await client.call_tool("delete_order", {"order_id": "A1"})
    assert refused.is_error and "Nothing was executed" in _text(refused)

    resource = await client.read_resource("nexura://docs/readme")
    assert resource.contents[0].text == "Nexura interop resource text."  # type: ignore[union-attr]
    prompt = await client.get_prompt("greet", {"name": "Ann"})
    assert prompt.messages[0].content.text == "Hello Ann"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("mode", "version"),
    [("auto", "2026-07-28"), ("2026-07-28", "2026-07-28"), ("legacy", "2025-11-25")],
)
async def test_official_client_against_nexura_stdio(mode: str, version: str) -> None:
    async with Client(NEXURA_STDIO, mode=mode) as client:
        await _official_round_trip(client, version, mode)


@pytest.mark.parametrize(
    ("mode", "version"),
    [("auto", "2026-07-28"), ("2026-07-28", "2026-07-28"), ("legacy", "2025-11-25")],
)
async def test_official_client_against_nexura_http(
    nexura_mcp_http: str, mode: str, version: str
) -> None:
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"})
    async with Client(streamable_http_client(nexura_mcp_http, http_client=http), mode=mode) as c:
        await _official_round_trip(c, version, mode)


async def test_official_modern_client_cannot_skip_the_token(nexura_mcp_http: str) -> None:
    with pytest.raises(Exception):  # noqa: B017 - the SDK raises its own transport error
        async with Client(nexura_mcp_http, mode="2026-07-28") as client:
            await client.list_tools()


def _legacy_client(*args: str) -> dict[str, Any]:
    if not LEGACY_PYTHON.exists():
        pytest.skip("legacy/ environment not synced (cd legacy && uv sync)")
    out = subprocess.run(
        [str(LEGACY_PYTHON), str(ROOT / "legacy" / "legacy_client.py"), *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return dict(json.loads(out.stdout.strip().splitlines()[-1]))


def _check_legacy_view(seen: dict[str, Any]) -> None:
    assert seen["protocolVersion"] == "2025-11-25"
    assert seen["tools"] == ["helper", "add", "delete_order"]
    assert seen["add"] == "42"
    assert seen["refused"]["isError"] is True
    assert "Nothing was executed" in seen["refused"]["text"]
    assert seen["resource"] == "Nexura interop resource text."
    assert seen["prompt"] == "Hello Ann"


def test_handshake_only_official_client_against_nexura_stdio() -> None:
    _check_legacy_view(
        _legacy_client("stdio", sys.executable, str(SERVERS / "nexura_mcp_server.py"))
    )


def test_handshake_only_official_client_against_nexura_http(nexura_mcp_http: str) -> None:
    _check_legacy_view(_legacy_client("http", nexura_mcp_http, TOKEN))


# ------------------------------------------------- Nexura client -> official server


def _official_stdio() -> MCPServer:
    return MCPServer.stdio(
        sys.executable,
        str(SERVERS / "official_mcp_server.py"),
        "stdio",
        allowed_commands={sys.executable},
    )


async def _nexura_round_trip(remote: MCPServer, *, headers_checked: bool) -> None:
    async with remote:
        session = remote.session
        assert session.era == "modern" and session.server_version == "2026-07-28"
        assert session.server_info["name"] == "official-interop"
        assert "2026-07-28" in session.server_supported_versions
        assert "server/discover" in session.cache_hints

        tools = {t.name: t for t in await remote.tools()}
        assert set(tools) == {"multiply", "shout", "regional_lookup"}
        assert session.cache_hints["tools/list"].scope in ("public", "private")

        agent = Agent(
            name="caller",
            instructions="",
            tools=list(tools.values()),
            model=FakeModel(
                responses=[
                    [
                        ToolCall(id="m", name="multiply", arguments={"a": 6, "b": 7}),
                        ToolCall(
                            id="r",
                            name="regional_lookup",
                            arguments={"region": "eu-west-1", "key": "k1"},
                        ),
                    ],
                    "done",
                ]
            ),
        )
        result = await agent.run_async("go")
        out = {m.tool_call_id: m.content for m in result.messages if m.role == "tool"}
        assert out["m"] == "42"
        # Over HTTP the official server rejects this call unless Mcp-Param-Region matches.
        assert out["r"] == "k1@eu-west-1"

        resources = await remote.list_resources()
        assert [r.uri for r in resources] == ["memo://greeting"]
        assert await remote.read_resource("memo://greeting") == "hello from the official SDK"
        prompts = await remote.list_prompts()
        assert [p.name for p in prompts] == ["review"]
        messages = await remote.get_prompt("review", {"code": "x = 1"})
        assert messages[0].content == "Please review: x = 1"
    _ = headers_checked


async def test_nexura_client_against_official_stdio() -> None:
    await _nexura_round_trip(_official_stdio(), headers_checked=False)


async def test_nexura_client_against_official_http(official_mcp_http: str) -> None:
    await _nexura_round_trip(MCPServer.http(official_mcp_http), headers_checked=True)


async def test_official_server_enforces_the_param_header_nexura_sends(
    official_mcp_http: str,
) -> None:
    """Proves the Mcp-Param-Region header is required, so the call above really sent it."""
    meta = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "regional_lookup",
            "arguments": {"region": "eu", "key": "k"},
            "_meta": meta,
        },
    }
    headers = {
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2026-07-28",
        "Mcp-Method": "tools/call",
        "Mcp-Name": "regional_lookup",
    }
    async with httpx.AsyncClient() as http:
        missing = await http.post(official_mcp_http, json=body, headers=headers)
        assert missing.status_code == 400
        assert missing.json()["error"]["code"] == -32020
        ok = await http.post(
            official_mcp_http, json=body, headers={**headers, "Mcp-Param-Region": "eu"}
        )
        assert ok.status_code == 200


# ------------------------------------------ Nexura client -> handshake-only server


def _legacy_stdio(**options: Any) -> MCPServer:
    if not LEGACY_PYTHON.exists():
        pytest.skip("legacy/ environment not synced (cd legacy && uv sync)")
    return MCPServer.stdio(
        str(LEGACY_PYTHON),
        str(LEGACY_SERVER),
        "stdio",
        allowed_commands={str(LEGACY_PYTHON)},
        **options,
    )


async def _legacy_round_trip(remote: MCPServer) -> None:
    async with remote:
        session = remote.session
        assert session.era == "legacy" and session.server_version == "2025-11-25"
        tools = await remote.list_tools()
        assert [t.name for t in tools] == ["subtract"]
        result = await session.call_tool("subtract", {"a": 5, "b": 7})
        assert result.content[0].text == "-2"
        assert await remote.read_resource("memo://legacy") == "hello from mcp 1.30"
        messages = await remote.get_prompt("summarize", {"text": "abc"})
        assert messages[0].content == "Summarize: abc"


async def test_nexura_client_falls_back_to_handshake_over_stdio() -> None:
    await _legacy_round_trip(_legacy_stdio())


async def test_nexura_client_falls_back_to_handshake_over_http(legacy_mcp_http: str) -> None:
    await _legacy_round_trip(MCPServer.http(legacy_mcp_http))


async def test_pinned_modern_client_refuses_a_handshake_only_server() -> None:
    remote = _legacy_stdio(protocol_version="2026-07-28")
    try:
        with pytest.raises(McpProtocolVersionMismatchError):
            await remote.connect()
    finally:
        await remote.close()


async def test_pinned_handshake_version_skips_the_probe() -> None:
    remote = _official_stdio()
    remote.session.requested_version = "2025-06-18"
    async with remote:
        assert remote.session.era == "legacy"
        assert remote.session.server_version == "2025-06-18"
        assert [t.name for t in await remote.list_tools()][0] == "multiply"
