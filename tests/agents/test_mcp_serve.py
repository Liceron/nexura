"""Serving agents and tools over MCP, checked with Nexura's own MCP client."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from nexura_agents import Agent, AgentMCPServer, FakeModel, MCPServer, ToolCall, tool
from nexura_agents.mcp.types import LATEST_PROTOCOL_VERSION
from pydantic import BaseModel

FIXTURE = Path(__file__).with_name("mcp_agent_server_fixture.py")


def call(name: str, call_id: str = "c1", **arguments: Any) -> list[ToolCall]:
    return [ToolCall(id=call_id, name=name, arguments=arguments)]


async def test_stdio_server_exposes_agents_and_tools_to_any_mcp_client() -> None:
    remote = MCPServer.stdio(sys.executable, str(FIXTURE), allowed_commands={sys.executable})
    async with remote:
        definitions = {d.name: d for d in await remote.list_tools()}
        assert set(definitions) == {"support", "refunds", "order_status", "issue_refund"}
        assert definitions["support"].description == "Answers questions about orders."
        assert definitions["support"].inputSchema["required"] == ["request"]
        assert remote.session.server_version == LATEST_PROTOCOL_VERSION

        tools = {t.name: t for t in await remote.tools()}
        # A local agent delegates to the remote Nexura agent over MCP.
        local = Agent(
            name="Concierge",
            instructions="",
            tools=[
                tools["support"],
                tools["order_status"],
                tools["issue_refund"],
                tools["refunds"],
            ],
            model=FakeModel(
                responses=[
                    [
                        ToolCall(id="a", name="support", arguments={"request": "Where is A7?"}),
                        ToolCall(id="b", name="order_status", arguments={"order_id": "B2"}),
                        ToolCall(
                            id="c", name="issue_refund", arguments={"order_id": "A7", "amount": 9.5}
                        ),
                        ToolCall(id="d", name="refunds", arguments={"request": "refund A7"}),
                    ],
                    "done",
                ]
            ),
        )
        result = await local.run_async("help")

    out = {m.tool_call_id: m.content for m in result.messages if m.role == "tool"}
    assert out["a"] == "Order A7 is out for delivery."
    assert json.loads(out["b"]) == {"order_id": "B2", "status": "out for delivery"}
    # Approval-gated work is refused, never executed, and the reason reaches the caller.
    assert "needs human approval" in out["c"] and "Nothing was executed" in out["c"]
    assert "Stopped before issue_refund" in out["d"]


class Ticket(BaseModel):
    order_id: str
    priority: int


@tool
def echo(text: str) -> str:
    """Echo text."""
    return text


def triage_agent() -> Agent:
    return Agent(
        name="Triage Desk",
        description="Files tickets.",
        instructions="",
        model=FakeModel(responses=['{"order_id": "A1", "priority": 2}']),
        output_type=Ticket,
    )


def http_client(app: Any, **headers: str) -> MCPServer:
    return MCPServer.http(
        "http://testserver/mcp", headers=headers, transport=httpx.ASGITransport(app=app)
    )


async def test_http_app_round_trip_with_token() -> None:
    server = AgentMCPServer("desk", agents=[triage_agent()], tools=[echo])
    app = server.http_app(token="s3cret")
    async with http_client(app, Authorization="Bearer s3cret") as remote:
        tools = {t.name: t for t in await remote.tools()}
        assert set(tools) == {"triage_desk", "echo"}
        agent = Agent(
            name="Caller",
            instructions="",
            tools=list(tools.values()),
            model=FakeModel(responses=[call("triage_desk", "t", request="A1 is late"), "filed"]),
        )
        result = await agent.run_async("file it")
    out = {m.tool_call_id: m.content for m in result.messages if m.role == "tool"}
    assert json.loads(out["t"]) == {"order_id": "A1", "priority": 2}  # typed output as JSON


async def raw(app: Any, method: str, body: Any = None, **headers: str) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        return await client.request(
            method, "/mcp", headers=headers, content=None if body is None else json.dumps(body)
        )


async def test_http_app_rejects_bad_auth_origin_session_and_methods() -> None:
    app = AgentMCPServer("desk", tools=[echo]).http_app(token="s3cret")
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": LATEST_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"},
        },
    }
    auth = {"Authorization": "Bearer s3cret"}

    assert (await raw(app, "POST", init)).status_code == 401
    assert (await raw(app, "POST", init, Authorization="Bearer wrong")).status_code == 401
    assert (await raw(app, "POST", init, Origin="https://evil.example", **auth)).status_code == 403

    ok = await raw(app, "POST", init, **auth)
    session = ok.headers["mcp-session-id"]
    assert ok.json()["result"]["protocolVersion"] == LATEST_PROTOCOL_VERSION

    listing = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    assert (await raw(app, "POST", listing, **auth)).status_code == 400  # no session
    assert (
        await raw(app, "POST", listing, **{"Mcp-Session-Id": "forged"}, **auth)
    ).status_code == 404
    note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    assert (await raw(app, "POST", note, **{"Mcp-Session-Id": session}, **auth)).status_code == 202
    assert (await raw(app, "GET", None, **auth)).status_code == 405
    assert (
        await raw(app, "DELETE", None, **{"Mcp-Session-Id": session}, **auth)
    ).status_code == 200
    assert (
        await raw(app, "POST", listing, **{"Mcp-Session-Id": session}, **auth)
    ).status_code == 404  # session ended


def test_duplicate_exposed_names_are_rejected() -> None:
    @tool(name="triage_desk")
    def clash() -> str:
        return ""

    with pytest.raises(ValueError, match="triage_desk"):
        AgentMCPServer("x", agents=[triage_agent()], tools=[clash])
