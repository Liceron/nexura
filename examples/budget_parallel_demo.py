# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Ten offline FakeModel agents share one admission allowance, explicitly reserved."""

from __future__ import annotations

import asyncio

from nexura_agents import Agent, BudgetConfig, BudgetManager, FakeModel, Runner, Usage
from nexura_agents.usage.errors import BudgetExceededError


async def main() -> None:
    budget = BudgetManager().create_run_budget(
        "offline-demo",
        "ten-agents",
        config=BudgetConfig(max_tokens=30),
    )
    refused = 0

    def show(stage: str) -> None:
        status = budget.get_status()
        print(
            f"{stage}: spent={status.total_tokens} tokens / ${status.total_spent_usd:f}; "
            f"reserved={status.total_reserved_tokens} tokens / ${status.total_reserved_usd:f}; "
            f"remaining={status.remaining_tokens} tokens; refused={refused}"
        )

    async def worker(index: int) -> None:
        nonlocal refused
        child = budget.create_child_budget(f"agent-{index}")
        try:
            reservation = await child.admit_provider_call_async("gpt-4o", estimated_tokens=10)
        except BudgetExceededError:
            refused += 1
            return
        try:
            # Manual admission demo: the Runner below has no budget, avoiding double admission.
            await asyncio.sleep(0)
            agent = Agent(
                name=f"Agent {index}",
                instructions="Offline scripted response.",
                model=FakeModel(
                    responses=["done"],
                    default_usage=Usage(
                        prompt_tokens=5,
                        completion_tokens=5,
                        total_tokens=10,
                    ),
                ),
            )
            result = await Runner().run_async(agent, "Work")
        except BaseException:
            child.settle_provider_call(reservation, is_cancelled=True)
            raise
        else:
            child.settle_provider_call(reservation, usage=result.usage)

    tasks = [asyncio.create_task(worker(i)) for i in range(10)]
    await asyncio.sleep(0)
    show("in flight")
    await asyncio.gather(*tasks)
    show("settled")
    assert refused == 7
    assert budget.get_status().total_tokens == 30
    assert budget.get_status().total_reserved_tokens == 0


if __name__ == "__main__":
    asyncio.run(main())
