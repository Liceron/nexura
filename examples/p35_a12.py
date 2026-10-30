# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Keyless local fake: automatic budget admission and signed, single-use approval."""

from __future__ import annotations

import asyncio
from decimal import Decimal

from nexura_agents import Agent, FakeModel, Runner, ToolCall, Usage, tool
from nexura_agents.durable import MemoryCheckpointStore
from nexura_agents.errors import ApprovalDecisionError
from nexura_agents.types import RunInterruption
from nexura_agents.usage import BudgetConfig, BudgetManager
from nexura_agents.usage.models import ModelPricing


async def main() -> None:
    manager = BudgetManager()
    manager.pricing_table.add_model_pricing(
        ModelPricing(
            model_id="fake-model",
            input_cost_per_million=Decimal("0"),
            output_cost_per_million=Decimal("0"),
            source="explicit local fake fixture",
        )
    )
    budget = manager.create_run_budget("local", "demo", config=BudgetConfig(max_tokens=20000))
    store = MemoryCheckpointStore()  # use SQLiteCheckpointStore for process durability
    # Local example key only. Real apps load the same secret securely across restarts.
    secret = b"local-offline-example-signing-key"
    ran: list[int] = []

    @tool(requires_approval=True)
    async def record(amount: int) -> str:
        """Record a local integer; no network or payment calls."""
        ran.append(amount)
        return "recorded"

    agent = Agent(
        name="Local",
        instructions="",
        tools=[record],
        model=FakeModel(
            responses=[[ToolCall("c", "record", {"amount": 5})], "done"],
            default_usage=Usage(prompt_tokens=5, completion_tokens=5, total_tokens=10),
        ),
    )
    runner = Runner(budget=budget, checkpoint_store=store, approval_signing_secret=secret)
    paused = await runner.run_async(agent, "go")
    assert paused.interruption is not None and ran == []
    reviewed = paused.interruption.pending_digests()  # retain when showing the reviewer
    carried = RunInterruption.from_json(paused.interruption.to_json())
    resumed = Runner(budget=budget, checkpoint_store=store, approval_signing_secret=secret)
    result = await resumed.resume_async(agent, carried, approve=reviewed)
    assert result.output == "done" and ran == [5]
    try:
        await resumed.resume_async(agent, carried, approve=True)
    except ApprovalDecisionError as exc:
        assert "consumed" in str(exc)
    else:
        raise AssertionError("replay was accepted")
    status = budget.get_status()
    assert status.total_tokens == 20 and status.total_reserved_tokens == 0
    print("Local fake: 20 tokens settled; one approved call; replay refused.")


if __name__ == "__main__":
    asyncio.run(main())
