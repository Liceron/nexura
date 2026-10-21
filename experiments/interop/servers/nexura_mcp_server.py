"""Nexura AgentMCPServer used by the interop tests.

    python nexura_mcp_server.py stdio
    python nexura_mcp_server.py http PORT

One plain tool (``add``), one tool that needs approval (``delete_order``, refused over
MCP), one agent driven by a labelled local fake model, one resource and one prompt.
No network access and no paid model calls.
"""

from __future__ import annotations

import sys

from nexura_agents import Agent, AgentMCPServer, FakeModel, tool
from nexura_agents.mcp.types import McpPromptArgument


@tool
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@tool(requires_approval=True)
def delete_order(order_id: str) -> str:
    """Delete an order (needs human approval)."""
    return f"deleted {order_id}"


def build() -> AgentMCPServer:
    helper = Agent(
        name="helper",
        description="Answers with a fixed sentence (local fake model).",
        instructions="",
        model=FakeModel(responses=["fake-model answer"] * 20),
    )
    server = AgentMCPServer("nexura-interop", agents=[helper], tools=[add, delete_order])

    server.session.register_resource(
        "nexura://docs/readme", "readme", "Nexura interop resource text.", mime_type="text/plain"
    )

    async def greet(args: dict[str, str]) -> list[dict[str, object]]:
        return [{"role": "user", "content": {"type": "text", "text": f"Hello {args['name']}"}}]

    server.session.register_prompt(
        "greet", "Greets someone.", [McpPromptArgument(name="name", required=True)], greet
    )
    return server


if __name__ == "__main__":
    mode = sys.argv[1]
    if mode == "stdio":
        build().run_stdio()
    else:
        import uvicorn

        uvicorn.run(
            build().http_app(token="interop-token"),
            host="127.0.0.1",
            port=int(sys.argv[2]),
            log_level="warning",
        )
