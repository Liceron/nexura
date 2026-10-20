"""Keyless synthetic order support example using FakeModel.

Demonstrates:
1. Defining a typed tool using @tool over synthetic customer orders.
2. Preserving Indic script (Hindi/English code-mixed) Unicode text unchanged across turns.
3. Observability through ordered lifecycle events via stream_async.
4. Synchronous execution via runner.run().
"""

from __future__ import annotations

import asyncio
from typing import Any

from nexura_runtime.events import RunEventType

from nexura_runtime import Agent, FakeModel, Runner, ToolCall, tool

# Synthetic in-memory orders database with code-mixed Hindi and English content
ORDERS_DB: dict[str, dict[str, Any]] = {
    "ORD-9872": {
        "order_id": "ORD-9872",
        "customer": "आरव शर्मा (Aarav Sharma)",
        "status": "Out for Delivery",
        "destination": "42 Connaught Place, New Delhi",
        "notes": "नमस्ते! आपका पार्सल आज दोपहर 2 बजे तक पहुँच जाएगा। (Package arriving by 2 PM)",
    },
    "ORD-1044": {
        "order_id": "ORD-1044",
        "customer": "Priya Patel",
        "status": "Processing",
        "destination": "15 MG Road, Bengaluru",
        "notes": "ऑर्डर तैयार किया जा रहा है। (Order is being prepared)",
    },
}


@tool
def lookup_order(order_id: str) -> dict[str, Any]:
    """Look up order status, customer details, and delivery notes by order ID."""
    order = ORDERS_DB.get(order_id)
    if not order:
        return {"error": f"Order '{order_id}' not found."}
    return order


async def run_streaming_example() -> None:
    print("=" * 60)
    print("1. Asynchronous Streaming Execution with Lifecycle Events")
    print("=" * 60)

    indic_note = ORDERS_DB["ORD-9872"]["notes"]

    # Scripted FakeModel turns:
    # Turn 1: model calls lookup_order
    # Turn 2: model provides final answer with preserved Unicode
    fake_model = FakeModel(
        responses=[
            [ToolCall(id="call_ord_9872", name="lookup_order", arguments={"order_id": "ORD-9872"})],
            f"ऑर्डर #ORD-9872 की स्थिति: Out for Delivery.\nनोट: {indic_note}",
        ]
    )

    agent = Agent(
        name="OrderSupportBot",
        instructions="Helpful order support assistant. Check order status using lookup_order.",
        model=fake_model,
        tools=[lookup_order],
    )

    runner = Runner()

    print(f"Agent: {agent.name}")
    print(f"Registered Tools: {[t.name for t in agent.tools]}")
    print("\nStreaming events:")

    final_output = ""
    async for event in runner.stream_async(agent, "Where is my order ORD-9872?"):
        print(f"  --> [{event.type.value.upper()}] run_id={event.run_id[:8]}... data={event.data}")
        if event.type == RunEventType.FINAL_OUTPUT:
            final_output = event.data.get("output", "")

    print("\nFinal Output:")
    print(final_output)

    # Verify Indic Unicode survived completely intact
    assert indic_note in final_output, "Indic Unicode note did not survive into final output!"
    print("\n[VERIFIED] Indic script (Hindi/English code-mixed) survived intact!")


def run_sync_example() -> None:
    print("\n" + "=" * 60)
    print("2. Synchronous Execution Convenience via runner.run()")
    print("=" * 60)

    indic_note = ORDERS_DB["ORD-1044"]["notes"]

    fake_model = FakeModel(
        responses=[
            [ToolCall(id="call_ord_1044", name="lookup_order", arguments={"order_id": "ORD-1044"})],
            f"Order #ORD-1044 Status: Processing. नोट: {indic_note}",
        ]
    )

    agent = Agent(
        name="OrderSupportBotSync",
        instructions="Support assistant",
        model=fake_model,
        tools=[lookup_order],
    )

    runner = Runner()
    result = runner.run(agent, "Please check ORD-1044")

    print(f"Run ID: {result.run_id}")
    print(f"Status: {result.status}")
    print(f"Turns: {result.turns_count}")
    print(f"Tool calls: {result.tool_calls_count}")
    print(f"Final output: {result.final_output}")

    assert indic_note in result.final_output
    print("[VERIFIED] Synchronous run completed successfully with Unicode preserved.")


def main() -> None:
    asyncio.run(run_streaming_example())
    run_sync_example()


if __name__ == "__main__":
    main()
