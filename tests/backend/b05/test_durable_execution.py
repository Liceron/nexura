"""Acceptance tests for B05: Durable execution and worker lifecycle."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
from nexura_runtime.contracts.context import ExecutionContext
from nexura_runtime.contracts.errors import (
    BudgetExhaustedError,
    DeadlineExceededError,
    IndeterminateEffectError,
)
from nexura_runtime.errors import ExecutionCancelledError

from nexura_runtime.runtime.execution import (
    DurableExecutionEngine,
    DurableExecutionModule,
    DurableNodeSpec,
    DurableWorkflowSpec,
    FencingConflictError,
    NodeIntent,
)
from nexura_runtime.storage import SQLiteRepository


class DurableFakeBankService:
    """Separate durable service fixture tracking external effects."""

    def __init__(self) -> None:
        self.transfer_log: list[dict[str, Any]] = []

    def execute_transfer(self, transfer_id: str, amount: int, to_account: str) -> dict[str, Any]:
        self.transfer_log.append(
            {"transfer_id": transfer_id, "amount": amount, "to_account": to_account}
        )
        return {"status": "SUCCESS", "transfer_id": transfer_id}


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    return tmp_path / "test_b05.db"


@pytest.mark.asyncio
async def test_crash_recovery_matrix_with_durable_fake_service(temp_db: Path) -> None:
    """Acceptance case: Crash before dispatch, during handler, after remote success

    and before checkpoint using a separate durable fake service.
    """
    repo = SQLiteRepository(temp_db)
    fake_bank = DurableFakeBankService()

    def bank_transfer_handler(state: dict[str, Any]) -> dict[str, Any]:
        result = fake_bank.execute_transfer(
            transfer_id=state["transfer_id"],
            amount=state["amount"],
            to_account=state["to_account"],
        )
        return {"receipt": result}

    engine = DurableExecutionEngine(repo, handlers={"bank_transfer": bank_transfer_handler})

    spec = DurableWorkflowSpec(
        workflow_id="wf_money_transfer",
        nodes={
            "transfer": DurableNodeSpec(
                node_id="transfer",
                handler="bank_transfer",
                effect_policy="non_idempotent_write",
                fuel_cost=1,
            )
        },
        entry_node="transfer",
        terminal_nodes=["transfer"],
    )

    # --- Scenario 1: Crash before dispatch (dispatched=False) ---
    run_id_1 = "run-crash-before-dispatch"
    await engine.init_run(
        "ws_bank",
        run_id_1,
        spec,
        initial_state={"transfer_id": "TX-1", "amount": 1000, "to_account": "ACC-88"},
    )
    # Simulate a crash right after recording pre-dispatch intent but before actual dispatch
    rec1 = await repo.get_run("ws_bank", run_id_1)
    assert rec1 is not None
    state1 = rec1.state
    state1["pre_dispatch_intent"] = NodeIntent(
        node_id="transfer",
        effect_policy="non_idempotent_write",
        dispatched=False,
    ).model_dump()
    state1["worker_id"] = "worker-1"
    state1["lease_expires_at"] = time.time() + 30.0
    await repo.update_run("ws_bank", run_id_1, rec1.revision, "running", state_update=state1)

    # Resume on a fresh worker
    await engine.execute_step("ws_bank", run_id_1, "worker-1", spec)
    assert len(fake_bank.transfer_log) == 1
    assert fake_bank.transfer_log[0]["transfer_id"] == "TX-1"
    resumed_rec1 = await engine.get_state("ws_bank", run_id_1)
    assert resumed_rec1.status == "completed"

    # --- Scenario 2: Crash after remote success and before checkpoint ---
    # Remote call succeeded, but worker crashed before committing completed_nodes
    run_id_2 = "run-crash-after-remote-success"
    await engine.init_run(
        "ws_bank",
        run_id_2,
        spec,
        initial_state={"transfer_id": "TX-2", "amount": 2000, "to_account": "ACC-99"},
    )
    # Simulate: bank transfer was executed remotely
    fake_bank.execute_transfer("TX-2", 2000, "ACC-99")
    assert len(fake_bank.transfer_log) == 2

    # Simulate crashed state in DB: dispatched=True on a non_idempotent_write node
    rec2 = await repo.get_run("ws_bank", run_id_2)
    assert rec2 is not None
    state2 = rec2.state
    state2["pre_dispatch_intent"] = NodeIntent(
        node_id="transfer",
        effect_policy="non_idempotent_write",
        dispatched=True,
    ).model_dump()
    state2["worker_id"] = "worker-2"
    state2["lease_expires_at"] = time.time() + 30.0

    await repo.update_run("ws_bank", run_id_2, rec2.revision, "running", state_update=state2)

    # On restart, non-idempotent intent must raise IndeterminateEffectError to prevent
    # duplicate transfers
    with pytest.raises(IndeterminateEffectError) as exc_info:
        await engine.execute_step("ws_bank", run_id_2, "worker-2", spec)
    assert "Indeterminate remote outcome" in str(exc_info.value)
    # Crucial check: bank was NOT called a second time!
    assert len(fake_bank.transfer_log) == 2

    # --- Scenario 3: Crash during handler for an idempotent_write / read node ---
    read_counter = 0

    def query_balance(state: dict[str, Any]) -> dict[str, Any]:
        nonlocal read_counter
        read_counter += 1
        return {"balance": 50000}

    engine.register_handler("query_balance", query_balance)
    spec_read = DurableWorkflowSpec(
        workflow_id="wf_balance",
        nodes={
            "balance": DurableNodeSpec(
                node_id="balance",
                handler="query_balance",
                effect_policy="read",
            )
        },
        entry_node="balance",
        terminal_nodes=["balance"],
    )

    run_id_3 = "run-crash-read-node"
    await engine.init_run("ws_bank", run_id_3, spec_read)
    rec3 = await repo.get_run("ws_bank", run_id_3)
    assert rec3 is not None
    state3 = rec3.state
    state3["pre_dispatch_intent"] = NodeIntent(
        node_id="balance",
        effect_policy="read",
        dispatched=True,
    ).model_dump()
    state3["worker_id"] = "worker-3"
    state3["lease_expires_at"] = time.time() + 30.0
    await repo.update_run("ws_bank", run_id_3, rec3.revision, "running", state_update=state3)

    # Because policy is "read", safe to retry!
    res_state = await engine.execute_step("ws_bank", run_id_3, "worker-3", spec_read)
    assert res_state.status == "completed"
    assert res_state.state["balance"] == 50000
    assert read_counter == 1

    await repo.close()


@pytest.mark.asyncio
async def test_duplicate_workers_cannot_commit_stale_state(temp_db: Path) -> None:
    """Acceptance case: Duplicate workers cannot commit stale state."""
    repo = SQLiteRepository(temp_db)
    engine = DurableExecutionEngine(repo)

    spec = DurableWorkflowSpec(
        workflow_id="wf_stale",
        nodes={
            "step1": DurableNodeSpec(node_id="step1", handler="dummy", fuel_cost=1),
        },
        entry_node="step1",
        terminal_nodes=["step1"],
    )
    engine.register_handler("dummy", lambda s: {"ok": True})

    run_id = "run-fencing-test"
    await engine.init_run("ws_ops", run_id, spec)

    # Worker 1 claims with very short lease
    state_w1 = await engine.claim_run("ws_ops", run_id, "worker-1", lease_duration=0.05)
    assert state_w1.worker_id == "worker-1"
    assert state_w1.fencing_token == 1

    # Let lease expire
    await asyncio.sleep(0.08)

    # Worker 2 claims run after Worker 1's lease expired
    state_w2 = await engine.claim_run("ws_ops", run_id, "worker-2", lease_duration=30.0)
    assert state_w2.worker_id == "worker-2"
    assert state_w2.fencing_token == 2

    # Worker 1 attempts to execute step -> rejected with FencingConflictError
    with pytest.raises(FencingConflictError) as exc_info:
        await engine.execute_step("ws_ops", run_id, "worker-1", spec)
    assert exc_info.value.worker_id == "worker-1"

    # Worker 2 executes step successfully
    state_done = await engine.execute_step("ws_ops", run_id, "worker-2", spec)
    assert state_done.status == "completed"

    await repo.close()


@pytest.mark.asyncio
async def test_cancellation_schedules_no_further_work(temp_db: Path) -> None:
    """Acceptance case: Cancellation schedules no further work."""
    repo = SQLiteRepository(temp_db)
    engine = DurableExecutionEngine(repo)

    step2_executed = False

    def handler_1(s: dict[str, Any]) -> dict[str, Any]:
        return {"step1_done": True}

    def handler_2(s: dict[str, Any]) -> dict[str, Any]:
        nonlocal step2_executed
        step2_executed = True
        return {"step2_done": True}

    engine.register_handler("h1", handler_1)
    engine.register_handler("h2", handler_2)

    spec = DurableWorkflowSpec(
        workflow_id="wf_cancel",
        nodes={
            "node1": DurableNodeSpec(node_id="node1", handler="h1"),
            "node2": DurableNodeSpec(node_id="node2", handler="h2"),
        },
        entry_node="node1",
        transitions={"node1": "node2"},
        terminal_nodes=["node2"],
    )

    run_id = "run-cancel-test"
    await engine.init_run("ws_ops", run_id, spec)
    await engine.claim_run("ws_ops", run_id, "worker-cancel")

    # Execute step 1
    s1 = await engine.execute_step("ws_ops", run_id, "worker-cancel", spec)
    assert s1.current_node_id == "node2"

    # Durable cancellation requested
    cancelled_state = await engine.request_cancellation("ws_ops", run_id)
    assert cancelled_state.status == "cancelled"

    # Worker tries to execute step 2 -> raises ExecutionCancelledError
    with pytest.raises(ExecutionCancelledError):
        await engine.execute_step("ws_ops", run_id, "worker-cancel", spec)

    # Node 2 was never executed
    assert not step2_executed

    # Run remains cancelled in storage
    final_state = await engine.get_state("ws_ops", run_id)
    assert final_state.status == "cancelled"
    assert "node2" not in final_state.completed_nodes

    await repo.close()


@pytest.mark.asyncio
async def test_final_node_timeout_remains_enforced(temp_db: Path) -> None:
    """Acceptance case: Final-node timeout remains enforced."""
    repo = SQLiteRepository(temp_db)
    engine = DurableExecutionEngine(repo)

    async def slow_terminal_handler(s: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(0.5)
        return {"done": True}

    engine.register_handler("slow_terminal", slow_terminal_handler)

    spec = DurableWorkflowSpec(
        workflow_id="wf_timeout",
        nodes={
            "n_fast": DurableNodeSpec(node_id="n_fast", handler="fast"),
            "n_terminal": DurableNodeSpec(
                node_id="n_terminal",
                handler="slow_terminal",
                # A6 founder decision 2: a timed-out non-pure node pauses effect_unknown
                # (its outcome is unknown); this test is about a pure node's timeout.
                effect_policy="pure",
                timeout_seconds=0.08,  # Exceedingly short timeout
            ),
        },
        entry_node="n_fast",
        transitions={"n_fast": "n_terminal"},
        terminal_nodes=["n_terminal"],
    )
    engine.register_handler("fast", lambda s: {"fast": True})

    run_id = "run-timeout-terminal"
    await engine.init_run("ws_ops", run_id, spec)
    await engine.claim_run("ws_ops", run_id, "worker-timeout")

    # Step 1: fast node completes
    s1 = await engine.execute_step("ws_ops", run_id, "worker-timeout", spec)
    assert s1.current_node_id == "n_terminal"

    # Step 2: terminal node times out
    with pytest.raises(DeadlineExceededError) as exc_info:
        await engine.execute_step("ws_ops", run_id, "worker-timeout", spec)
    assert "timed out after" in str(exc_info.value)

    # Verify status in storage is failed with error
    final_state = await engine.get_state("ws_ops", run_id)
    assert final_state.status == "failed"
    assert "timed out" in (final_state.error or "")

    await repo.close()


@pytest.mark.asyncio
async def test_step_fuel_budget_persistence_across_resume(temp_db: Path) -> None:
    """Verify fuel and active-time budget persist accurately across restarts."""
    repo = SQLiteRepository(temp_db)
    engine = DurableExecutionEngine(repo)

    engine.register_handler("n1_h", lambda s: {"n1": True})
    engine.register_handler("n2_h", lambda s: {"n2": True})

    spec = DurableWorkflowSpec(
        workflow_id="wf_fuel",
        nodes={
            "node_a": DurableNodeSpec(node_id="node_a", handler="n1_h", fuel_cost=3),
            "node_b": DurableNodeSpec(node_id="node_b", handler="n2_h", fuel_cost=5),
        },
        entry_node="node_a",
        transitions={"node_a": "node_b"},
        terminal_nodes=["node_b"],
        default_fuel_budget=7,  # 3 + 5 = 8 > 7 -> should exhaust on node_b
    )

    run_id = "run-fuel-test"
    await engine.init_run("ws_ops", run_id, spec)
    await engine.claim_run("ws_ops", run_id, "worker-fuel")

    # Execute node_a (fuel remaining: 7 - 3 = 4)
    s1 = await engine.execute_step("ws_ops", run_id, "worker-fuel", spec)
    assert s1.fuel_consumed == 3
    assert s1.fuel_remaining == 4

    # Simulate restart on another engine instance pointing to the same DB
    engine_restarted = DurableExecutionEngine(repo)
    engine_restarted.register_handler("n2_h", lambda s: {"n2": True})

    # Node B requires 5 fuel, but only 4 remain -> BudgetExhaustedError
    with pytest.raises(BudgetExhaustedError) as exc_fuel:
        await engine_restarted.execute_step("ws_ops", run_id, "worker-fuel", spec)
    assert exc_fuel.value.details["consumed"] == 3
    assert exc_fuel.value.details["limit"] == 7

    await repo.close()


@pytest.mark.asyncio
async def test_durable_execution_module_contract_invocation(temp_db: Path) -> None:
    """Verify DurableExecutionModule conformance via B00/B01 module contract."""
    mod = DurableExecutionModule()
    assert mod.manifest.module_id == "nexura.runtime.execution"

    ctx = ExecutionContext(workspace_id="ws_mod_test", run_id="run_ctx_1", node_id="node_1")

    # 1. Initialize a run in DB first
    repo = SQLiteRepository(temp_db)
    engine = DurableExecutionEngine(repo)
    spec = DurableWorkflowSpec(
        workflow_id="wf_sample",
        nodes={"start": DurableNodeSpec(node_id="start", handler="none")},
        entry_node="start",
        terminal_nodes=["start"],
    )
    await engine.init_run("ws_mod_test", "run-mod-001", spec)
    await repo.close()

    # 2. Invoke module to claim
    result = await mod.invoke(
        ctx,
        inputs={"run_id": "run-mod-001", "action": "claim", "worker_id": "worker-unit"},
        config={"storage_uri": f"sqlite://{temp_db}"},
    )
    assert result.outputs["run_id"] == "run-mod-001"
    assert result.outputs["status"] == "running"
    assert result.outputs["fencing_token"] == 1

    # 3. Invoke module to cancel
    cancel_res = await mod.invoke(
        ctx,
        inputs={"run_id": "run-mod-001", "action": "cancel"},
        config={"storage_uri": f"sqlite://{temp_db}"},
    )
    assert cancel_res.outputs["status"] == "cancelled"
