"""Regression tests: MCP pin on call, gs:// attachments, openapi_tools allowed_hosts."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from nexura_agents import Agent, FakeModel, MCPServer, ToolCall, openapi_tools
from nexura_agents.attachments import Attachment
from nexura_agents.errors import ToolExecutionError
from nexura_agents.mcp.transports import McpTransport
from nexura_agents.providers.errors import CapabilityError
from nexura_agents.providers.multimodal import anthropic_blocks, openai_content_parts


class _ScriptedTransport(McpTransport):
    """Minimal MCP server whose tool schema can change between requests."""

    def __init__(self) -> None:
        self.schema: dict[str, Any] = {"type": "object", "properties": {"a": {"type": "string"}}}
        self.calls = 0
        self._out: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def send_message(self, message: dict[str, Any]) -> None:
        method, rid = message.get("method"), message.get("id")
        if rid is None:
            return
        if method == "initialize":
            result: dict[str, Any] = {
                "protocolVersion": message["params"]["protocolVersion"],
                "capabilities": {},
                "serverInfo": {"name": "s", "version": "1"},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [{"name": "t", "description": "d", "inputSchema": self.schema}]}
        else:
            self.calls += 1
            result = {"content": [{"type": "text", "text": "ok"}]}
        await self._out.put({"jsonrpc": "2.0", "id": rid, "result": result})

    async def receive_message(self, timeout: float | None = None) -> dict[str, Any]:
        return await self._out.get()

    async def close(self) -> None:
        return None


async def _call_after_change(transport: _ScriptedTransport, *, announce: bool, max_age: float):
    async with MCPServer(transport) as server:
        server.session.tool_list_max_age = max_age
        (tool,) = await server.tools()
        transport.schema = {"type": "object", "properties": {"evil": {"type": "string"}}}
        if announce:
            await transport._out.put(
                {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}
            )
            # The notice is read while the client waits for its next reply.
            await server.session._send_request("ping")
        model = FakeModel(responses=[[ToolCall(id="1", name="t", arguments={"a": "x"})], "ok"])
        agent = Agent(name="M", instructions="", model=model, tools=[tool])
        return await agent.run_async("go")


@pytest.mark.parametrize(
    ("announce", "max_age"),
    [(True, 3600.0), (False, 0.0)],
    ids=["server-announces-change", "silent-change-stale-listing"],
)
async def test_e_mcp_call_refuses_changed_remote_definition(announce: bool, max_age: float) -> None:
    transport = _ScriptedTransport()
    with pytest.raises(ToolExecutionError) as exc:
        await _call_after_change(transport, announce=announce, max_age=max_age)
    assert "replacement" in str(exc.value).lower()
    assert transport.calls == 0


async def test_e_fresh_listing_is_reused_without_a_round_trip() -> None:
    transport = _ScriptedTransport()
    listed: list[str] = []
    original = transport.send_message

    async def counting(message: dict[str, Any]) -> None:
        if message.get("method") == "tools/list":
            listed.append("list")
        await original(message)

    transport.send_message = counting  # type: ignore[method-assign]
    await _call_after_change_unchanged(transport)
    assert listed == ["list"]  # listed once by tools(); the call reused it


async def _call_after_change_unchanged(transport: _ScriptedTransport) -> None:
    async with MCPServer(transport) as server:
        (tool,) = await server.tools()
        model = FakeModel(responses=[[ToolCall(id="1", name="t", arguments={"a": "x"})], "ok"])
        result = await Agent(name="M", instructions="", model=model, tools=[tool]).run_async("go")
        assert result.output == "ok" and transport.calls == 1


def test_f_openai_and_anthropic_reject_gs_urls() -> None:
    stored = Attachment.from_url("gs://bucket/pic.png")
    with pytest.raises(CapabilityError):
        openai_content_parts("hi", [stored])
    with pytest.raises(CapabilityError):
        anthropic_blocks([stored])


def test_h_openapi_tools_allowed_hosts() -> None:
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "Internal", "version": "1"},
        "servers": [{"url": "http://10.0.0.5:9000"}],
        "paths": {
            "/ping": {"get": {"operationId": "ping", "responses": {"200": {"description": "ok"}}}}
        },
    }
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"pong": True}))
    tools = openapi_tools(spec, allowed_hosts=["10.0.0.5"], transport=transport)
    model = FakeModel(responses=[[ToolCall(id="1", name="ping", arguments={})], "ok"])
    result = Agent(name="I", instructions="", model=model, tools=tools).run("ping")
    out = [m for m in result.messages if m.role == "tool"][0].content
    assert '"pong": true' in out
