# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Nexura benchmark adapter testing S1, S2a, S2b, and S3."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from nexura_agents import (
    Agent,
    FakeModel,
    MemoryCheckpointStore,
    Runner,
    ToolCall,
    Usage,
    tool,
)
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.providers.base import BaseProvider
from nexura_agents.providers.types import GenerationRequest, ProviderCapabilities
from nexura_agents.types import ModelResponse
from nexura_agents.usage import BudgetConfig, BudgetManager
from nexura_agents.usage.errors import BudgetExceededError

from .base import BaseAdapter, ScenarioResult


class _CrashError(BaseException):
    """Simulated process crash for durability benchmarking."""


class _ScriptedFakeProvider(BaseProvider):
    """Offline test model respecting Nexura provider admission boundaries."""

    def __init__(self, token_cost: int = 20) -> None:
        super().__init__("gpt-4o", ProviderCapabilities(can_generate=True, can_tools=True))
        self.calls = 0
        self.token_cost = token_cost
        self.fake = FakeModel(
            responses=["done"],
            default_usage=Usage(
                prompt_tokens=token_cost // 2,
                completion_tokens=token_cost // 2,
                total_tokens=token_cost,
            ),
        )

    def _estimate_tokens(self, req: GenerationRequest) -> int:
        return self.token_cost

    async def _generate_internal(
        self, request: GenerationRequest, context: ExecutionContext | None
    ) -> ModelResponse:
        self.calls += 1
        return await self.fake.generate(request.messages)

    async def _stream_internal(
        self, request: GenerationRequest, context: ExecutionContext | None
    ) -> Any:
        raise NotImplementedError

    async def _embed_internal(self, texts: list[str], context: ExecutionContext | None) -> Any:
        raise NotImplementedError


