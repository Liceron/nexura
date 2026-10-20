"""Comprehensive acceptance and unit tests for Module B19: Agents, Subagents and Handoffs."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from nexura_runtime.contracts.context import ExecutionContext
from nexura_runtime.providers.fake import StreamingFakeProvider
from nexura_runtime.tools import tool
from nexura_runtime.types import Message, ModelResponse, ToolCall

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
from nexura_runtime.runtime.approvals.errors import ApprovalRequiredError
from nexura_runtime.runtime.approvals.gate import ApprovalGate
from nexura_runtime.runtime.approvals.manager import ApprovalPolicyManager
from nexura_runtime.runtime.approvals.models import ApprovalDecisionRecord


class TestAgentsSubagentsHandoffsAcceptance:
    """Acceptance tests verifying multi-agent coordination, subagents, and safety boundaries."""

    @pytest.mark.asyncio
    async def test_recursive_handoffs_stop_at_depth_bound(self) -> None:
        """Recursive handoffs must be halted when exceeding configured depth bound."""
        # Setup infinite ping-pong between agent_a and agent_b
        provider_a = StreamingFakeProvider(
            canned_responses=[
                ModelResponse(
                    message=Message(
                        role="assistant",
                        tool_calls=[
                            ToolCall(
                                id="call_to_b",
                                name="transfer_to_agent_b",
                                arguments={"reason": "Transferring to B"},
                            )
                        ],
                    )
                )
                for _ in range(10)
            ]
        )
        provider_b = StreamingFakeProvider(
            canned_responses=[
                ModelResponse(
                    message=Message(
                        role="assistant",
                        tool_calls=[
                            ToolCall(
                                id="call_to_a",
                                name="transfer_to_agent_a",
                                arguments={"reason": "Transferring to A"},
                            )
                        ],
                    )
                )
                for _ in range(10)
            ]
        )

        agent_a = AgentDefinition(
            name="agent_a",
            model=provider_a,
            handoffs=[HandoffTarget(target="agent_b")],
        )
        agent_b = AgentDefinition(
            name="agent_b",
            model=provider_b,
            handoffs=[HandoffTarget(target="agent_a")],
        )

        # Allow max 2 handoffs: a -> b (depth 1), b -> a (depth 2), a -> b (depth 3 exceeds max 2)
        coord = AgentCoordinator(
            agents=[agent_a, agent_b],
            max_handoff_depth=2,
            total_fuel=20,
        )

        with pytest.raises(MaxHandoffDepthExceededError) as exc_info:
            await coord.run_async("agent_a", "Start recursive loop")

        err = exc_info.value
        assert err.current_depth == 3
        assert err.max_depth == 2
        assert err.agent_name == "agent_a"
        assert err.target_agent == "agent_b"

    @pytest.mark.asyncio
    async def test_handoffs_stop_at_fuel_bound(self) -> None:
        """Coordinated executions must halt when global turn fuel budget is exhausted."""
        provider_a = StreamingFakeProvider(
            canned_responses=[
                ModelResponse(
                    message=Message(
                        role="assistant",
                        tool_calls=[
                            ToolCall(
                                id="call_to_b",
                                name="transfer_to_agent_b",
                                arguments={"reason": "Transferring to B"},
                            )
                        ],
                    )
                )
                for _ in range(10)
            ]
        )
        provider_b = StreamingFakeProvider(
            canned_responses=[
                ModelResponse(
                    message=Message(
                        role="assistant",
                        tool_calls=[
                            ToolCall(
                                id="call_to_a",
                                name="transfer_to_agent_a",
                                arguments={"reason": "Transferring to A"},
                            )
                        ],
                    )
                )
                for _ in range(10)
            ]
        )

        agent_a = AgentDefinition(
            name="agent_a",
            model=provider_a,
            handoffs=[HandoffTarget(target="agent_b")],
        )
        agent_b = AgentDefinition(
            name="agent_b",
            model=provider_b,
            handoffs=[HandoffTarget(target="agent_a")],
        )

        # Allow max 10 depth, but set fuel to 2 turns
        coord = AgentCoordinator(
            agents=[agent_a, agent_b],
            max_handoff_depth=10,
            total_fuel=2,
        )

        with pytest.raises(HandoffFuelExhaustedError) as exc_info:
            await coord.run_async("agent_a", "Start loop until fuel runs out")

        err = exc_info.value
        assert err.consumed_fuel == 2
        assert err.fuel_budget == 2
        assert err.agent_name == "agent_a"

    @pytest.mark.asyncio
    async def test_parallel_child_histories_isolated(self) -> None:
        """Parallel child subagent runs must have completely isolated message histories."""

        @tool
        def tool_alpha(x: int) -> int:
            return x * 10

        @tool
        def tool_beta(x: int) -> int:
            return x + 100

        # Subagent 1 uses tool_alpha
        sub_provider_1 = StreamingFakeProvider(
            canned_responses=[
                ModelResponse(
                    message=Message(
                        role="assistant",
                        tool_calls=[ToolCall(id="tc_1", name="tool_alpha", arguments={"x": 5})],
                    )
                ),
                ModelResponse(message=Message(role="assistant", content="Alpha result: 50")),
            ]
        )
        # Subagent 2 uses tool_beta
        sub_provider_2 = StreamingFakeProvider(
            canned_responses=[
                ModelResponse(
                    message=Message(
                        role="assistant",
                        tool_calls=[ToolCall(id="tc_2", name="tool_beta", arguments={"x": 7})],
                    )
                ),
                ModelResponse(message=Message(role="assistant", content="Beta result: 107")),
            ]
        )

        child_1 = AgentDefinition(
            name="child_1",
            instructions="Child 1 instructions",
            model=sub_provider_1,
            tools=[tool_alpha],
        )
        child_2 = AgentDefinition(
            name="child_2",
            instructions="Child 2 instructions",
            model=sub_provider_2,
            tools=[tool_beta],
        )

        tool_sub_1 = child_1.as_tool()
        tool_sub_2 = child_2.as_tool()

        # Execute parallel runs concurrently via asyncio.gather
        res_1, res_2 = await asyncio.gather(
            tool_sub_1.arun(query="Run child 1 task"),
            tool_sub_2.arun(query="Run child 2 task"),
        )

        assert res_1 == "Alpha result: 50"
        assert res_2 == "Beta result: 107"

        # Verify recorded requests in providers did not bleed histories
        req_1_msgs = sub_provider_1.recorded_requests[1].messages
        req_2_msgs = sub_provider_2.recorded_requests[1].messages

        # Child 1 history has tool_alpha, never tool_beta
        assert any(m.name == "tool_alpha" for m in req_1_msgs)
        assert not any(m.name == "tool_beta" for m in req_1_msgs)

        # Child 2 history has tool_beta, never tool_alpha
        assert any(m.name == "tool_beta" for m in req_2_msgs)
        assert not any(m.name == "tool_alpha" for m in req_2_msgs)

    @pytest.mark.asyncio
    async def test_parent_cancellation_prevents_later_child_tools(self) -> None:
        """Parent cancellation must immediately prevent any subsequent child tool calls."""
        tool_calls_counter = {"executed": 0}

        @tool
        def side_effect_tool(param: str) -> str:
            tool_calls_counter["executed"] += 1
            return f"Processed {param}"

        provider = StreamingFakeProvider(
            canned_responses=[
                ModelResponse(
                    message=Message(
                        role="assistant",
                        tool_calls=[
                            ToolCall(
                                id="tc_side_effect",
                                name="side_effect_tool",
                                arguments={"param": "critical_data"},
                            )
                        ],
                    )
                )
            ]
        )

        agent = AgentDefinition(
            name="sensitive_agent",
            model=provider,
            tools=[side_effect_tool],
        )

        coord = AgentCoordinator(agents=[agent])

        # Create parent context and mark cancelled before tool execution
        parent_ctx = ExecutionContext(
            workspace_id="ws_cancel",
            run_id="run_parent_123",
            node_id="parent_node",
            is_cancelled=True,
        )

        with pytest.raises(ParentCancelledError) as exc_info:
            await coord.run_async(
                "sensitive_agent", "Execute critical operation", context=parent_ctx
            )

        err = exc_info.value
        assert err.parent_run_id == "run_parent_123"
        assert err.child_agent_name == "sensitive_agent"
        # Tool MUST NOT have executed: zero side effects!
        assert tool_calls_counter["executed"] == 0

    def test_restricted_child_cannot_escalate_capabilities_registration(self) -> None:
        """AgentDefinition must reject registration of tools outside allowed_tools."""

        @tool
        def read_tool() -> str:
            return "read"

        @tool
        def delete_tool() -> str:
            return "delete"

        provider = StreamingFakeProvider(canned_responses=["ok"])

        with pytest.raises(CapabilityEscalationError) as exc_info:
            AgentDefinition(
                name="escalation_tester",
                model=provider,
                tools=[read_tool, delete_tool],
                allowed_tools=["read_tool"],  # delete_tool is forbidden!
            )

        err = exc_info.value
        assert err.agent_name == "escalation_tester"
        assert err.attempted_tool == "delete_tool"
        assert err.allowed_tools == ["read_tool"]

    @pytest.mark.asyncio
    async def test_restricted_child_cannot_escalate_capabilities_runtime(self) -> None:
        """Agent invocation must block model from calling forbidden tools at runtime."""

        @tool
        def safe_query(query: str) -> str:
            return f"safe result: {query}"

        # Model requests unauthorized tool call
        provider = StreamingFakeProvider(
            canned_responses=[
                ModelResponse(
                    message=Message(
                        role="assistant",
                        tool_calls=[
                            ToolCall(
                                id="tc_forbidden",
                                name="unauthorized_exec",
                                arguments={"cmd": "drop table"},
                            )
                        ],
                    )
                )
            ]
        )

        agent = AgentDefinition(
            name="restricted_agent",
            model=provider,
            tools=[safe_query],
            allowed_tools=["safe_query"],
        )

        coord = AgentCoordinator(agents=[agent])

        with pytest.raises(CapabilityEscalationError) as exc_info:
            await coord.run_async("restricted_agent", "Run unauthorized exec")

        err = exc_info.value
        assert err.agent_name == "restricted_agent"
        assert err.attempted_tool == "unauthorized_exec"
        assert err.allowed_tools == ["safe_query"]

    @pytest.mark.asyncio
    async def test_model_refusal_termination_is_explicit(self) -> None:
        """Model refusal (safety or policy rejection) must raise explicit ModelRefusalError."""
        provider = StreamingFakeProvider(
            canned_responses=[
                ModelResponse(
                    message=Message(
                        role="assistant", content="Request rejected due to safety policy."
                    ),
                    finish_reason="refusal",
                )
            ]
        )
        agent = AgentDefinition(name="guarded_agent", model=provider)
        coord = AgentCoordinator(agents=[agent])

        with pytest.raises(ModelRefusalError) as exc_info:
            await coord.run_async("guarded_agent", "Unsafe prompt")

        err = exc_info.value
        assert err.agent_name == "guarded_agent"
        assert "safety policy" in err.refusal_reason.lower()

    @pytest.mark.asyncio
    async def test_model_length_termination_is_explicit(self) -> None:
        """Premature termination due to length/tokens must raise ModelLengthExceededError."""
        provider = StreamingFakeProvider(
            canned_responses=[
                ModelResponse(
                    message=Message(role="assistant", content="Incomplete generation truncated..."),
                    finish_reason="length",
                )
            ]
        )
        agent = AgentDefinition(name="verbose_agent", model=provider)
        coord = AgentCoordinator(agents=[agent])

        with pytest.raises(ModelLengthExceededError) as exc_info:
            await coord.run_async("verbose_agent", "Long prompt")

        err = exc_info.value
        assert err.agent_name == "verbose_agent"

    @pytest.mark.asyncio
    async def test_agent_as_tool_cannot_bypass_approval_gating(self, tmp_path: Path) -> None:
        """Agent-as-tool with approval_required=True cannot execute without claimed approval."""
        mgr = ApprovalPolicyManager(tmp_path / "approvals.db")
        gate = ApprovalGate(mgr)

        provider = StreamingFakeProvider(
            canned_responses=[
                ModelResponse(
                    message=Message(role="assistant", content="Sensitive operation completed.")
                )
            ]
        )
        sensitive_agent = AgentDefinition(
            name="banking_operator",
            model=provider,
            approval_required=True,
        )

        agent_tool = sensitive_agent.as_tool(approval_gate=gate)

        parent_ctx = ExecutionContext(
            workspace_id="ws_bank",
            run_id="run_parent_bank",
            node_id="transfer_node",
        )

        from nexura_runtime.runtime.agents.definition import current_agent_context

        token = current_agent_context.set(parent_ctx)
        try:
            # 1. First invocation without approval: must raise ApprovalRequiredError
            with pytest.raises(ApprovalRequiredError) as exc_info:
                await agent_tool.arun(query="Transfer $50,000")

            req_id = exc_info.value.approval_id
            assert req_id is not None
            assert exc_info.value.action_name == "delegate_to_banking_operator"

            # 2. Approve request via manager
            dec = ApprovalDecisionRecord(
                workspace_id="ws_bank",
                approval_id=req_id,
                decided_by_principal_id="admin_user",
                decided_by_role="admin",
                decision="approved",
                expected_payload_digest=exc_info.value.payload_digest,
            )
            mgr.submit_decision("ws_bank", req_id, dec)

            # 3. Second invocation with approved approval_id passed in context data
            approved_res = await agent_tool.arun(
                query="Transfer $50,000",
                context_data={"approval_id": req_id},
            )
            # Since approval was claimed by gate
            assert "completed" in approved_res
        finally:
            current_agent_context.reset(token)

    @pytest.mark.asyncio
    async def test_agent_coordinator_module_contract_and_traces(self) -> None:
        """AgentCoordinatorModule must conform to B00/B01 contracts and produce valid traces."""
        provider = StreamingFakeProvider(
            canned_responses=[
                ModelResponse(
                    message=Message(role="assistant", content="Coordinated multi-agent answer.")
                )
            ]
        )
        agent = AgentDefinition(name="support_agent", model=provider)
        coord = AgentCoordinator(agents=[agent])
        mod = AgentCoordinatorModule(coord, module_id="nexura.agents.test_coord")

        assert mod.manifest.kind == "agent"
        assert mod.manifest.module_id == "nexura.agents.test_coord"
        assert mod.manifest.effect_policy == "idempotent_write"

        ctx = ExecutionContext(workspace_id="ws_mod", run_id="run_mod_1", node_id="agent_step")
        res = await mod.execute(ctx, {"prompt": "Inquire support"}, config={})

        assert res.status == "success"
        assert res.outputs["final_output"] == "Coordinated multi-agent answer."
        assert res.outputs["final_agent"] == "support_agent"
        assert len(res.outputs["traces"]) == 1
        assert res.outputs["traces"][0]["agent_name"] == "support_agent"
