"""Round 3, R2-M1 filter: a fenced terminal sweep now skips unlinked reservations ("left to
a takeover"). A run that completes normally has no takeover, so an unlinked reservation of
the run's own budget scope (the service convention: scope = run id) stays active for good.
Round 2 ended it at the terminal status (include_unlinked=own_scope).

Unlinked = made outside the run's dispatch scopes, e.g. on a plain threading.Thread (no
contextvars) or by code written before S11.
"""

from __future__ import annotations

import threading
from typing import Any

from nexura_agents.usage import BudgetConfig, BudgetManager, UsageLedger

from nexura_runtime.workflow import (
    MemoryWorkflowStore,
    ModuleRegistry,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
)


async def test_unlinked_reservation_of_the_run_scope_does_not_outlive_a_completed_run() -> None:
    mgr = BudgetManager(ledger=UsageLedger())
    budget = mgr.create_run_budget(
        "default", "wf-w3", config=BudgetConfig(max_tokens=1_000_000), default_model="fake-model"
    )

    def handler(state: dict[str, Any]) -> dict[str, Any]:
        def background() -> None:  # no contextvars: the reservation is unlinked
            budget.admit_provider_call("fake-model", estimated_tokens=300)  # never settled

        t = threading.Thread(target=background)
        t.start()
        t.join()
        return {"ok": True}

    reg = ModuleRegistry()
    reg.register("h", handler, effect="idempotent")
    spec = WorkflowSpec(
        name="w3",
        entry_node="a",
        nodes={"a": WorkflowNode(id="a", handler="h")},
        terminal_nodes=["a"],
    )
    engine = WorkflowEngine(spec, reg, store=MemoryWorkflowStore(), budget=budget)
    result = await engine.run({}, workflow_run_id="wf-w3")
    assert result.status == "completed"
    left = [r for r in mgr.ledger._reservations.values() if r.status in ("active", "expired")]
    assert left == [], f"{len(left)} unlinked reservation(s) of the run's scope left active"
