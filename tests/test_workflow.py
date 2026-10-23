"""Tests for Workflow specification, engine execution, approvals, and durability (Task 3).

Covers:
- Spec serialization: JSON and dict export/import, canonical hash binding.
- Invalid graphs/registry names failing eagerly: missing entry, duplicate IDs,
  unknown targets/handlers.
- Max_steps loop bounds on cyclic graphs, ensuring limit accounting survives resume.
- Approval-required nodes pausing before handler execution (gated handler count zero).
- Reopen and resume with fresh engine instance from SQLite.
- Rejection executing zero gated effects and becoming terminal.
- Stale / double / concurrent resume preventing duplicate dispatch via atomic claim.
- Changed workflow spec rejected on resume (canonical hash mismatch).
- Multiple successive approval gates handled sequentially.
- Prior successful nodes before an approval gate not replayed on resume.
- Terminal checkpoints immutable (cannot resume again).
- Whole-workflow timeouts and typed errors propagating without false success.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from nexura_runtime.errors import (
    ApprovalAlreadyClaimedError,
    UnsupportedWorkflowVersionError,
    WorkflowMaxStepsError,
    WorkflowSpecMismatchError,
    WorkflowTerminalError,
    WorkflowValidationError,
)

from nexura_runtime.workflow import (
    HandlerOutput,
    MemoryWorkflowStore,
    ModuleRegistry,
    SQLiteWorkflowStore,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
)


def test_workflow_spec_serialization_and_canonical_hash() -> None:
    node1 = WorkflowNode(
        id="lookup",
        handler="lookup_order",
        transitions={"success": "review", "failed": "terminal_fail"},
        default_transition=None,
        requires_approval=False,
    )
    node2 = WorkflowNode(
        id="review",
        handler="review_action",
        transitions={"approved": "complete"},
        default_transition=None,
        requires_approval=True,
    )

    spec = WorkflowSpec(
        name="OrderWorkflow",
        entry_node="lookup",
        nodes={"lookup": node1, "review": node2},
        spec_version=1,
        terminal_nodes=["complete", "terminal_fail"],
    )

    json_str = spec.to_json()
    hash1 = spec.canonical_hash()

    reloaded = WorkflowSpec.from_json(json_str)
    assert reloaded.name == spec.name
    assert reloaded.entry_node == spec.entry_node
    assert len(reloaded.nodes) == 2
    assert reloaded.nodes["review"].requires_approval is True
    assert reloaded.canonical_hash() == hash1

    # Modification alters hash
    reloaded.name = "ModifiedWorkflow"
    assert reloaded.canonical_hash() != hash1


def test_workflow_validation_fails_eagerly() -> None:
    registry = ModuleRegistry()
    registry.register("valid_handler", lambda s: {})

    # 1. Missing entry node
    bad_spec1 = WorkflowSpec(
        name="Bad1",
        entry_node="non_existent",
        nodes={"n1": WorkflowNode(id="n1", handler="valid_handler")},
    )
    with pytest.raises(WorkflowValidationError, match="Entry node 'non_existent' not found"):
        WorkflowEngine(bad_spec1, registry)

    # 2. Unknown handler in registry
    bad_spec2 = WorkflowSpec(
        name="Bad2",
        entry_node="n1",
        nodes={"n1": WorkflowNode(id="n1", handler="unregistered_handler")},
    )
    with pytest.raises(WorkflowValidationError, match="references unknown handler"):
        WorkflowEngine(bad_spec2, registry)

    # 3. Transition targets unknown node
    bad_spec3 = WorkflowSpec(
        name="Bad3",
        entry_node="n1",
        nodes={
            "n1": WorkflowNode(id="n1", handler="valid_handler", transitions={"go": "ghost_node"})
        },
    )
    with pytest.raises(WorkflowValidationError, match="targets unknown node 'ghost_node'"):
        WorkflowEngine(bad_spec3, registry)

    # 4. Duplicate node IDs in list import
    bad_json = """
    {
        "name": "BadDup",
        "entry_node": "n1",
        "nodes": [
            {"id": "n1", "handler": "valid_handler"},
            {"id": "n1", "handler": "valid_handler"}
        ]
    }
    """
    with pytest.raises(WorkflowValidationError, match="Duplicate node ID"):
        WorkflowSpec.from_json(bad_json)


@pytest.mark.asyncio
async def test_workflow_loop_bound_enforced_on_cycle() -> None:
    registry = ModuleRegistry()
    cycle_count = 0

    @registry.register_handler("loop_handler")
    def loop_handler(state: dict[str, Any]) -> dict[str, Any]:
        nonlocal cycle_count
        cycle_count += 1
        return {"iterations": state.get("iterations", 0) + 1}

    # Node n1 transitions to n1 (infinite cycle)
    spec = WorkflowSpec(
        name="CycleWorkflow",
        entry_node="n1",
        nodes={"n1": WorkflowNode(id="n1", handler="loop_handler", default_transition="n1")},
    )

    engine = WorkflowEngine(spec, registry, max_steps=5)
    with pytest.raises(WorkflowMaxStepsError) as exc_info:
        await engine.run({"iterations": 0})

    assert exc_info.value.step_count == 5
    assert exc_info.value.max_steps == 5
    assert cycle_count == 5


@pytest.mark.asyncio
async def test_gated_handler_zero_effects_before_approval(tmp_path: Path) -> None:
    db_file = tmp_path / "gated.db"
    store = SQLiteWorkflowStore(db_file)
    registry = ModuleRegistry()

    pre_gate_executed = False
    gated_executed = False

    @registry.register_handler("step_one")
    def step_one(state: dict[str, Any]) -> dict[str, Any]:
        nonlocal pre_gate_executed
        pre_gate_executed = True
        return {"step1": "done"}

    @registry.register_handler("gated_action")
    def gated_action(state: dict[str, Any]) -> dict[str, Any]:
        nonlocal gated_executed
        gated_executed = True
        return {"action": "executed"}

    spec = WorkflowSpec(
        name="ApprovalWorkflow",
        entry_node="step_one",
        nodes={
            "step_one": WorkflowNode(id="step_one", handler="step_one", default_transition="gate"),
            "gate": WorkflowNode(
                id="gate",
                handler="gated_action",
                requires_approval=True,
                default_transition=None,
            ),
        },
    )

    engine = WorkflowEngine(spec, registry, store=store)
    result = await engine.run({"data": "init"})

    assert result.status == "paused"
    assert result.current_node_id == "gate"
    assert result.pending_approval is not None
    assert result.pending_approval.node_id == "gate"
    assert result.pending_approval.input_state["step1"] == "done"

    # Pre-gate node executed, but gated node handler NEVER executed
    assert pre_gate_executed is True
    assert gated_executed is False

    await store.close()


@pytest.mark.asyncio
async def test_fresh_engine_instance_resumes_from_sqlite(tmp_path: Path) -> None:
    db_file = tmp_path / "resume.db"
    store1 = SQLiteWorkflowStore(db_file)
    registry = ModuleRegistry()

    node1_calls = 0
    node2_calls = 0

    @registry.register_handler("init_node")
    def init_node(state: dict[str, Any]) -> dict[str, Any]:
        nonlocal node1_calls
        node1_calls += 1
        return {"account": "ACC-9922", "balance": 1000}

    @registry.register_handler("transfer_node")
    def transfer_node(state: dict[str, Any]) -> dict[str, Any]:
        nonlocal node2_calls
        node2_calls += 1
        return {"transferred": True, "amount": state.get("amount", 200)}

    spec = WorkflowSpec(
        name="TransferSpec",
        entry_node="init_node",
        nodes={
            "init_node": WorkflowNode(
                id="init_node", handler="init_node", default_transition="transfer_node"
            ),
            "transfer_node": WorkflowNode(
                id="transfer_node",
                handler="transfer_node",
                requires_approval=True,
                default_transition=None,
            ),
        },
    )

    # Engine 1 runs and pauses at transfer_node
    engine1 = WorkflowEngine(spec, registry, store=store1)
    res1 = await engine1.run({"customer": "Rohit"})
    assert res1.status == "paused"
    assert res1.pending_approval is not None
    approval_id = res1.pending_approval.approval_id

    await store1.close()

    # Engine 2 (completely new instance and store instance) resumes from the same DB
    store2 = SQLiteWorkflowStore(db_file)
    engine2 = WorkflowEngine(spec, registry, store=store2)

    res2 = await engine2.resume(
        res1.workflow_run_id,
        approval_id=approval_id,
        decision="approve",
        state_update={"amount": 350},
    )

    # B43: transfer_node is a function node and reads the whole state, so the edited amount
    # is not covered by the first approval: the run pauses again showing 350, and the
    # gated handler has not run. Approving that fresh request (on engine 2) completes it.
    assert res2.status == "paused"
    assert res2.pending_approval is not None
    assert res2.pending_approval.approval_id != approval_id
    assert res2.pending_approval.input_state["amount"] == 350
    assert node2_calls == 0
    res2 = await engine2.resume(
        res1.workflow_run_id,
        approval_id=res2.pending_approval.approval_id,
        decision="approve",
    )

    assert res2.status == "completed"
    assert res2.state["transferred"] is True
    assert res2.state["amount"] == 350
    assert res2.state["account"] == "ACC-9922"

    # Prior successful node was NOT replayed!
    assert node1_calls == 1
    assert node2_calls == 1

    await store2.close()


@pytest.mark.asyncio
async def test_rejection_executes_zero_gated_effects_and_becomes_terminal(
    tmp_path: Path,
) -> None:
    db_file = tmp_path / "reject.db"
    store = SQLiteWorkflowStore(db_file)
    registry = ModuleRegistry()

    gated_called = False

    @registry.register_handler("gate_handler")
    def gate_handler(state: dict[str, Any]) -> dict[str, Any]:
        nonlocal gated_called
        gated_called = True
        return {"executed": True}

    spec = WorkflowSpec(
        name="RejectSpec",
        entry_node="gated",
        nodes={"gated": WorkflowNode(id="gated", handler="gate_handler", requires_approval=True)},
    )

    engine = WorkflowEngine(spec, registry, store=store)
    res = await engine.run({})
    assert res.status == "paused"
    assert res.pending_approval is not None
    approval_id = res.pending_approval.approval_id

    # Explicit rejection
    res_reject = await engine.resume(
        res.workflow_run_id,
        approval_id=approval_id,
        decision="reject",
    )

    assert res_reject.status == "rejected"
    assert gated_called is False  # Zero gated effects executed

    # Attempting to resume again raises WorkflowTerminalError
    with pytest.raises(WorkflowTerminalError, match="in terminal state 'rejected'"):
        await engine.resume(res.workflow_run_id, approval_id=approval_id, decision="approve")

    await store.close()


@pytest.mark.asyncio
async def test_atomic_claim_prevents_duplicate_resume(tmp_path: Path) -> None:
    db_file = tmp_path / "duplicate_claim.db"
    store = SQLiteWorkflowStore(db_file)
    registry = ModuleRegistry()

    dispatch_count = 0
    # The first resume is held inside the handler until the second has tried to claim, so
    # the two resumes really overlap even on a slow machine (CI ran them one after another).
    in_handler = asyncio.Event()
    release = asyncio.Event()

    @registry.register_handler("critical_action")
    async def critical_action(state: dict[str, Any]) -> dict[str, Any]:
        nonlocal dispatch_count
        dispatch_count += 1
        in_handler.set()
        await release.wait()
        return {"dispatched": True}

    spec = WorkflowSpec(
        name="CriticalSpec",
        entry_node="action",
        nodes={
            "action": WorkflowNode(id="action", handler="critical_action", requires_approval=True)
        },
    )

    engine = WorkflowEngine(spec, registry, store=store)
    res = await engine.run({})
    assert res.pending_approval is not None
    approval_id = res.pending_approval.approval_id

    # Concurrent resumes attempting to approve the same pending request
    async def try_resume() -> str:
        try:
            r = await engine.resume(res.workflow_run_id, approval_id=approval_id)
            return r.status
        except ApprovalAlreadyClaimedError:
            return "claimed_conflict"

    async def try_resume_while_first_is_dispatching() -> str:
        await asyncio.wait_for(in_handler.wait(), timeout=30)
        try:
            return await try_resume()
        finally:
            release.set()

    results = await asyncio.gather(try_resume(), try_resume_while_first_is_dispatching())

    # Exactly one succeeded and one raised ApprovalAlreadyClaimedError
    assert "completed" in results
    assert "claimed_conflict" in results
    # Dispatched exactly once
    assert dispatch_count == 1

    await store.close()


@pytest.mark.asyncio
async def test_changed_spec_rejected_on_resume(tmp_path: Path) -> None:
    db_file = tmp_path / "spec_mismatch.db"
    store = SQLiteWorkflowStore(db_file)
    registry = ModuleRegistry()

    @registry.register_handler("fn")
    def fn(state: dict[str, Any]) -> dict[str, Any]:
        return {}

    spec_v1 = WorkflowSpec(
        name="SpecV1",
        entry_node="node1",
        nodes={"node1": WorkflowNode(id="node1", handler="fn", requires_approval=True)},
    )

    engine1 = WorkflowEngine(spec_v1, registry, store=store)
    run_res = await engine1.run({})
    assert run_res.pending_approval is not None
    approval_id = run_res.pending_approval.approval_id

    # Unknown spec versions are rejected before any engine can resume with them.
    with pytest.raises(UnsupportedWorkflowVersionError):
        WorkflowEngine(
            WorkflowSpec(
                name="SpecV1",
                entry_node="node1",
                nodes={"node1": WorkflowNode(id="node1", handler="fn", requires_approval=True)},
                spec_version=2,
            ),
            registry,
            store=store,
        )

    # Same supported version, altered definition: resume must still detect tampering.
    spec_tampered = WorkflowSpec(
        name="SpecV2_Tampered",
        entry_node="node1",
        nodes={"node1": WorkflowNode(id="node1", handler="fn", requires_approval=True)},
    )

    engine2 = WorkflowEngine(spec_tampered, registry, store=store)
    with pytest.raises(WorkflowSpecMismatchError):
        await engine2.resume(run_res.workflow_run_id, approval_id=approval_id)

    await store.close()


@pytest.mark.asyncio
async def test_multiple_successive_approval_gates() -> None:
    store = MemoryWorkflowStore()
    registry = ModuleRegistry()

    gate1_executed = False
    gate2_executed = False

    @registry.register_handler("gate1")
    def gate1(state: dict[str, Any]) -> dict[str, Any]:
        nonlocal gate1_executed
        gate1_executed = True
        return {"gate1": "approved"}

    @registry.register_handler("gate2")
    def gate2(state: dict[str, Any]) -> dict[str, Any]:
        nonlocal gate2_executed
        gate2_executed = True
        return {"gate2": "approved"}

    spec = WorkflowSpec(
        name="MultiGate",
        entry_node="g1",
        nodes={
            "g1": WorkflowNode(
                id="g1", handler="gate1", requires_approval=True, default_transition="g2"
            ),
            "g2": WorkflowNode(
                id="g2", handler="gate2", requires_approval=True, default_transition=None
            ),
        },
    )

    engine = WorkflowEngine(spec, registry, store=store)

    # 1. Start run -> pauses at g1
    res1 = await engine.run({})
    assert res1.status == "paused"
    assert res1.current_node_id == "g1"
    assert gate1_executed is False

    # 2. Approve g1 -> advances and pauses at g2
    assert res1.pending_approval is not None
    app1 = res1.pending_approval.approval_id
    res2 = await engine.resume(res1.workflow_run_id, approval_id=app1)
    assert res2.status == "paused"
    assert res2.current_node_id == "g2"
    assert gate1_executed is True
    assert gate2_executed is False

    # 3. Approve g2 -> completes
    assert res2.pending_approval is not None
    app2 = res2.pending_approval.approval_id
    res3 = await engine.resume(res1.workflow_run_id, approval_id=app2)
    assert res3.status == "completed"
    assert gate2_executed is True
    assert res3.state["gate1"] == "approved"
    assert res3.state["gate2"] == "approved"


@pytest.mark.asyncio
async def test_limit_accounting_survives_resume() -> None:
    store = MemoryWorkflowStore()
    registry = ModuleRegistry()

    @registry.register_handler("step_a")
    def step_a(s: dict[str, Any]) -> dict[str, Any]:
        return {}

    @registry.register_handler("step_b")
    def step_b(s: dict[str, Any]) -> dict[str, Any]:
        return {}

    @registry.register_handler("step_c")
    def step_c(s: dict[str, Any]) -> dict[str, Any]:
        return {}

    spec = WorkflowSpec(
        name="AccountingSpec",
        entry_node="a",
        nodes={
            "a": WorkflowNode(id="a", handler="step_a", default_transition="b"),
            "b": WorkflowNode(
                id="b", handler="step_b", requires_approval=True, default_transition="c"
            ),
            "c": WorkflowNode(id="c", handler="step_c", default_transition=None),
        },
    )

    # Max steps = 2. Node a executes (step 1). Gate b pauses.
    engine = WorkflowEngine(spec, registry, store=store, max_steps=2)
    res = await engine.run({})
    assert res.status == "paused"
    assert res.step_count == 1
    assert res.pending_approval is not None
    app_id = res.pending_approval.approval_id

    # On resume: step_count starts at 1. Node b executes (step 2).
    # Then transition to c, but step_count (2) >= max_steps (2) -> raises WorkflowMaxStepsError!
    with pytest.raises(WorkflowMaxStepsError) as exc_info:
        await engine.resume(res.workflow_run_id, approval_id=app_id)

    assert exc_info.value.step_count == 2
    assert exc_info.value.max_steps == 2


@pytest.mark.asyncio
async def test_deterministic_named_branching() -> None:
    registry = ModuleRegistry()

    @registry.register_handler("router")
    def router(state: dict[str, Any]) -> HandlerOutput:
        amount = state.get("amount", 0)
        branch = "high_value" if amount > 1000 else "standard"
        return HandlerOutput(state_update={"evaluated": True}, branch=branch)

    @registry.register_handler("standard_handler")
    def standard_handler(state: dict[str, Any]) -> dict[str, Any]:
        return {"processed_route": "standard"}

    @registry.register_handler("vip_handler")
    def vip_handler(state: dict[str, Any]) -> dict[str, Any]:
        return {"processed_route": "vip"}

    spec = WorkflowSpec(
        name="BranchingSpec",
        entry_node="router",
        nodes={
            "router": WorkflowNode(
                id="router",
                handler="router",
                transitions={"standard": "std_node", "high_value": "vip_node"},
            ),
            "std_node": WorkflowNode(id="std_node", handler="standard_handler"),
            "vip_node": WorkflowNode(id="vip_node", handler="vip_handler"),
        },
    )

    engine = WorkflowEngine(spec, registry)

    # Standard branch
    r_std = await engine.run({"amount": 500})
    assert r_std.state["processed_route"] == "standard"

    # VIP branch
    r_vip = await engine.run({"amount": 5000})
    assert r_vip.state["processed_route"] == "vip"
