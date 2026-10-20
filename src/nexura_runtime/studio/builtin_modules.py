# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Built-in synthetic modules and seeded template workflow for Nexura Studio.

All handlers in this module operate on synthetic local mock data and require zero
external API keys, paid credits, or remote cloud connections.
"""

from __future__ import annotations

import copy
import time
import zlib
from typing import Any

from nexura_agents.agent import Agent
from nexura_agents.models.fake import FakeModel
from nexura_agents.runner import Runner
from nexura_agents.tools import tool
from nexura_agents.types import ToolCall, Usage

from nexura_runtime.studio.traces import traced_run
from nexura_runtime.workflow import (
    HandlerOutput,
    ModuleRegistry,
    WorkflowNode,
    WorkflowSpec,
)

SYNTHETIC_ORDERS: dict[str, dict[str, Any]] = {
    "ORD-DELHI-501": {
        "order_id": "ORD-DELHI-501",
        "customer": "अमित वर्मा (Amit Verma)",
        "amount": 4500,
        "item": "स्मार्टफोन डिस्प्ले (Smartphone Display)",
        "category": "refund",
        "issue_notes": (
            "उत्पाद क्षतिग्रस्त मिला। ग्राहक ने पूर्ण वापसी का अनुरोध किया है। "
            "(Product damaged. Full refund requested.)"
        ),
    },
    "ORD-MUMBAI-202": {
        "order_id": "ORD-MUMBAI-202",
        "customer": "Priya Rao",
        "amount": 1200,
        "item": "वायरलेस चार्जर (Wireless Charger)",
        "category": "standard_support",
        "issue_notes": "डिलीवरी स्थिति की पुष्टि करें। (Please confirm delivery status.)",
    },
}

DEFAULT_MODULE_METADATA: list[dict[str, Any]] = [
    {
        "name": "synthetic_order_lookup",
        "title": "Synthetic Order Lookup",
        "description": "Looks up customer order details from a local synthetic database.",
        "disclaimer": "Synthetic / Local Mock Data (No Live Model or Network Required)",
        "is_synthetic": True,
        "parameters": ["order_id"],
        "outputs": ["customer", "amount", "item", "category", "issue_notes", "order_found"],
        "branches": [],
    },
    {
        "name": "synthetic_router",
        "title": "Synthetic Query Router",
        "description": (
            "Evaluates order amount and category to branch into refund vs standard support."
        ),
        "disclaimer": "Synthetic / Local Mock Data (No Live Model or Network Required)",
        "is_synthetic": True,
        "parameters": ["amount", "category"],
        "outputs": ["classification", "route"],
        "branches": ["refund", "standard_support"],
    },
    {
        "name": "synthetic_ticket_creator",
        "title": "Synthetic Ticket Creator",
        "description": (
            "Generates a support ticket and authorization code. Typically approval-gated."
        ),
        "disclaimer": "Synthetic / Local Mock Data (No Live Model or Network Required)",
        "is_synthetic": True,
        "parameters": ["amount", "customer", "order_id"],
        "outputs": ["ticket_id", "resolution", "ticket_status", "authorized_at"],
        "branches": [],
    },
    {
        "name": "synthetic_fake_agent",
        "title": "Synthetic Agent Handler",
        "description": (
            "Invokes a local FakeModel-backed agent turn to formulate a localized response."
        ),
        "disclaimer": "Synthetic / Local Mock Data (No Live Model or Network Required)",
        "is_synthetic": True,
        "parameters": ["customer", "ticket_id"],
        "outputs": ["agent_response", "agent_turns"],
        "branches": [],
    },
]


def synthetic_order_lookup(state: dict[str, Any]) -> dict[str, Any]:
    """Look up order details from synthetic in-memory store."""
    order_id = state.get("order_id", "ORD-DELHI-501")
    order = SYNTHETIC_ORDERS.get(order_id)
    if not order:
        return {
            "order_id": order_id,
            "order_found": False,
            "error_note": f"Synthetic order '{order_id}' not found.",
        }
    return {
        "order_id": order["order_id"],
        "customer": order["customer"],
        "amount": order["amount"],
        "item": order["item"],
        "category": order["category"],
        "issue_notes": order["issue_notes"],
        "order_found": True,
    }


def synthetic_router(state: dict[str, Any]) -> HandlerOutput:
    """Route based on order amount and issue category."""
    amount = float(state.get("amount", 0))
    category = str(state.get("category", ""))

    if amount >= 2000 or category == "refund":
        return HandlerOutput(
            state_update={
                "classification": "High-Value Refund Request (Approval Gate Required)",
                "route": "refund",
            },
            branch="refund",
        )
    return HandlerOutput(
        state_update={
            "classification": "Standard Customer Inquiry",
            "route": "standard_support",
        },
        branch="standard_support",
    )


def synthetic_ticket_creator(state: dict[str, Any]) -> dict[str, Any]:
    """Create a synthetic ticket record. Typically placed behind an approval gate."""
    order_id = state.get("order_id", "ORD-UNKNOWN")
    customer = state.get("customer", "Customer")
    amount = state.get("amount", 0)

    # crc32 is stable across processes, unlike hash(), so every surface agrees.
    ticket_id = f"TICKET-SYNTH-{zlib.crc32(str(order_id).encode()) % 10000:04d}"
    resolution = (
        f"रिफंड स्वीकृत: ₹{amount} ग्राहक {customer} को जारी किया गया। "
        f"(Synthetic refund approved for order #{order_id} under ticket {ticket_id})"
    )

    return {
        "ticket_id": ticket_id,
        "ticket_status": "ISSUED",
        "resolution": resolution,
        "authorized_at": time.time(),
    }


async def synthetic_fake_agent(state: dict[str, Any]) -> dict[str, Any]:
    """Invoke local FakeModel agent turn without external API calls."""
    customer = state.get("customer", "Valued Customer")
    ticket_id = state.get("ticket_id", "N/A")

    fake_response = (
        f"नमस्ते {customer}! आपका सहायता टिकट {ticket_id} सफलतापूर्वक तैयार कर दिया गया है। "
        f"हमारी सहायता टीम जल्द ही आपसे संपर्क करेगी। "
        f"(Hello {customer}! Your support ticket {ticket_id} has been created.)"
    )

    model = FakeModel(responses=[fake_response])
    agent = Agent(
        name="SyntheticSupportAgent",
        instructions=(
            "Synthesize local polite support responses preserving multilingual Indic text."
        ),
        model=model,
    )
    runner = Runner()
    user_prompt = f"Prepare message for customer {customer} regarding {ticket_id}"
    result = await traced_run(runner, agent, user_prompt)

    return {
        "agent_response": result.final_output,
        "agent_turns": result.turns_count,
    }


async def synthetic_trace_agent(state: dict[str, Any]) -> dict[str, Any]:
    """Local FakeModel agent that calls a tool, for exercising run traces (no network).

    The tool call carries a deliberately fake credential so redaction is visible in traces.
    With ``state["fail"]`` set the node raises after the agent finished, to show a failed run.
    """

    @tool(description="Look up a synthetic policy document.")
    def lookup_policy(topic: str, api_key: str = "") -> str:
        return f"Policy for {topic}: refunds within 30 days. Internal key sk-FAKE0123456789abcdef"

    topic = str(state.get("topic", "refunds"))
    model = FakeModel(
        responses=[
            [
                ToolCall(
                    id="call_policy_1",
                    name="lookup_policy",
                    arguments={"topic": topic, "api_key": "sk-FAKE0123456789abcdef"},
                )
            ],
            f"Policy summary for {topic}: refunds are accepted within 30 days.",
        ],
        default_usage=Usage(prompt_tokens=120, completion_tokens=30, total_tokens=150),
    )
    agent = Agent(
        name="PolicyAgent",
        instructions="Answer from the policy lookup tool.",
        model=model,
        tools=[lookup_policy],
    )
    result = await traced_run(Runner(), agent, f"What is the {topic} policy?")
    if state.get("fail"):
        raise RuntimeError("synthetic failure after agent run (token=sk-FAKE0123456789abcdef)")
    return {"policy_answer": result.final_output, "agent_turns": result.turns_count}


def get_default_registry() -> ModuleRegistry:
    """Construct a ModuleRegistry pre-populated with synthetic handlers."""
    registry = ModuleRegistry()
    registry.register("synthetic_order_lookup", synthetic_order_lookup)
    registry.register("synthetic_router", synthetic_router)
    registry.register("synthetic_ticket_creator", synthetic_ticket_creator)
    registry.register("synthetic_fake_agent", synthetic_fake_agent)
    registry.register("synthetic_trace_agent", synthetic_trace_agent)
    return registry


def get_default_module_metadata() -> list[dict[str, Any]]:
    """Return immutable copies of module metadata for the Studio API."""
    return copy.deepcopy(DEFAULT_MODULE_METADATA)


def get_default_workflow_spec() -> WorkflowSpec:
    """Create the seeded order support workflow template."""
    nodes = {
        "lookup": WorkflowNode(
            id="lookup",
            handler="synthetic_order_lookup",
            default_transition="route",
        ),
        "route": WorkflowNode(
            id="route",
            handler="synthetic_router",
            transitions={
                "refund": "approval_gate",
                "standard_support": "agent_assist",
            },
            default_transition="approval_gate",
        ),
        "approval_gate": WorkflowNode(
            id="approval_gate",
            handler="synthetic_ticket_creator",
            requires_approval=True,  # Execution pauses BEFORE this handler runs
            default_transition="agent_assist",
        ),
        "agent_assist": WorkflowNode(
            id="agent_assist",
            handler="synthetic_fake_agent",
            default_transition=None,
        ),
    }

    return WorkflowSpec(
        name="OrderSupportWorkflow",
        entry_node="lookup",
        nodes=nodes,
        spec_version=1,
        terminal_nodes=["agent_assist"],
    )
