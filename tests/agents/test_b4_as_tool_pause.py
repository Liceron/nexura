"""An agent used as a tool must not hide that it stopped for approval (B4)."""

from __future__ import annotations

import pytest
from nexura_agents import Agent, FakeModel, ToolCall, tool
from nexura_agents.errors import ToolError


@tool(requires_approval=True)
def refund(order_id: str) -> str:
    """Refund an order."""
    return "refunded"


async def test_sub_agent_waiting_for_approval_fails_the_tool_call_loudly() -> None:
    clerk = Agent(
        name="Clerk",
        instructions="",
        model=FakeModel(
            responses=[[ToolCall(id="r", name="refund", arguments={"order_id": "A1"})]]
        ),
        tools=[refund],
    )
    boss = Agent(
        name="Boss",
        instructions="",
        model=FakeModel(
            responses=[
                [ToolCall(id="c", name="clerk", arguments={"request": "refund A1"})],
                "done",
            ]
        ),
        tools=[clerk.as_tool()],
    )
    # Before the fix the calling model got "null" and could report success.
    with pytest.raises(ToolError, match="Clerk.*stopped before refund.*approval"):
        await boss.run_async("go")
