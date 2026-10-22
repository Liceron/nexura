"""A6 S11 review repro R3: a workflow run journaled under budget scope A is recovered under scope B
silently.

_restore_budget only fails closed when the engine has *no* budget. With any other budget it
calls restore_run_budget(self.budget), which restores the policy of the engine's OWN scope,
not the recorded one: the run's original limit is not enforced, the original scope's
open reservations are still settled (they are linked by execution, not scope), and no
warning is given (the Runner at least warns). The recorded scope is never consulted.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any

import pytest
from nexura_agents.types import Usage
from nexura_agents.usage import BudgetConfig, BudgetManager, UsageLedger

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
        name="r3",
        entry_node="a",
        nodes={
            "a": WorkflowNode(id="a", handler="note", default_transition="b"),
            "b": WorkflowNode(id="b", handler="pay"),
        },
        terminal_nodes=["b"],
    )


def registry(holder: dict[str, Any], crash: set[str]) -> ModuleRegistry:
    reg = ModuleRegistry()

    def handler(name: str) -> Any:
        def run(state: dict[str, Any]) -> dict[str, Any]:
            budget = holder["budget"]
            res = budget.admit_provider_call("fake-model", estimated_tokens=200)
            if name in crash:
                crash.discard(name)
                raise Crash(name)
            budget.settle_provider_call(res, usage=Usage(1, 1, 2))
            return {name: True}

        return run

    reg.register("note", handler("note"), effect="idempotent")
    reg.register("pay", handler("pay"), effect="idempotent")
    return reg


async def test_recover_under_another_scope_is_refused_or_restores_the_recorded_one(
    tmp_path: Path,
) -> None:
    store = MemoryWorkflowStore()
    mgr = BudgetManager(ledger=UsageLedger(str(tmp_path / "u.db")))
    tight = mgr.create_run_budget(
        "ws", "tenant-A", config=BudgetConfig(max_tokens=1_000), default_model="fake-model"
    )
    holder = {"budget": tight}
    engine = WorkflowEngine(spec(), registry(holder, {"pay"}), store=store, budget=tight)
    with pytest.raises(Crash):
        await engine.run({}, workflow_run_id="wf-r3")

    loose = mgr.create_run_budget(
        "ws", "tenant-B", config=BudgetConfig(max_tokens=10_000_000), default_model="fake-model"
    )
    holder["budget"] = loose
    again = WorkflowEngine(spec(), registry(holder, set()), store=store, budget=loose)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            await again.recover("wf-r3")
        except Exception:
            return  # refused: fail closed, fine
    restored = again.budget
    assert restored.run_id == "tenant-A" or any("scope" in str(w.message) for w in caught), (
        f"recovered silently under scope {restored.run_id!r} "
        f"(max_tokens={restored.config.max_tokens}); run was journaled under 'tenant-A'"
    )
