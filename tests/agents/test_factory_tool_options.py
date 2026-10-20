"""F27: module_tool, openapi_tools and MCPServer pass the per-tool options through."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest
from nexura_agents import (
    Agent,
    FakeModel,
    GuardrailResult,
    MCPServer,
    Runner,
    ToolCall,
    module_tool,
)
from nexura_agents.errors import SDKValidationError
from nexura_agents.mcp import McpInMemoryTransport, McpServerSession
from nexura_agents.openapi.toolset import openapi_tools
from nexura_agents.run_context import RunContext

SPEC: dict[str, Any] = {
    "openapi": "3.0.3",
    "info": {"title": "Shop", "version": "1"},
    "servers": [{"url": "https://shop.example.com/v1"}],
    "paths": {
        "/orders/{order_id}": {
            "get": {
                "operationId": "getOrder",
                "parameters": [
                    {
                        "name": "order_id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {"200": {"description": "ok"}},
            }
        },
        "/refunds": {
            "post": {
                "operationId": "createRefund",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "order_id": {"type": "string"},
                                    "amount": {"type": "number"},
                                },
                                "required": ["order_id", "amount"],
                            }
                        }
                    },
                },
                "responses": {"201": {"description": "created"}},
            }
        },
    },
}


def shop_transport(log: list[Any]) -> Any:
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        log.append(request)
        return httpx.Response(201, json={"refund_id": "R9"})

    return httpx.MockTransport(handler)


OPTIONS = {"on_timeout", "input_guardrails", "output_guardrails", "enabled"}


def call(name: str, **arguments: Any) -> list[ToolCall]:
    return [ToolCall(id="c1", name=name, arguments=arguments)]


def tool_messages(result: Any) -> list[str | None]:
    return [m.content for m in result.messages if m.role == "tool"]


def test_every_factory_accepts_the_options() -> None:
    for factory, timeout_name in (
        (module_tool, "timeout"),
        (openapi_tools, "timeout"),
        (MCPServer.__init__, "tool_timeout"),
    ):
        params = set(inspect.signature(factory).parameters)
        assert OPTIONS | {timeout_name} <= params, factory


def block_big_refunds(ctx: RunContext[Any], args: dict[str, Any]) -> GuardrailResult:
    if ctx.tool_name == "createRefund" and args.get("amount", 0) > 100:
        return GuardrailResult.rejected("refunds over 100 need a manager")
    return GuardrailResult.passed()


async def test_openapi_tools_get_guardrails_and_enabled() -> None:
    import httpx

    log: list[httpx.Request] = []
    tools = openapi_tools(
        SPEC,
        transport=shop_transport(log),
        input_guardrails=[block_big_refunds],
        enabled=lambda ctx: ctx.deps != "read-only",
    )
    assert {t.name for t in tools} == {"getOrder", "createRefund"}
    assert all(t.input_guardrails == (block_big_refunds,) for t in tools)

    model = FakeModel(responses=[call("createRefund", order_id="A1", amount=500), "escalated"])
    agent = Agent(name="Shop", instructions="i", model=model, tools=tools)
    # createRefund is a POST, so it pauses for tool approval; guardrails run on the approved call.
    runner = Runner()
    paused = await runner.run_async(agent, "refund")
    assert paused.interruption is not None and log == []
    result = await runner.resume_async(
        agent, paused.interruption, approve=paused.interruption.pending_digests()
    )
    assert tool_messages(result) == [
        "Tool call blocked by guardrail 'block_big_refunds': refunds over 100 need a manager"
    ]
    assert log == []  # the HTTP call was never made

    hidden = FakeModel(responses=["no tools"])
    await Agent(name="Shop", instructions="i", model=hidden, tools=tools).run_async(
        "x", deps="read-only"
    )
    assert hidden.calls[0]["tools"] is None


def test_openapi_tools_validate_the_options() -> None:
    with pytest.raises(SDKValidationError, match="timeout must be"):
        openapi_tools(SPEC, timeout=-1)


async def slow_mcp_server(**options: Any) -> tuple[MCPServer, asyncio.Task[None]]:
    client_side, server_side = McpInMemoryTransport.create_pair()
    session = McpServerSession(server_side)

    async def slow(args: dict[str, Any]) -> str:
        await asyncio.sleep(5)
        return "late"

    session.register_tool("slow", "Slow.", {"type": "object"}, slow)
    task = asyncio.create_task(session.serve_loop())
    return MCPServer(client_side, name="s", **options), task


async def test_mcp_server_tools_get_the_timeout() -> None:
    mcp, task = await slow_mcp_server(tool_timeout=0.1)
    try:
        async with mcp:
            tools = await mcp.tools()
            assert tools[0].timeout == 0.1
            model = FakeModel(responses=[call("slow"), "gave up"])
            agent = Agent(name="A", instructions="i", model=model, tools=tools)
            result = await agent.run_async("go")
    finally:
        task.cancel()
    assert result.output == "gave up"
    assert tool_messages(result) == ["Tool call timed out: 'slow' did not finish within 0.1s."]


async def test_mcp_server_validates_options_when_tools_are_built() -> None:
    mcp, task = await slow_mcp_server(on_timeout="retry")
    try:
        async with mcp:
            with pytest.raises(SDKValidationError, match="on_timeout"):
                await mcp.tools()
    finally:
        task.cancel()
