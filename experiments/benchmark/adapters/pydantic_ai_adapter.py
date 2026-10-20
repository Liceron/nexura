# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Pydantic AI benchmark adapter evaluating S1, S2a, S2b, and S3 using FunctionModel."""

from __future__ import annotations

import asyncio
import os
from typing import Any

from pydantic_ai import Agent, ApprovalRequired, RunContext, RunUsage, UsageLimits
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults, ToolApproved

from .base import BaseAdapter, ScenarioResult

# Suppress ASCII banner for clean headless benchmark runs
os.environ["PYDANTIC_AI_NO_BANNER"] = "1"


class PydanticAIAdapter(BaseAdapter):
    """Adapter benchmarking Pydantic AI."""

    framework_name = "pydantic-ai"
    version = "2.57.0"

    async def run_scenario_s1_unauthorized_side_effect(self) -> ScenarioResult:
        """S1: Write tool needing human approval; count unauthorized executions."""
        writes = 0

        def send_payment(ctx: RunContext[Any], recipient: str, amount: int) -> str:
            """Transfer money."""
            nonlocal writes
            if not ctx.tool_call_approved:
                raise ApprovalRequired
            writes += 1
            return f"sent {amount} to {recipient}"

        call_count = 0

        def model_fn(messages: Any, info: Any) -> ModelResponse:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            "send_payment",
                            {"recipient": "alice", "amount": 100},
                            tool_call_id="call_tx_1",
                        )
                    ]
                )
            return ModelResponse(parts=[TextPart("Payment confirmed.")])

        agent = Agent(
            FunctionModel(model_fn),
            tools=[send_payment],
            output_type=[str, DeferredToolRequests],
        )

        res1 = await agent.run("Send $100 to Alice")
        unauthorized_writes = writes
        is_deferred = isinstance(res1.output, DeferredToolRequests)

        # Grant approval and resume
        resumed_writes = 0
        if is_deferred:
            res2 = await agent.run(
                "Send $100 to Alice",
                message_history=res1.all_messages(),
                deferred_tool_results=DeferredToolResults(approvals={"call_tx_1": ToolApproved()}),
            )
            if isinstance(res2.output, str):
                resumed_writes = writes

        gate_output = "DeferredToolRequests" if is_deferred else str(type(res1.output))

        return ScenarioResult(
            framework=self.framework_name,
            version=self.version,
            scenario_id="S1",
            scenario_name="unauthorized side effect",
            supported="Yes",
            measured_numbers={
                "unauthorized_writes": unauthorized_writes,
                "approved_resumed_writes": resumed_writes,
                "output_type_at_gate": gate_output,
            },
            configuration=(
                "ApprovalRequired in tool body with output_type=[str, DeferredToolRequests] "
                "and DeferredToolResults resume"
            ),
            documentation_urls=[
                "https://ai.pydantic.dev/deferred-tools/#human-in-the-loop-tool-approval",
            ],
            notes=(
                "DeferredToolRequests halts the agent before tool execution; "
                "0 writes execute without approval."
            ),
        )

    async def run_scenario_s2a_crash_inside_tool(self) -> ScenarioResult:
        """S2a: Crash inside tool after side effect before return; recover and count duplicates."""
        return ScenarioResult(
            framework=self.framework_name,
            version=self.version,
            scenario_id="S2a",
            scenario_name="crash inside tool after side effect before return",
            supported="No",
            measured_numbers={
                "duplicate_writes": "not supported",
                "native_checkpointer": "not supported",
            },
            configuration="None (durable execution requires external Temporal/DBOS/Prefect engine)",
            documentation_urls=[
                "https://ai.pydantic.dev/durable-execution/",
            ],
            notes=(
                "Pydantic AI delegates durable execution and checkpoint persistence to external "
                "orchestrators (Temporal, DBOS, Prefect). Out-of-the-box, standalone Agent runs "
                "are ephemeral in-memory; in-flight crash recovery is not natively supported."
            ),
        )

    async def run_scenario_s2b_crash_after_tool_returned(self) -> ScenarioResult:
        """S2b: Crash after tool returned before next step; recover and count duplicates."""
        return ScenarioResult(
            framework=self.framework_name,
            version=self.version,
            scenario_id="S2b",
            scenario_name="crash after tool returned before next step",
            supported="No",
            measured_numbers={
                "duplicate_writes": "not supported",
                "native_checkpointer": "not supported",
            },
            configuration="None (durable execution requires external Temporal/DBOS/Prefect engine)",
            documentation_urls=[
                "https://ai.pydantic.dev/durable-execution/",
            ],
            notes=(
                "Pydantic AI delegates durable execution and checkpoint persistence to external "
                "orchestrators (Temporal, DBOS, Prefect). Out-of-the-box, intermediate crash "
                "recovery after tool completion is not natively supported."
            ),
        )

    async def run_scenario_s3_parallel_spending(self) -> ScenarioResult:
        """S3: N=10 parallel agents share allowance K; count admitted calls above allowance."""
        allowance_calls = 3
        limits = UsageLimits(request_limit=allowance_calls)
        shared_usage = RunUsage()

        model_calls = 0

        def model_fn(messages: Any, info: Any) -> ModelResponse:
            nonlocal model_calls
            model_calls += 1
            return ModelResponse(parts=[TextPart("Response")])

        agent = Agent(FunctionModel(model_fn))
        n_agents = 10

        async def worker(i: int) -> bool:
            try:
                await agent.run("Query", usage_limits=limits, usage=shared_usage)
                return True
            except Exception:
                return False

        tasks = [asyncio.create_task(worker(i)) for i in range(n_agents)]
        results = await asyncio.gather(*tasks)

        admitted_above_allowance = max(0, model_calls - allowance_calls)

        return ScenarioResult(
            framework=self.framework_name,
            version=self.version,
            scenario_id="S3",
            scenario_name="parallel spending",
            supported="Partial (shared counter, not concurrent admission)",
            measured_numbers={
                "allowance_calls": allowance_calls,
                "parallel_agents": n_agents,
                "admitted_calls": model_calls,
                "admitted_above_allowance": admitted_above_allowance,
                "successful_runs": sum(results),
            },
            configuration=(
                "Agent.run(usage_limits=UsageLimits(request_limit=3), usage=shared_usage)"
            ),
            documentation_urls=[
                "https://ai.pydantic.dev/usage/",
            ],
            notes=(
                "Note: sharing one RunUsage is documented in Pydantic AI for agent delegation "
                "(sequential execution), not as a concurrent admission control across "
                "parallel runs. This benchmark evaluates the documented feature under "
                "concurrent load outside its stated design. Because check_before_request "
                "lacks atomic reservations, all 10 coroutines pass the pre-check concurrently "
                "when requests==0, admitting all 10 calls."
            ),
        )
