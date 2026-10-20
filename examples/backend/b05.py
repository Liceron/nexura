"""B05 Standalone Example: Durable Execution and Worker Lifecycle.

Demonstrates:
1. Executing a read-only workflow with an embedded worker.
2. Simulating worker kill/crash after step 1 checkpoint.
3. Restarting with a new worker (new fencing token lease) and resuming
   the checkpoint WITHOUT repeating completed nodes.
4. Invariant verification (invocation counts, fuel consumption, and event timeline).
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

from nexura_runtime.runtime.execution import (
    DurableExecutionEngine,
    DurableNodeSpec,
    DurableWorkflowSpec,
)
from nexura_runtime.storage import SQLiteRepository


async def run_worker_crash_and_resume_demo(db_path: Path) -> None:
    print("--- B05: Durable Execution & Worker Recovery Demonstration ---")
    repo = SQLiteRepository(db_path)
    workspace_id = "ws_delhi_credit"
    run_id = "run_credit_eval_701"

    # Invocation counters to verify no completed nodes are repeated upon restart
    invocation_counts = {
        "fetch_customer": 0,
        "evaluate_score": 0,
        "format_offer": 0,
    }

    # 1. Define node handlers
    async def fetch_customer(state: dict[str, Any]) -> dict[str, Any]:
        invocation_counts["fetch_customer"] += 1
        print("  [Node: fetch_customer] Executing (read effect)...")
        await asyncio.sleep(0.02)
        return {
            "customer_id": "CUST-IN-1002",
            "name": "राजेश शर्मा (Rajesh Sharma)",
            "monthly_income": 85000,
        }

    async def evaluate_score(state: dict[str, Any]) -> dict[str, Any]:
        invocation_counts["evaluate_score"] += 1
        print("  [Node: evaluate_score] Executing (pure effect)...")
        income = state["monthly_income"]
        eligible_limit = income * 5
        return {"credit_score": 790, "max_limit": eligible_limit}

    async def format_offer(state: dict[str, Any]) -> dict[str, Any]:
        invocation_counts["format_offer"] += 1
        print("  [Node: format_offer] Executing (pure effect)...")
        name = state["name"]
        limit = state["max_limit"]
        return {
            "summary_hindi": f"बधाई हो {name}, आपकी पूर्व-स्वीकृत सीमा ₹{limit:,} है।",
            "decision": "APPROVED",
        }

    engine1 = DurableExecutionEngine(
        repo,
        handlers={
            "fetch_customer": fetch_customer,
            "evaluate_score": evaluate_score,
            "format_offer": format_offer,
        },
    )

    # 2. Build durable workflow specification
    spec = DurableWorkflowSpec(
        workflow_id="wf_credit_eval",
        nodes={
            "fetch_customer": DurableNodeSpec(
                node_id="fetch_customer",
                handler="fetch_customer",
                effect_policy="read",
                fuel_cost=1,
            ),
            "evaluate_score": DurableNodeSpec(
                node_id="evaluate_score",
                handler="evaluate_score",
                effect_policy="pure",
                fuel_cost=1,
            ),
            "format_offer": DurableNodeSpec(
                node_id="format_offer",
                handler="format_offer",
                effect_policy="pure",
                fuel_cost=1,
            ),
        },
        entry_node="fetch_customer",
        transitions={
            "fetch_customer": "evaluate_score",
            "evaluate_score": "format_offer",
        },
        terminal_nodes=["format_offer"],
        default_fuel_budget=10,
        default_time_budget_seconds=30.0,
    )

    print(f"\n1. Initializing workflow run '{run_id}'...")
    initial_state = await engine1.init_run(workspace_id, run_id, spec)
    print(
        f"   Run initialized. Entry node: {initial_state.current_node_id}, "
        f"status: {initial_state.status}"
    )

    # 3. Worker 1 claims run and executes Node 1
    worker_1_id = "worker-emb-alpha"
    print(f"\n2. Worker 1 ('{worker_1_id}') claiming run...")
    state_w1 = await engine1.claim_run(workspace_id, run_id, worker_1_id, lease_duration=0.05)
    print(f"   Lease acquired. Fencing token: {state_w1.fencing_token}")

    print("   Worker 1 executing step 1 ('fetch_customer')...")
    step1_state = await engine1.execute_step(workspace_id, run_id, worker_1_id, spec)
    print(f"   Step 1 finished. Completed nodes: {step1_state.completed_nodes}")
    print(f"   Next node to execute: '{step1_state.current_node_id}'")
    print(f"   Customer loaded: {step1_state.state.get('name')}")

    # 4. SIMULATE WORKER 1 SUDDEN CRASH / KILL
    print("\n3. *** Simulating abrupt Worker 1 process termination / crash ***")
    print(f"   Invocation count for 'fetch_customer' so far: {invocation_counts['fetch_customer']}")
    del engine1  # Worker 1 is dead
    print("   Waiting for Worker 1 lease to expire before Worker 2 claims run...")
    await asyncio.sleep(0.06)

    # 5. Worker 2 starts up, claims lease with advanced fencing token, and resumes
    worker_2_id = "worker-emb-beta"
    print(f"\n4. Worker 2 ('{worker_2_id}') restarting from durable storage...")

    engine2 = DurableExecutionEngine(
        repo,
        handlers={
            "fetch_customer": fetch_customer,
            "evaluate_score": evaluate_score,
            "format_offer": format_offer,
        },
    )

    state_w2 = await engine2.claim_run(workspace_id, run_id, worker_2_id, lease_duration=10.0)
    print(f"   Worker 2 acquired lease. New fencing token: {state_w2.fencing_token}")
    print(f"   Resumed state has completed nodes: {state_w2.completed_nodes}")
    print(f"   Current node to execute: '{state_w2.current_node_id}'")

    print("\n5. Worker 2 running remaining steps to completion...")
    final_state = await engine2.run_until_idle(workspace_id, run_id, worker_2_id, spec)

    print("\n--- Execution Finished ---")
    print(f"Final Status: {final_state.status}")
    print(
        f"Step Fuel Consumed: {final_state.fuel_consumed} / Remaining: {final_state.fuel_remaining}"
    )
    print(f"Decision: {final_state.state.get('decision')}")

    print(f"Offer Text: {final_state.state.get('summary_hindi')}")

    # 6. Verify Acceptance Invariants
    print("\n--- Verifying Invariants ---")
    print(f"Invocation counts: {invocation_counts}")
    assert invocation_counts["fetch_customer"] == 1, (
        f"ERROR: Node 1 was re-executed {invocation_counts['fetch_customer']} times!"
    )
    assert invocation_counts["evaluate_score"] == 1
    assert invocation_counts["format_offer"] == 1
    print("SUCCESS: Completed node 'fetch_customer' was NOT repeated upon resume.")

    # 7. Verify audit event stream from repository
    events = await repo.get_run_events(workspace_id, run_id)
    print(f"\nTotal audit events in run log: {len(events)}")
    for evt in events:
        print(f"  [{evt.seq}] {evt.event_type} - {evt.payload.get('node_id', '')}")

    await repo.close()


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "durable_exec.db"
        await run_worker_crash_and_resume_demo(db_path)
    print("\nB05 execution completed successfully.")


if __name__ == "__main__":
    asyncio.run(main())
