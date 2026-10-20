"""Tools from an MCP server and from an OpenAPI spec.

Scenario: an Ops agent needs arithmetic (served by an MCP server) and order
lookup/cancel (a REST API described by an inline OpenAPI spec). The MCP server
is an in-process ``AgentMCPServer`` reached via ``httpx.ASGITransport``; the
REST API is faked with ``httpx.MockTransport``. Cancelling is a write, so it
pauses for approval (``requires_approval="writes"``).

Demonstrates: ``MCPServer.http``, ``AgentMCPServer.http_app``, ``openapi_tools``.

Offline: scripted ``FakeModel`` only. To use a real model:
# model="openai:gpt-4.1-mini"
"""

import asyncio

import httpx
from nexura_agents import (
    Agent,
    AgentMCPServer,
    FakeModel,
    MCPServer,
    Runner,
    ToolCall,
    openapi_tools,
    tool,
)

SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "Shop", "version": "1"},
    "servers": [{"url": "https://api.shop.example/v1"}],
    "paths": {
        "/orders/{order_id}": {
            "get": {
                "operationId": "getOrder",
                "summary": "Get an order",
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
        "/orders/{order_id}/cancel": {
            "post": {
                "operationId": "cancelOrder",
                "summary": "Cancel an order",
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
    },
}


@tool
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


async def main() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "A1", "status": "open", "total": 40})

    rest_tools = openapi_tools(
        SPEC,
        headers={"Authorization": "Bearer demo-key"},
        requires_approval="writes",
        transport=httpx.MockTransport(handler),
    )

    mcp_app = AgentMCPServer("calc", tools=[add]).http_app()
    transport = httpx.ASGITransport(app=mcp_app)
    async with MCPServer.http("http://calc.test/mcp", transport=transport) as calc:
        mcp_tools = await calc.tools()
        print("MCP tools:", [t.name for t in mcp_tools])
        print("OpenAPI tools:", [t.name for t in rest_tools])

        model = FakeModel(
            responses=[
                [ToolCall(id="1", name="getOrder", arguments={"order_id": "A1"})],
                [ToolCall(id="2", name="add", arguments={"a": 40, "b": 5})],
                [ToolCall(id="3", name="cancelOrder", arguments={"order_id": "A1"})],
                "Order A1 (total 40, 45 with shipping) cancelled.",
            ]
        )
        agent = Agent(
            name="Ops",
            instructions="Use tools to manage orders.",
            model=model,
            tools=[*mcp_tools, *rest_tools],
        )
        runner = Runner()
        paused = await runner.run_async(agent, "Check order A1 with shipping 5, then cancel it")
        print("status after first pass:", paused.status)
        print("requests so far:", [(r.method, r.url.path) for r in requests])
        done = await runner.resume_async(agent, paused.interruption, approve=True)
        print("requests total:", [(r.method, r.url.path) for r in requests])
        print("final:", done.output)
        assert [r.method for r in requests] == ["GET", "POST"]
        assert requests[0].headers["authorization"] == "Bearer demo-key"
    print("MCP AND OPENAPI OK")


if __name__ == "__main__":
    asyncio.run(main())
