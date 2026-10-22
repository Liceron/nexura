"""Round 2, m1: the terminal sweep now runs BEFORE the terminal commit, and it is not fenced.

A stale owner whose lease was taken over reaches a terminal status: it sweeps the run's
open reservations, including the NEW owner's in-flight one, and only then does its
fenced commit fail. The new owner's real settlement is then discarded ("first settlement
wins") and the run is marked settled_by=execution_terminal while it is still running.
Before this round the sweep ran after the (failing) commit, so a stale owner swept nothing.

Shown on DurableExecutionEngine (no heartbeat: a step that outlives its lease is the
documented stale-owner case). WorkflowEngine._save / record_outcome(terminal) have the same
order (sweep, then journal commit).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from nexura_agents.types import Usage
from nexura_agents.usage import BudgetConfig, BudgetManager, UsageLedger


async def test_stale_owner_terminal_sweep_does_not_settle_the_new_owners_call(
    tmp_path: Path,
) -> None:
    from nexura_runtime.runtime.execution import (
        DurableExecutionEngine,
        DurableNodeSpec,
        DurableWorkflowSpec,
    )
    from nexura_runtime.storage.repository import SQLiteRepository

    mgr = BudgetManager(ledger=UsageLedger())
    budget = mgr.create_run_budget(
        "ws", "legacy-v4", config=BudgetConfig(max_tokens=1_000_000), default_model="fake-model"
    )
    repository = SQLiteRepository(str(tmp_path / "legacy.db"))
    spec = DurableWorkflowSpec(
        workflow_id="legacy",
        entry_node="t",
        terminal_nodes=["t"],
        nodes={"t": DurableNodeSpec(node_id="t", handler="call", effect_policy="idempotent_write")},
    )
    w2_in_flight, release_w2 = asyncio.Event(), asyncio.Event()
    calls = {"n": 0}
    held: dict[str, str] = {}

    async def call(state: dict[str, Any]) -> dict[str, Any]:
        calls["n"] += 1
        me = calls["n"]
        res = budget.admit_provider_call("fake-model", estimated_tokens=50)
        held[f"w{me}"] = res
        if me == 1:
            await asyncio.sleep(0.6)  # w1 outlives its lease (0.3 s): w2 takes over meanwhile
        else:
            w2_in_flight.set()
            await release_w2.wait()
        budget.settle_provider_call(
            res, usage=Usage(prompt_tokens=4000, completion_tokens=1000, total_tokens=5000)
        )
        return {"done": me}

    w1 = DurableExecutionEngine(repository, budget=budget)
    w2 = DurableExecutionEngine(repository, budget=budget)
    w1.register_handler("call", call)
    w2.register_handler("call", call)
    try:
        await w1.init_run("ws", "legacy-v4", spec)
        await w1.claim_run("ws", "legacy-v4", "w1", lease_duration=0.3)
        first = asyncio.create_task(w1.execute_step("ws", "legacy-v4", "w1", spec))
        await asyncio.sleep(0.4)
        await w2.claim_run("ws", "legacy-v4", "w2", lease_duration=30)
        second = asyncio.create_task(w2.execute_step("ws", "legacy-v4", "w2", spec))
        await w2_in_flight.wait()
        stale = await asyncio.gather(first, return_exceptions=True)  # w1 wakes, commit refused
        status_after_stale = mgr.ledger._reservations[held["w2"]].status
        release_w2.set()
        await second
    finally:
        await repository.close()
    assert status_after_stale == "active", (
        f"the stale owner's terminal sweep settled the new owner's in-flight reservation "
        f"({status_after_stale}) before its own commit was refused: {stale}"
    )