class NexuraAdapter(BaseAdapter):
    """Adapter benchmarking Nexura standalone SDK."""

    framework_name = "nexura"
    version = "0.4.0"

    async def run_scenario_s1_unauthorized_side_effect(self) -> ScenarioResult:
        """S1: Write tool needing human approval; count unauthorized executions."""
        writes = 0

        @tool(requires_approval=True)
        def send_payment(recipient: str, amount: int) -> str:
            """Transfer money."""
            nonlocal writes
            writes += 1
            return f"sent {amount} to {recipient}"

        model = FakeModel(
            responses=[
                [
                    ToolCall(
                        id="c1",
                        name="send_payment",
                        arguments={"recipient": "alice", "amount": 100},
                    )
                ],
                "Payment completed.",
            ]
        )
        agent = Agent(
            name="PaymentAgent",
            instructions="Handle payments safely.",
            model=model,
            tools=[send_payment],
        )
        runner = Runner()
        paused = await runner.run_async(agent, "Send $100 to Alice")

        unauthorized_writes = writes
        resumed_writes = 0
        if paused.status == "paused" and paused.interruption is not None:
            resumed = await runner.resume_async(
                agent,
                paused.interruption,
                approve=paused.interruption.pending_digests(),
            )
            if resumed.status == "completed":
                resumed_writes = writes

        return ScenarioResult(
            framework=self.framework_name,
            version=self.version,
            scenario_id="S1",
            scenario_name="unauthorized side effect",
            supported="Yes",
            measured_numbers={
                "unauthorized_writes": unauthorized_writes,
                "approved_resumed_writes": resumed_writes,
                "paused_at_gate": paused.status == "paused",
            },
            configuration="@tool(requires_approval=True) with Runner.run_async and resume_async",
            documentation_urls=["docs/approvals.md"],
            notes="Tool execution is gated before invocation; 0 writes execute without approval.",
        )

    async def run_scenario_s2a_crash_inside_tool(self) -> ScenarioResult:
        """S2a: Crash inside tool after side effect before return; recover and count duplicates."""
        writes = 0
        crashed = False

        @tool(idempotent=False)
        def send_payment(recipient: str, amount: int) -> str:
            """Transfer money."""
            nonlocal writes, crashed
            writes += 1
            if not crashed:
                crashed = True
                raise _CrashError("Crash inside tool after side effect, before return")
            return f"sent {amount} to {recipient}"

        store = MemoryCheckpointStore()
        model = FakeModel(
            responses=[
                [
                    ToolCall(
                        id="call_tx_1",
                        name="send_payment",
                        arguments={"recipient": "bob", "amount": 250},
                    )
                ],
                "Payment done.",
            ]
        )
        agent = Agent(
            name="TransferAgent",
            instructions="Send payments.",
            model=model,
            tools=[send_payment],
        )
        runner = Runner(checkpoint_store=store)

        with contextlib.suppress(_CrashError):
            await runner.run_async(agent, "Transfer $250 to Bob")

        writes_at_crash = writes
        run_ids = await store.list_incomplete()
        run_id = run_ids[0]

        # Fresh agent and runner recovering from checkpoint store
        recovered_agent = Agent(
            name="TransferAgent",
            instructions="Send payments.",
            model=FakeModel(responses=["Transfer finished."]),
            tools=[send_payment],
        )
        recovered_runner = Runner(checkpoint_store=store)
        recovery_result = await recovered_runner.recover_async(recovered_agent, run_id)

        # Nexura refuses to re-run non-idempotent in-flight tools; pauses for review
        duplicate_writes = writes - writes_at_crash
        reason = recovery_result.interruption.reason if recovery_result.interruption else None

        return ScenarioResult(
            framework=self.framework_name,
            version=self.version,
            scenario_id="S2a",
            scenario_name="crash inside tool after side effect before return",
            supported="Yes",
            measured_numbers={
                "writes_before_crash": writes_at_crash,
                "total_writes_after_recovery": writes,
                "duplicate_writes": duplicate_writes,
                "recovered_status": recovery_result.status,
                "interruption_reason": reason,
            },
            configuration="Runner(checkpoint_store=MemoryCheckpointStore()) with recover_async",
            documentation_urls=["docs/durable-runs.md"],
            notes=(
                "Nexura flags non-idempotent in-flight tools as outcome_unknown_after_crash. "
                "Instead of blindly re-running, it halts in status='paused' and requests "
                "human review; 0 duplicate writes execute."
            ),
        )

    async def run_scenario_s2b_crash_after_tool_returned(self) -> ScenarioResult:
        """S2b: Crash after tool returned before next step; recover and count duplicates."""
        writes = 0

        @tool(idempotent=False)
        def send_payment(recipient: str, amount: int) -> str:
            """Transfer money."""
            nonlocal writes
            writes += 1
            return f"sent {amount} to {recipient}"

        seen_calls = 0

        def crashing_handler(messages: Any, tools: Any) -> Any:
            nonlocal seen_calls
            seen_calls += 1
            if seen_calls == 1:
                return [
                    ToolCall(
                        id="call_tx_1",
                        name="send_payment",
                        arguments={"recipient": "bob", "amount": 250},
                    )
                ]
            if seen_calls == 2:
                # Tool returned successfully; process crashes before next step
                raise _CrashError("Crash after tool returned before next step")
            return "All done."

        store = MemoryCheckpointStore()
        agent = Agent(
            name="TransferAgent",
            instructions="Send payments.",
            model=FakeModel(handler=crashing_handler),
            tools=[send_payment],
        )
        runner = Runner(checkpoint_store=store)

        with contextlib.suppress(_CrashError):
            await runner.run_async(agent, "Transfer $250 to Bob")

        writes_at_crash = writes
        run_ids = await store.list_incomplete()
        run_id = run_ids[0]

        recovered_agent = Agent(
            name="TransferAgent",
            instructions="Send payments.",
            model=FakeModel(responses=["Transfer finished."]),
            tools=[send_payment],
        )
        recovered_runner = Runner(checkpoint_store=store)
        recovery_result = await recovered_runner.recover_async(recovered_agent, run_id)

        duplicate_writes = writes - writes_at_crash

        return ScenarioResult(
            framework=self.framework_name,
            version=self.version,
            scenario_id="S2b",
            scenario_name="crash after tool returned before next step",
            supported="Yes",
            measured_numbers={
                "writes_before_crash": writes_at_crash,
                "total_writes_after_recovery": writes,
                "duplicate_writes": duplicate_writes,
                "recovered_status": recovery_result.status,
            },
            configuration="Runner(checkpoint_store=MemoryCheckpointStore()) with recover_async",
            documentation_urls=["docs/durable-runs.md"],
            notes=(
                "Nexura commits completed tool outputs to checkpoint; recovery reuses the recorded "
                "output without re-executing the non-idempotent write."
            ),
        )

    async def run_scenario_s3_parallel_spending(self) -> ScenarioResult:
        """S3: N=10 parallel agents share allowance K; count admitted calls above allowance."""
        allowance_calls = 3
        token_cost_per_call = 20
        max_tokens = allowance_calls * token_cost_per_call

        budget_manager = BudgetManager()
        root_budget = budget_manager.create_run_budget(
            workspace_id="ws_benchmark",
            run_id="shared_run",
            config=BudgetConfig(max_tokens=max_tokens),
        )

        n_agents = 10
        child_budgets = [root_budget.create_child_budget(f"worker_{i}") for i in range(n_agents)]
        providers = [_ScriptedFakeProvider(token_cost=token_cost_per_call) for _ in child_budgets]
        agents = [
            Agent(name=f"agent_{i}", instructions="Answer briefly.", model=providers[i])
            for i in range(n_agents)
        ]

        async def worker(i: int) -> bool:
            runner = Runner(budget=child_budgets[i])
            try:
                await runner.run_async(agents[i], "Run query")
                return True
            except BudgetExceededError:
                return False

        tasks = [asyncio.create_task(worker(i)) for i in range(n_agents)]
        results = await asyncio.gather(*tasks)

        admitted_calls = sum(p.calls for p in providers)
        admitted_above_allowance = max(0, admitted_calls - allowance_calls)

        return ScenarioResult(
            framework=self.framework_name,
            version=self.version,
            scenario_id="S3",
            scenario_name="parallel spending",
            supported="Yes (shared allowance)",
            measured_numbers={
                "allowance_calls": allowance_calls,
                "parallel_agents": n_agents,
                "admitted_calls": admitted_calls,
                "admitted_above_allowance": admitted_above_allowance,
                "successful_runs": sum(results),
            },
            configuration=(
                "BudgetManager.create_run_budget with child budgets and Runner(budget=...)"
            ),
            documentation_urls=["docs/budgets-and-usage.md"],
            notes=(
                "Atomic admission reservations ensure no calls above the shared allowance are "
                "dispatched to provider models."
            ),
        )
