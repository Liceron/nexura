"""A6 S11 review repro R7: DurableExecutionEngine.claim_run does not fail closed without a budget.

The engine records no budget scope, so a worker constructed without ``budget=`` claims a
run a budgeted worker started and stopped: nothing is refused, the stopped worker's
reservation stays ``active`` forever, and the docs' "continuing a run started under a
budget without any budget raises" does not hold for this takeover path.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from nexura_agents.usage import BudgetConfig, BudgetManager, UsageLedger


class Crash(BaseException):
    pass


async def test_claim_without_budget_is_refused(tmp_path: Path) -> None:
    from nexura_runtime.runtime.execution import (
        DurableExecutionEngine,
        DurableNodeSpec,
        DurableWorkflowSpec,
    )
    from nexura_runtime.storage.repository import SQLiteRepository

    mgr = BudgetManager(ledger=UsageLedger())
    budget = mgr.create_run_budget(
        "ws", "legacy-r7", config=BudgetConfig(max_tokens=10_000), default_model="fake-model"
    )
    repository = SQLiteRepository(str(tmp_path / "legacy.db"))
    spec = DurableWorkflowSpec(
        workflow_id="legacy",
        entry_node="t",
        terminal_nodes=["t"],
        nodes={
            "t": DurableNodeSpec(
                node_id="t", handler="transfer", effect_policy="non_idempotent_write"
            )
        },
    )
    crash = {"now": True}

    async def transfer(state: dict[str, Any]) -> dict[str, Any]:
        budget.admit_provider_call("fake-model", estimated_tokens=50)
        if crash.pop("now", False):
            raise Crash("worker died mid-call")
        return {"done": True}

    first = DurableExecutionEngine(repository, budget=budget)
    first.register_handler("transfer", transfer)
    try:
        await first.init_run("ws", "legacy-r7", spec)
        await first.claim_run("ws", "legacy-r7", "w1", lease_duration=0.2)
        with pytest.raises(Crash):
            await first.execute_step("ws", "legacy-r7", "w1", spec)
        await asyncio.sleep(0.3)
        bare = DurableExecutionEngine(repository)  # no budget
        bare.register_handler("transfer", transfer)
        refused = False
        try:
            await bare.claim_run("ws", "legacy-r7", "w2", lease_duration=5)
        except Exception:
            refused = True
        left = [r for r in mgr.ledger._reservations.values() if r.status == "active"]
        assert refused or not left, (
            f"claimed without a budget; {len(left)} reservation(s) left active"
        )
    finally:
        await repository.close()
