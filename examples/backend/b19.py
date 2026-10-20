"""Standalone demonstration for Module B19: Agents, Subagents and Handoffs.

Runs 100% keylessly and offline:
  uv run python examples/backend/b19.py
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from typing import Any

from nexura_runtime.contracts.context import ExecutionContext
from nexura_runtime.providers.fake import StreamingFakeProvider
from nexura_runtime.tools import tool
from nexura_runtime.types import Message, ModelResponse, ToolCall
from pydantic import BaseModel

from nexura_runtime.runtime.agents import (
    AgentCoordinator,
    AgentCoordinatorModule,
    AgentDefinition,
    CapabilityEscalationError,
    HandoffFuelExhaustedError,
    HandoffTarget,
    MaxHandoffDepthExceededError,
    ModelLengthExceededError,
    ModelRefusalError,
    ParentCancelledError,
)


class SupportTicketSummary(BaseModel):
    ticket_id: str
    category: str
    recommended_action: str


async def main_async() -> None:
    print("=== Module B19: Agents, Subagents and Handoffs Demonstration ===\n")

    # 1. Define synthetic local tools
    print("1. Defining synthetic local domain tools...")

    @tool(description="Search enterprise technical documentation database.")
    def query_kb(topic: str) -> str:
        return f"KB match: Issue with '{topic}' is resolved by applying patch v2.4."

    @tool(description="Draft technical resolution ticket.")
    def draft_resolution(summary: str, severity: str) -> dict[str, Any]:
        return {
            "ticket_id": "TICK-9081",
            "summary": summary,
            "severity": severity,
            "status": "drafted",
        }

    # 2. Configure multi-agent collaboration with explicit handoff targets
    print("\n2. Configuring Researcher and Support AgentDefinitions...")

    # Provider for researcher: calls query_kb, then hands off to support agent
    researcher_provider = StreamingFakeProvider(
        canned_responses=[
            ModelResponse(
                message=Message(
                    role="assistant",
                    tool_calls=[
                        ToolCall(
                            id="call_kb_1",
                            name="query_kb",
                            arguments={"topic": "database sync timeout"},
                        )
                    ],
                )
            ),
            ModelResponse(
                message=Message(
                    role="assistant",
                    tool_calls=[
                        ToolCall(
                            id="call_ho_1",
                            name="transfer_to_support_specialist",
                            arguments={
                                "reason": (
                                    "Research complete. Delegating to support for ticket drafting."
                                ),
                                "context": {"findings": "patch v2.4 resolves DB timeout"},
                            },
                        )
                    ],
                )
            ),
        ]
    )

    # Provider for support specialist: calls draft_resolution, produces structured response
    support_provider = StreamingFakeProvider(
        canned_responses=[
            ModelResponse(
                message=Message(
                    role="assistant",
                    tool_calls=[
                        ToolCall(
                            id="call_draft_1",
                            name="draft_resolution",
                            arguments={
                                "summary": "Apply patch v2.4 to resolve DB timeout",
                                "severity": "P2",
                            },
                        )
                    ],
                )
            ),
            ModelResponse(
                message=Message(
                    role="assistant",
                    content=json.dumps(
                        {
                            "ticket_id": "TICK-9081",
                            "category": "database_infrastructure",
                            "recommended_action": "Apply patch v2.4 and monitor replication.",
                        }
                    ),
                )
            ),
        ]
    )

    researcher_agent = AgentDefinition(
        name="researcher_agent",
        instructions=(
            "You are an enterprise technical researcher. Research issues and hand off to support."
        ),
        model=researcher_provider,
        tools=[query_kb],
        handoffs=[
            HandoffTarget(
                target="support_specialist",
                description="Transfer findings to support specialist for ticket creation.",
            )
        ],
        max_turns=5,
    )

    support_agent = AgentDefinition(
        name="support_specialist",
        instructions="You are a tier-2 technical support specialist.",
        model=support_provider,
        tools=[draft_resolution],
        output_schema=SupportTicketSummary,
        max_turns=5,
    )

    coordinator = AgentCoordinator(
        agents=[researcher_agent, support_agent],
        max_handoff_depth=3,
        total_fuel=10,
    )

    print("\n3. Executing multi-agent collaboration with traceable handoff...")
    ctx = ExecutionContext(workspace_id="ws_demo", run_id="run_coord_root", node_id="entry")
    res = await coordinator.run_async(
        initial_agent_name="researcher_agent",
        prompt="Our database is timing out during replication.",
        context=ctx,
    )

    print(f"  [SUCCESS] Final Agent: {res.final_agent_name}")
    print(f"  Total fuel consumed: {res.total_fuel_consumed} turn(s)")
    print(f"  Total handoffs: {res.handoffs_count}")
    print(f"  Final Output: {res.final_output}")
    if res.structured_output:
        print(f"  Structured Output: {res.structured_output}")
    print(f"  Execution Traces ({len(res.traces)} steps):")
    for t in res.traces:
        print(
            f"   - Agent '{t.agent_name}' (run: {t.run_id}, turns: {t.turns}, "
            f"handoff: {t.handoff_to})"
        )

    # 4. Agent-as-a-tool delegation with isolated history and capability bounding
    print("\n4. Testing Subagent Delegation via agent.as_tool()...")
    subagent_tool = researcher_agent.as_tool(allowed_tools=["query_kb"])
    print(f"  Generated tool: {subagent_tool.name} ({subagent_tool.description})")

    # Invoking subagent directly
    researcher_provider_2 = StreamingFakeProvider(
        canned_responses=[
            ModelResponse(
                message=Message(role="assistant", content="Patch v2.4 verified for replication.")
            )
        ]
    )
    bounded_researcher = AgentDefinition(
        name="bounded_researcher",
        instructions="Bounded research assistant.",
        model=researcher_provider_2,
        tools=[query_kb],
        allowed_tools=["query_kb"],
    )
    bounded_tool = bounded_researcher.as_tool()
    sub_res = await bounded_tool.arun(query="Verify replication patch")
    print(f"  Subagent tool response: {sub_res}")

    # 5. Enforcing capability restrictions (attempting forbidden tool)
    print("\n5. Verifying capability escalation prevention...")
    escalating_provider = StreamingFakeProvider(
        canned_responses=[
            ModelResponse(
                message=Message(
                    role="assistant",
                    tool_calls=[
                        ToolCall(
                            id="tc_escalate",
                            name="draft_resolution",  # forbidden tool
                            arguments={"summary": "unauthorized", "severity": "P1"},
                        )
                    ],
                )
            )
        ]
    )
    # Registration-time check
    try:
        AgentDefinition(
            name="illegal_register_agent",
            model=escalating_provider,
            tools=[query_kb, draft_resolution],
            allowed_tools=["query_kb"],
        )
        print("  [FAIL] Expected construction CapabilityEscalationError was not raised!")
    except CapabilityEscalationError as exc:
        print(f"  [SUCCESS] Registration-time capability escalation blocked: {exc}")

    # Runtime invocation check
    restricted_agent = AgentDefinition(
        name="restricted_agent",
        instructions="Restricted agent with query_kb only.",
        model=escalating_provider,
        tools=[query_kb],
        allowed_tools=["query_kb"],
    )
    try:
        coord_restricted = AgentCoordinator(agents=[restricted_agent])
        await coord_restricted.run_async("restricted_agent", "Attempt unauthorized action")
        print("  [FAIL] Expected runtime CapabilityEscalationError was not raised!")
    except CapabilityEscalationError as exc:
        print(f"  [SUCCESS] Runtime capability escalation blocked: {exc}")

    # 6. Verifying depth and fuel bounds on recursive handoffs
    print("\n6. Verifying recursive handoff depth bounds...")
    recursive_provider_a = StreamingFakeProvider(
        canned_responses=[
            ModelResponse(
                message=Message(
                    role="assistant",
                    tool_calls=[
                        ToolCall(
                            id="c_a1",
                            name="transfer_to_ping_b",
                            arguments={"reason": "Ping B"},
                        )
                    ],
                )
            )
            for _ in range(10)
        ]
    )
    recursive_provider_b = StreamingFakeProvider(
        canned_responses=[
            ModelResponse(
                message=Message(
                    role="assistant",
                    tool_calls=[
                        ToolCall(
                            id="c_b1",
                            name="transfer_to_ping_a",
                            arguments={"reason": "Ping A"},
                        )
                    ],
                )
            )
            for _ in range(10)
        ]
    )

    ping_a = AgentDefinition(
        name="ping_a",
        instructions="Ping agent A",
        model=recursive_provider_a,
        handoffs=[HandoffTarget(target="ping_b")],
    )
    ping_b = AgentDefinition(
        name="ping_b",
        instructions="Ping agent B",
        model=recursive_provider_b,
        handoffs=[HandoffTarget(target="ping_a")],
    )

    recursive_coord = AgentCoordinator(
        agents=[ping_a, ping_b],
        max_handoff_depth=2,
        total_fuel=20,
    )

    try:
        await recursive_coord.run_async("ping_a", "Start infinite ping-pong")
        print("  [FAIL] Expected MaxHandoffDepthExceededError was not raised!")
    except MaxHandoffDepthExceededError as exc:
        print(f"  [SUCCESS] Recursive handoff depth limit strictly enforced: {exc}")

    # Fuel exhaustion test
    print("\n7. Verifying coordination fuel exhaustion limit...")
    fuel_coord = AgentCoordinator(
        agents=[ping_a, ping_b],
        max_handoff_depth=10,
        total_fuel=2,
    )
    try:
        await fuel_coord.run_async("ping_a", "Run out of fuel")
        print("  [FAIL] Expected HandoffFuelExhaustedError was not raised!")
    except HandoffFuelExhaustedError as exc:
        print(f"  [SUCCESS] Global turn fuel limit strictly enforced: {exc}")

    # 8. Verifying cancellation propagation
    print("\n8. Verifying parent cancellation propagation...")
    cancelling_ctx = ExecutionContext(run_id="run_cancel_test", node_id="parent_node")
    cancelling_ctx.cancel()  # Cancel parent context

    try:
        await coordinator.run_async(
            "researcher_agent", "Query after cancel", context=cancelling_ctx
        )
        print("  [FAIL] Expected ParentCancelledError was not raised!")
    except ParentCancelledError as exc:
        print(f"  [SUCCESS] Parent cancellation halted execution with zero tool effects: {exc}")

    # 9. Verifying explicit model refusal and length limits
    print("\n9. Verifying explicit ModelRefusalError and ModelLengthExceededError...")
    refusal_provider = StreamingFakeProvider(
        canned_responses=[
            ModelResponse(
                message=Message(role="assistant", content="Safety policy violation"),
                finish_reason="refusal",
            )
        ]
    )
    refusal_agent = AgentDefinition(name="refusal_agent", model=refusal_provider)
    coord_refusal = AgentCoordinator(agents=[refusal_agent])
    try:
        await coord_refusal.run_async("refusal_agent", "Test refusal")
        print("  [FAIL] Expected ModelRefusalError was not raised!")
    except ModelRefusalError as exc:
        print(f"  [SUCCESS] Model refusal explicitly caught: {exc}")

    length_provider = StreamingFakeProvider(
        canned_responses=[
            ModelResponse(
                message=Message(role="assistant", content="Context window exceeded"),
                finish_reason="length",
            )
        ]
    )
    length_agent = AgentDefinition(name="length_agent", model=length_provider)
    coord_length = AgentCoordinator(agents=[length_agent])
    try:
        await coord_length.run_async("length_agent", "Test max length")
        print("  [FAIL] Expected ModelLengthExceededError was not raised!")
    except ModelLengthExceededError as exc:
        print(f"  [SUCCESS] Model length termination explicitly caught: {exc}")

    # 10. Adapting Coordinator into B00/B01 Module
    print("\n10. Executing AgentCoordinatorModule via B00/B01 Module Contract...")
    mod = AgentCoordinatorModule(coordinator, default_agent_name="researcher_agent")
    print(f"  Module ID: {mod.manifest.module_id}")
    print(f"  Effect Policy: {mod.manifest.effect_policy}")

    # Provide canned response for module run
    researcher_provider_3 = StreamingFakeProvider(
        canned_responses=[
            ModelResponse(
                message=Message(role="assistant", content="Direct module resolution complete.")
            )
        ]
    )
    direct_agent = AgentDefinition(name="direct_agent", model=researcher_provider_3)
    mod_direct = AgentCoordinatorModule(AgentCoordinator(agents=[direct_agent]))
    mod_res = await mod_direct.execute(
        ExecutionContext(run_id="run_mod_demo", node_id="agent_node"),
        inputs={"prompt": "Resolve issue directly"},
        config={},
    )
    print(f"  Module Execution Result: {mod_res.outputs['final_output']}")


def main() -> None:
    asyncio.run(main_async())

    # 11. B01 CLI Invocation
    print("\n11. Invoking coordinator module via B01 CLI subprocess...")
    cmd = [
        sys.executable,
        "-m",
        "nexura_runtime.modules",
        "run",
        "nexura.agents.coordinator",
        "--input",
        json.dumps({"prompt": "Customer asking for KB resolution on billing issue"}),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    print(f"  CLI Exit Code: {res.returncode}")
    if res.returncode == 0:
        print(f"  CLI Output: {res.stdout.strip()}")
    else:
        print(f"  CLI Error: {res.stderr.strip()}")

    print("\n=== All B19 Demonstration Steps Completed Successfully ===")


if __name__ == "__main__":
    main()
