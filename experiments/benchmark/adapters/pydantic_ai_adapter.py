# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Pydantic AI benchmark adapter evaluating S1, S2a, S2b, and S3 using FunctionModel."""

from __future__ import annotations

import asyncio
import importlib.metadata
import os
from decimal import Decimal
from typing import Any

from pydantic_ai import Agent, ApprovalRequired, RunContext, RunUsage, UsageLimits
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults, ToolApproved
from pydantic_ai.usage import RequestUsage
from pydantic_ai_harness.spend import Budget, SpendLimits

from .base import BaseAdapter, ScenarioResult
from .spending import (
    LIMIT_CALLS,
    LIMIT_USD,
    MODEL_LATENCY_S,
    N_AGENTS,
    Mode,
    SpendingCase,
    make_spending_result,
    tokens_cost_usd,
)

# Suppress ASCII banner for clean headless benchmark runs
os.environ["PYDANTIC_AI_NO_BANNER"] = "1"


class PydanticAIAdapter(BaseAdapter):
    """Adapter benchmarking Pydantic AI."""

    framework_name = "pydantic-ai"

    def __init__(self) -> None:
        self.version = importlib.metadata.version("pydantic-ai")
        self.harness_version = importlib.metadata.version("pydantic-ai-harness")

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
            scenario_id="S3-legacy",
            scenario_name="parallel spending (legacy P33 setup, superseded by S3a-c)",
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

    @staticmethod
    def _priced_model(case: SpendingCase, counter: list[int], *, with_cost: bool) -> FunctionModel:
        """Fake model reporting the case's usage; optionally with the response cost set.

        ``FunctionModel`` names are unknown to genai-prices, so ``UsageLimits.cost_limit`` can
        only see a cost the response carries (``fill_response_cost`` never overwrites one).
        ``SpendLimits`` prices through its documented ``price=`` hook instead.
        """

        async def model_fn(messages: Any, info: Any) -> ModelResponse:
            counter[0] += 1
            await asyncio.sleep(MODEL_LATENCY_S)  # keep every admitted call in flight together
            usage = RequestUsage(input_tokens=case.input_tokens, output_tokens=case.output_tokens)
            if with_cost:
                usage.cost = case.actual_cost_usd
            return ModelResponse(parts=[TextPart("Response")], usage=usage)

        return FunctionModel(model_fn, model_name="bench-fake-model")

    async def _run_workers(self, mode: Mode, run_one: Any, counter: list[int]) -> list[str]:
        async def worker(i: int) -> str:
            calls_before = counter[0]
            try:
                await run_one(i)
                return "ok"
            except UsageLimitExceeded:  # SpendLimitExceeded subclasses UsageLimitExceeded
                return "refused" if counter[0] == calls_before else "errored_after_call"

        if mode == "parallel":
            return list(await asyncio.gather(*(worker(i) for i in range(N_AGENTS))))
        return [await worker(i) for i in range(N_AGENTS)]

    async def run_spending(self, case: SpendingCase, mode: Mode) -> list[ScenarioResult]:
        """Fair S3: UsageLimits with a shared RunUsage, and harness SpendLimits."""
        results: list[ScenarioResult] = []

        # 1. Core UsageLimits. A limit applies to one run; the documented way to make several
        #    agents count against one allowance is to pass the same `usage` object
        #    (agent delegation). Request and USD caps are both set.
        counter = [0]
        limits = UsageLimits(request_limit=LIMIT_CALLS, cost_limit=LIMIT_USD)
        shared_usage = RunUsage()
        agent = Agent(self._priced_model(case, counter, with_cost=True))

        async def run_usage_limits(i: int) -> None:
            await agent.run("Query", usage_limits=limits, usage=shared_usage)

        outcomes = await self._run_workers(mode, run_usage_limits, counter)
        results.append(
            make_spending_result(
                framework=self.framework_name,
                version=self.version,
                feature="UsageLimits + shared RunUsage",
                case=case,
                mode=mode,
                admitted_calls=counter[0],
                refused_runs=outcomes.count("refused"),
                errored_after_call_runs=outcomes.count("errored_after_call"),
                supported="Partial (per-run limit; sharing is the delegation pattern)",
                configuration=(
                    f"agent.run(usage_limits=UsageLimits(request_limit={LIMIT_CALLS}, "
                    f"cost_limit=Decimal('{LIMIT_USD}')), usage=shared_run_usage)"
                ),
                documentation_urls=[
                    "https://pydantic.dev/docs/ai/core-concepts/agent/#usage-limits",
                    "https://pydantic.dev/docs/ai/guides/multi-agent-applications/#agent-delegation",
                ],
                notes=(
                    "UsageLimits checks the request count and cost before each request and the "
                    "cost again after each response. It reserves nothing, so parallel runs all "
                    "pass the pre-check while the shared counter is still zero. A run whose "
                    "response pushes the shared cost over the limit raises UsageLimitExceeded "
                    "after the call was already made and paid for. The docs say: 'Don't rely on "
                    "cost_limit as a hard billing guarantee.'"
                ),
                framework_recorded_spend_usd=(
                    Decimal(str(shared_usage.cost)) if shared_usage.cost is not None else None
                ),
            )
        )

        # 2. pydantic-ai-harness SpendLimits: a USD budget on a store shared by every run of the
        #    agent (the default InMemorySpendStore belongs to the capability instance).
        counter = [0]

        def price(response: ModelResponse) -> Decimal:
            return tokens_cost_usd(response.usage.input_tokens + response.usage.output_tokens)

        spend_limits: SpendLimits[None] = SpendLimits(
            budgets=[Budget(usd=LIMIT_USD, window="total")],
            price=price,
            on_unpriced="raise",
        )
        spend_agent = Agent(
            self._priced_model(case, counter, with_cost=False), capabilities=[spend_limits]
        )

        async def run_spend_limits(i: int) -> None:
            await spend_agent.run("Query")

        outcomes = await self._run_workers(mode, run_spend_limits, counter)
        statuses = await spend_limits.status()
        recorded = statuses[0].spent.usd if statuses else None
        results.append(
            make_spending_result(
                framework=self.framework_name,
                version=f"{self.version} (pydantic-ai-harness=={self.harness_version})",
                feature="SpendLimits (harness)",
                case=case,
                mode=mode,
                admitted_calls=counter[0],
                refused_runs=outcomes.count("refused"),
                errored_after_call_runs=outcomes.count("errored_after_call"),
                supported="Partial (documented as a brake, not a reservation)",
                configuration=(
                    "Agent(..., capabilities=[SpendLimits(budgets=[Budget("
                    f"usd=Decimal('{LIMIT_USD}'), window='total')], price=..., "
                    "on_unpriced='raise')])"
                ),
                documentation_urls=[
                    "https://pydantic.dev/docs/ai/harness/spend/",
                ],
                notes=(
                    "SpendLimits records each priced response and refuses the next request once "
                    "a window is spent. Its docs say: 'The request that crosses the line "
                    "completes, and concurrent runs can each pass the check before any of them "
                    "records anything' and 'Admission is all that call does: it reserves "
                    "nothing.'"
                ),
                framework_recorded_spend_usd=Decimal(str(recorded))
                if recorded is not None
                else None,
            )
        )
        return results
