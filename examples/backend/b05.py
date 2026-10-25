"""B05 Standalone Example: Durable Execution and Worker Lifecycle.

Demonstrates, with ``WorkflowEngine`` on a ``SQLiteWorkflowStore`` (the execution journal):
1. A worker runs a three-step workflow and is killed in the middle of step 2.
2. A new worker (a new engine on the same database) recovers the run: completed steps are
   NOT repeated, and the interrupted pure step is simply run again.
3. A payment step that is not declared idempotent is killed after the payment happened:
   recovery pauses the run ``effect_unknown`` instead of paying twice, and an operator
   resolves it.
4. Invariant checks (invocation counts) and the run's journal (lease token, effect record).

All effects are local fakes. (Before A6 S12 this example used the legacy
``DurableExecutionEngine``, which was removed.)
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

from nexura_agents.contracts.execution import ExecutionId

from nexura_runtime.workflow import (
    ModuleRegistry,
    SQLiteWorkflowStore,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
)


class WorkerKilled(BaseException):
    """Stands in for the worker process dying (nothing in the engine catches it)."""


async def run_worker_crash_and_resume_demo(db_path: Path) -> None:
    print("--- B05: Durable Execution & Worker Recovery Demonstration ---")
    calls = {"fetch_customer": 0, "evaluate_score": 0, "disburse": 0}
    kill = {"evaluate_score": True, "disburse": True}
    ledger: list[str] = []  # the payment provider's own record

    async def fetch_customer(state: dict[str, Any]) -> dict[str, Any]:
        calls["fetch_customer"] += 1
        print("  [fetch_customer] reading the customer (read effect)")
        return {"customer_id": "CUST-IN-1002", "name": "Rajesh Sharma", "monthly_income": 85000}

    async def evaluate_score(state: dict[str, Any]) -> dict[str, Any]:
        calls["evaluate_score"] += 1
        print("  [evaluate_score] scoring (pure)")
        if kill["evaluate_score"]:
            kill["evaluate_score"] = False
            print("  *** worker 1 is killed in the middle of evaluate_score ***")
            raise WorkerKilled
        return {"credit_score": 790, "max_limit": state["monthly_income"] * 5}

    async def disburse(state: dict[str, Any]) -> dict[str, Any]:
        calls["disburse"] += 1
        ledger.append(f"pay {state['max_limit']} to {state['customer_id']}")
        print("  [disburse] payment sent to the provider (not idempotent)")
        if kill["disburse"]:
            kill["disburse"] = False
            print("  *** worker 2 is killed after the payment, before recording it ***")
            raise WorkerKilled
        return {"disbursed": True}

    registry = ModuleRegistry()
    registry.register("fetch_customer", fetch_customer, effect="pure")
    registry.register("evaluate_score", evaluate_score, effect="pure")
    registry.register("disburse", disburse)  # undeclared: non_idempotent
    spec = WorkflowSpec(
        name="credit-eval",
        entry_node="fetch_customer",
        nodes={
            "fetch_customer": WorkflowNode(
                id="fetch_customer", handler="fetch_customer", default_transition="evaluate_score"
            ),
            "evaluate_score": WorkflowNode(
                id="evaluate_score", handler="evaluate_score", default_transition="disburse"
            ),
            "disburse": WorkflowNode(id="disburse", handler="disburse"),
        },
        terminal_nodes=["disburse"],
    )
    run_id = "run_credit_eval_701"

    print("\n1. Worker 1 starts the run...")
    store = SQLiteWorkflowStore(db_path)
    try:
        await WorkflowEngine(spec, registry, store=store).run({}, workflow_run_id=run_id)
    except WorkerKilled:
        pass
    finally:
        await store.close()

    print("\n2. Worker 2 starts on the same database and recovers the run...")
    store = SQLiteWorkflowStore(db_path)
    try:
        await WorkflowEngine(spec, registry, store=store).recover(run_id)
    except WorkerKilled:
        pass
    finally:
        await store.close()

    print("\n3. Worker 3 recovers again; the payment's outcome is unknown...")
    store = SQLiteWorkflowStore(db_path)
    try:
        engine = WorkflowEngine(spec, registry, store=store)
        paused = await engine.recover(run_id)
        print(f"   status={paused.status} pause_reason={paused.pause_reason}")
        assert paused.status == "paused" and paused.pause_reason == "effect_unknown"
        assert len(ledger) == 1, "the payment must not be sent twice"

        print("   An operator checks the provider's ledger and confirms the payment.")
        done = await engine.resolve_effect(
            run_id, "confirmed", actor="ops@example.test", result={"disbursed": True}
        )
        print(f"   Final status: {done.status}; limit: {done.state['max_limit']:,}")
        assert done.status == "completed"

        journal = await store.open_execution_store()
        snapshot = await journal.get(ExecutionId("default", run_id))
        (effect,) = snapshot.effects.values()
        print(
            f"   Journal: fencing token {snapshot.run.fencing_token}, effect {effect.status.value}"
        )
        print(f"            resolution {effect.resolution.value} by {effect.resolved_by}")
    finally:
        await store.close()

    print("\n--- Verifying Invariants ---")
    print(f"Invocation counts: {calls}; provider ledger: {ledger}")
    assert calls["fetch_customer"] == 1, "a completed step was repeated"
    assert calls["evaluate_score"] == 2  # the interrupted pure step ran again
    assert calls["disburse"] == 1 and len(ledger) == 1
    print("SUCCESS: completed steps were not repeated and the payment happened once.")


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        await run_worker_crash_and_resume_demo(Path(tmp_dir) / "durable_exec.db")
    print("\nB05 execution completed successfully.")


if __name__ == "__main__":
    asyncio.run(main())
