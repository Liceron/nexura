"""Launched as a subprocess by the tests: serves a support agent and two tools over stdio."""

from nexura_agents import Agent, AgentMCPServer, FakeModel, ToolCall, tool


@tool
def order_status(order_id: str) -> dict[str, str]:
    """Look up an order's delivery status."""
    return {"order_id": order_id, "status": "out for delivery"}


@tool(requires_approval=True)
def issue_refund(order_id: str, amount: float) -> str:
    """Refund an order (needs a human decision)."""
    return "refunded"


support = Agent(
    name="Support",
    description="Answers questions about orders.",
    instructions="Use tools.",
    model=FakeModel(
        responses=[
            [ToolCall(id="1", name="order_status", arguments={"order_id": "A7"})],
            "Order A7 is out for delivery.",
        ]
    ),
    tools=[order_status],
)

refunds = Agent(
    name="Refunds",
    description="Issues refunds.",
    instructions="",
    model=FakeModel(
        responses=[
            [ToolCall(id="r", name="issue_refund", arguments={"order_id": "A7", "amount": 9.5})]
        ]
    ),
    tools=[issue_refund],
)

AgentMCPServer("shop", agents=[support, refunds], tools=[order_status, issue_refund]).run_stdio()
