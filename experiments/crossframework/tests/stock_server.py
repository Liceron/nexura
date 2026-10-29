"""Stock approval behavior; the pipe reader is enabled only by an explicit test flag."""

import asyncio

from crossframework.stdio import serve
from nexura_agents import AgentMCPServer, tool


@tool(requires_approval=True)
def issue_refund(order_id: str, amount: int) -> str:
    """Fake refund which must never run without approval."""
    raise AssertionError("Stock MCP approval refusal must prevent dispatch")


asyncio.run(serve(AgentMCPServer("stock-approval-probe", tools=[issue_refund])))
