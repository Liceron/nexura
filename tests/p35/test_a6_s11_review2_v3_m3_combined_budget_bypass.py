"""Round 2, M3: a run journaled under RunBudget scope A (100,000 tokens) is continued under a
*combined* budget that does not contain A. _resolve_budget keeps "any other budget object"
as it is, and Runner._check_budget_scope only compares a plain RunBudget: both continue
silently, without A's limit. (With a plain RunBudget of another scope the workflow now
switches to A and warns, and the Runner warns.)"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any

import pytest
from nexura_agents import Agent, FakeModel, MemoryCheckpointStore, Runner, ToolCall, tool
from nexura_agents.usage import BudgetConfig, BudgetManager, UsageLedger, admission

from nexura_runtime.workflow import (
    MemoryWorkflowStore,
    ModuleRegistry,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
)


class Crash(BaseException):
    pass


def spec() -> WorkflowSpec:
    return WorkflowSpec(
        name="v3",
        entry_node="a",
        nodes={
            "a": WorkflowNode(id="a", handler="note", default_transition="b"),
            "b": WorkflowNode(id="b", handler="pay"),
        },
        terminal_nodes=["b"],
    )


def registry(crash: set[str]) -> ModuleRegistry:
    reg = ModuleRegistry()

    def handler(name: str) -> Any:
        def run(state: dict[str, Any]) -> dict[str, Any]:
            if name in crash:
                crash.discard(name)
                raise Crash(name)
            return {name: True}

        return run

    reg.register("note", handler("note"), effect="idempotent")
    reg.register("pay", handler("pay"), effect="idempotent")
    return reg


def managers(tmp_path: Path) -> tuple[Any, Any, Any]:
    mgr = BudgetManager(ledger=UsageLedger(str(tmp_path / "u.db")))
    tight = mgr.create_run_budget("ws", "tenant-A", config=BudgetConfig(max_tokens=100_000))
    loose = mgr.create_run_budget("ws", "tenant-B", config=BudgetConfig(max_tokens=10_000_000))
    other = mgr.create_run_budget("ws", "tenant-C", config=BudgetConfig(max_tokens=10_000_000))
    return tight, loose, other


async def test_workflow_combined_budget_without_the_recorded_scope(tmp_path: Path) -> None:
    tight, loose, other = managers(tmp_path)
    store = MemoryWorkflowStore()
    engine = WorkflowEngine(spec(), registry({"pay"}), store=store, budget=tight)
    with pytest.raises(Crash):
        await engine.run({}, workflow_run_id="wf-v3")
    combined = admission.combine_budgets(loose, other)
    again = WorkflowEngine(spec(), registry(set()), store=store, budget=combined)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            await again.recover("wf-v3")
        except Exception:
            return  # refused: fail closed
    scopes = [b.run_id for b in getattr(again.budget, "budgets", (again.budget,))]
    assert "tenant-A" in scopes or any("scope" in str(w.message) for w in caught), (
        f"continued silently under {scopes}; the run was journaled under tenant-A"
    )


async def test_runner_combined_budget_without_the_recorded_scope(tmp_path: Path) -> None:
    tight, loose, other = managers(tmp_path)

    @tool(requires_approval=True)
    def pay(amount: int) -> str:
        """Pay."""
        return "paid"

    def agent(first: bool) -> Agent:
        responses: list[Any] = ["done"]
        if first:
            responses.insert(0, [ToolCall(id="c1", name="pay", arguments={"amount": 1})])
        return Agent(name="A", instructions="i", model=FakeModel(responses=responses), tools=[pay])

    store = MemoryCheckpointStore()
    paused = await Runner(checkpoint_store=store, budget=tight).run_async(agent(True), "go")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        done = await Runner(
            checkpoint_store=store, budget=admission.combine_budgets(loose, other)
        ).resume_async(agent(False), paused.interruption, approve=True)
    assert done.status == "completed"
    assert any("scope" in str(w.message) for w in caught), (
        "resumed silently under a combined budget without the recorded scope tenant-A"
    )
