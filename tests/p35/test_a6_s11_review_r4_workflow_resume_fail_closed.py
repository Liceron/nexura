"""A6 S11 review repro R4 (check): a gated workflow resumed WITHOUT a budget is refused before the
approval is
consumed, so the same approval still works with the budget restored."""

from __future__ import annotations

import pytest
from nexura_agents.errors import WorkflowError
from nexura_agents.usage import BudgetConfig, BudgetManager, UsageLedger

from nexura_runtime.workflow import (
    MemoryWorkflowStore,
    ModuleRegistry,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
)


def spec() -> WorkflowSpec:
    return WorkflowSpec(
        name="r4",
        entry_node="a",
        nodes={
            "a": WorkflowNode(id="a", handler="note", default_transition="b"),
            "b": WorkflowNode(id="b", handler="pay", requires_approval=True),
        },
        terminal_nodes=["b"],
    )


def registry(ran: list[str]) -> ModuleRegistry:
    reg = ModuleRegistry()
    reg.register("note", lambda s: ran.append("note") or {"n": 1}, effect="idempotent")
    reg.register("pay", lambda s: ran.append("pay") or {"p": 1})
    return reg


async def test_resume_without_budget_refused_and_approval_survives() -> None:
    store = MemoryWorkflowStore()
    mgr = BudgetManager(ledger=UsageLedger())
    budget = mgr.create_run_budget("ws", "wf-r4", config=BudgetConfig(max_tokens=10_000))
    ran: list[str] = []
    paused = await WorkflowEngine(spec(), registry(ran), store=store, budget=budget).run(
        {}, workflow_run_id="wf-r4"
    )
    assert paused.status == "paused"
    approval = paused.pending_approval.approval_id
    with pytest.raises(WorkflowError, match="budget scope"):
        await WorkflowEngine(spec(), registry(ran), store=store).resume("wf-r4", approval)
    assert ran == ["note"]
    engine = WorkflowEngine(spec(), registry(ran), store=store, budget=budget)
    try:
        done = await engine.resume("wf-r4", approval)
    except Exception as exc:
        # The refused resume already claimed (consumed) the approval. Does recover() finish it?
        recovered = await engine.recover("wf-r4")
        pytest.fail(
            f"the budget refusal consumed the approval: resume now raises {exc!r}; "
            f"recover() -> {recovered.status}, ran={ran}"
        )
    assert done.status == "completed" and ran == ["note", "pay"]
