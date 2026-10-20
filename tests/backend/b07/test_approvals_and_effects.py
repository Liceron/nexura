"""Unit and acceptance tests for B07: Approval Policies and Effect Reconciliation."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
from nexura_runtime.contracts import ExecutionContext
from nexura_runtime.contracts.canonical import canonical_digest
from nexura_runtime.tools import tool

from nexura_runtime.runtime.approvals import (
    ApprovalAlreadyDecidedError,
    ApprovalClaimConflictError,
    ApprovalDecisionRecord,
    ApprovalError,
    ApprovalExpiredError,
    ApprovalGate,
    ApprovalModule,
    ApprovalPolicyManager,
    ApprovalRequest,
    ApprovalRequiredError,
    MutatedArgumentsError,
    WrongActorError,
    WrongWorkspaceError,
)
from nexura_runtime.runtime.effects import (
    EffectReconciler,
    ReconciliationError,
)


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    return tmp_path / "approvals_test.db"


@pytest.fixture
def manager(temp_db: Path) -> ApprovalPolicyManager:
    return ApprovalPolicyManager(temp_db)


@pytest.fixture
def gate(manager: ApprovalPolicyManager) -> ApprovalGate:
    return ApprovalGate(manager, default_expiry_seconds=300.0)


@pytest.fixture
def reconciler(temp_db: Path) -> EffectReconciler:
    return EffectReconciler(temp_db)


@pytest.fixture
def context() -> ExecutionContext:
    return ExecutionContext(
        workspace_id="ws_finance_corp",
        principal_id="user_alice",
        run_id="run_payment_100",
        node_id="approval_node_1",
        attempt_id="att_1",
    )


# ---------------------------------------------------------------------------
# 1. Concurrent approvals across processes dispatch at most once locally
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_concurrent_approvals_dispatch_at_most_once(
    manager: ApprovalPolicyManager,
    gate: ApprovalGate,
    context: ExecutionContext,
) -> None:
    # 1. Create request and approve it
    # B43: an approval is bound to its dispatch identity (run, node, action name, ...), so
    # the request names the action and every racing worker presents that same action.
    req = ApprovalRequest(
        approval_id="appr_concurrent_test",
        workspace_id=context.workspace_id,
        run_id=context.run_id,
        node_id="worker_step",
        action_name="execute_wire",
        action_payload={"tx_id": "TX9900", "amount": 10000},
        required_role="approver",
        expires_at=time.time() + 600,
    )
    manager.create_request(req)

    decision = ApprovalDecisionRecord(
        approval_id=req.approval_id,
        workspace_id=context.workspace_id,
        decided_by_principal_id="manager_bob",
        decided_by_role="approver",
        decision="approved",
        expected_payload_digest=req.payload_digest,
    )
    manager.submit_decision(context.workspace_id, req.approval_id, decision)

    # 2. Simulate 10 concurrent workers racing to dispatch the effect
    dispatch_count = 0
    conflict_count = 0

    async def _worker_task(worker_idx: int) -> None:
        nonlocal dispatch_count, conflict_count
        worker_ctx = ExecutionContext(
            workspace_id=context.workspace_id,
            principal_id=f"worker_{worker_idx}",
            run_id=context.run_id,
            node_id="worker_step",
            attempt_id="att_1",
        )
        try:
            await gate.execute_gated_action(
                worker_ctx,
                action_name="execute_wire",
                payload={"tx_id": "TX9900", "amount": 10000},
                handler=lambda: _perform_side_effect(),
                approval_id=req.approval_id,
            )
        except ApprovalClaimConflictError:
            conflict_count += 1

    async def _perform_side_effect() -> str:
        nonlocal dispatch_count
        dispatch_count += 1
        await asyncio.sleep(0.01)
        return "SUCCESS"

    tasks = [_worker_task(i) for i in range(10)]
    await asyncio.gather(*tasks)

    # Exactly one worker succeeded; all 9 others encountered claim conflict
    assert dispatch_count == 1, f"Expected exactly 1 dispatch, got {dispatch_count}"
    assert conflict_count == 9, f"Expected 9 claim conflicts, got {conflict_count}"


# ---------------------------------------------------------------------------
# 2. Stale/wrong-actor/wrong-workspace/mutated-argument decisions reject
# ---------------------------------------------------------------------------
def test_decision_rejection_matrix(
    manager: ApprovalPolicyManager,
    context: ExecutionContext,
) -> None:
    now = time.time()
    payload = {"account": "ACC-789", "amount": 5000}
    correct_digest = canonical_digest(payload)

    # A. Stale / Expired Approval
    expired_req = ApprovalRequest(
        approval_id="appr_expired",
        workspace_id="ws_finance_corp",
        run_id="run_exp",
        action_payload=payload,
        required_role="compliance",
        expires_at=now - 100,  # in past
    )
    manager.create_request(expired_req)

    dec_expired = ApprovalDecisionRecord(
        approval_id="appr_expired",
        workspace_id="ws_finance_corp",
        decided_by_principal_id="officer_1",
        decided_by_role="compliance",
        decision="approved",
        expected_payload_digest=correct_digest,
    )
    with pytest.raises(ApprovalExpiredError):
        manager.submit_decision("ws_finance_corp", "appr_expired", dec_expired)

    # B. Wrong Actor / Role
    actor_req = ApprovalRequest(
        approval_id="appr_actor_check",
        workspace_id="ws_finance_corp",
        run_id="run_act",
        action_payload=payload,
        required_role="cfo",
        expires_at=now + 1000,
    )
    manager.create_request(actor_req)

    dec_wrong_actor = ApprovalDecisionRecord(
        approval_id="appr_actor_check",
        workspace_id="ws_finance_corp",
        decided_by_principal_id="intern_dave",
        decided_by_role="intern",  # not 'cfo'
        decision="approved",
        expected_payload_digest=correct_digest,
    )
    with pytest.raises(WrongActorError) as exc_actor:
        manager.submit_decision("ws_finance_corp", "appr_actor_check", dec_wrong_actor)
    assert exc_actor.value.details["deciding_role"] == "intern"
    assert exc_actor.value.details["required_role"] == "cfo"

    # C. Wrong Workspace
    ws_req = ApprovalRequest(
        approval_id="appr_ws_check",
        workspace_id="ws_finance_corp",
        run_id="run_ws",
        action_payload=payload,
        required_role="compliance",
        expires_at=now + 1000,
    )
    manager.create_request(ws_req)

    dec_wrong_ws = ApprovalDecisionRecord(
        approval_id="appr_ws_check",
        workspace_id="ws_other_division",  # wrong workspace
        decided_by_principal_id="officer_1",
        decided_by_role="compliance",
        decision="approved",
        expected_payload_digest=correct_digest,
    )
    with pytest.raises(WrongWorkspaceError) as exc_ws:
        manager.submit_decision("ws_other_division", "appr_ws_check", dec_wrong_ws)
    assert exc_ws.value.details["request_workspace"] == "ws_finance_corp"
    assert exc_ws.value.details["decision_workspace"] == "ws_other_division"

    # D. Mutated Arguments
    mut_req = ApprovalRequest(
        approval_id="appr_mutated_check",
        workspace_id="ws_finance_corp",
        run_id="run_mut",
        action_payload=payload,
        required_role="compliance",
        expires_at=now + 1000,
    )
    manager.create_request(mut_req)

    dec_mutated = ApprovalDecisionRecord(
        approval_id="appr_mutated_check",
        workspace_id="ws_finance_corp",
        decided_by_principal_id="officer_1",
        decided_by_role="compliance",
        decision="approved",
        expected_payload_digest="tampered_digest_value",
    )
    with pytest.raises(MutatedArgumentsError) as exc_mut:
        manager.submit_decision("ws_finance_corp", "appr_mutated_check", dec_mutated)
    assert exc_mut.value.details["expected_digest"] == "tampered_digest_value"

    # E. Duplicate Decision on Already Decided Request
    dec_valid = ApprovalDecisionRecord(
        approval_id="appr_mutated_check",
        workspace_id="ws_finance_corp",
        decided_by_principal_id="officer_1",
        decided_by_role="compliance",
        decision="rejected",
        expected_payload_digest=correct_digest,
    )
    manager.submit_decision("ws_finance_corp", "appr_mutated_check", dec_valid)

    # Attempting to re-decide raises ApprovalAlreadyDecidedError
    with pytest.raises(ApprovalAlreadyDecidedError):
        manager.submit_decision("ws_finance_corp", "appr_mutated_check", dec_valid)


# ---------------------------------------------------------------------------
# 3. Approval cannot be bypassed by a subagent or direct tool API
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_approval_cannot_be_bypassed_by_subagent_or_tool(
    gate: ApprovalGate,
    manager: ApprovalPolicyManager,
    context: ExecutionContext,
) -> None:
    executed_tools: list[str] = []

    # Define a tool with critical side effects
    @tool(name="issue_customer_refund", description="Issues customer refund")
    def refund_tool(customer_id: str, amount: float) -> dict[str, Any]:
        executed_tools.append(customer_id)
        return {"refund_id": "REF-001", "amount": amount}

    # Wrap tool with approval gate
    gated_refund = gate.wrap_tool(refund_tool, required_role="finance_lead")

    # 1. Direct tool execution without approval pauses and creates pending approval
    with pytest.raises(ApprovalRequiredError) as exc_req:
        gated_refund.run(customer_id="CUST-77", amount=450.0, context=context)

    appr_id = exc_req.value.details["approval_id"]
    assert len(executed_tools) == 0, "Tool must NOT execute before approval!"

    # 2. Calling with a forged approval ID is rejected
    with pytest.raises(ApprovalError):
        gated_refund.run(
            customer_id="CUST-77",
            amount=450.0,
            context=context,
            approval_id="forged_approval_id_xyz",
        )
    assert len(executed_tools) == 0

    # 3. Calling with altered arguments after approval is rejected
    decision = ApprovalDecisionRecord(
        approval_id=appr_id,
        workspace_id=context.workspace_id,
        decided_by_principal_id="lead_carol",
        decided_by_role="finance_lead",
        decision="approved",
        expected_payload_digest=exc_req.value.details["payload_digest"],
    )
    manager.submit_decision(context.workspace_id, appr_id, decision)

    # Caller tries to change amount from 450 to 9999
    with pytest.raises(MutatedArgumentsError):
        gated_refund.run(
            customer_id="CUST-77",
            amount=9999.0,  # mutated!
            context=context,
            approval_id=appr_id,
        )
    assert len(executed_tools) == 0

    # 4. Calling with the exact approved arguments executes successfully
    result = gated_refund.run(
        customer_id="CUST-77",
        amount=450.0,
        context=context,
        approval_id=appr_id,
    )
    assert result["refund_id"] == "REF-001"
    assert executed_tools == ["CUST-77"]


# ---------------------------------------------------------------------------
# 4. Lost response stays indeterminate until explicitly reconciled
# ---------------------------------------------------------------------------
def test_lost_response_stays_indeterminate(
    reconciler: EffectReconciler,
    temp_db: Path,
) -> None:
    workspace = "ws_brokerage"
    effect_id = "eff_order_881"
    idempotency_key = "idem_key_881"
    payload = {"symbol": "INFY", "quantity": 100, "action": "BUY"}

    # 1. Record pre-dispatch intent
    reconciler.record_intent(
        workspace_id=workspace,
        effect_id=effect_id,
        run_id="run_trade_88",
        action_name="place_order",
        idempotency_key=idempotency_key,
        payload=payload,
    )

    # 2. Simulate transport connection drop / lost response
    reconciler.record_indeterminate(
        workspace, effect_id, reason="Brokerage gateway ECONNRESET after POST"
    )

    eff = reconciler.get_effect(workspace, effect_id)
    assert eff is not None
    assert eff.status == "indeterminate"
    assert "ECONNRESET" in (eff.reconciliation_notes or "")

    # 3. Reopen storage from fresh instance to verify durability
    reopened = EffectReconciler(temp_db)
    dur_eff = reopened.get_effect(workspace, effect_id)
    assert dur_eff is not None
    assert dur_eff.status == "indeterminate"

    # 4. Reconcile explicitly with audit trail
    reconciled = reopened.reconcile_effect(
        workspace_id=workspace,
        effect_id=effect_id,
        resolution="confirmed",
        notes="Order #ORD-772 confirmed directly in exchange order book",
    )
    assert reconciled.status == "reconciled"
    assert "CONFIRMED" in (reconciled.reconciliation_notes or "")

    # 5. Non-indeterminate effect cannot be reconciled
    with pytest.raises(ReconciliationError):
        reopened.reconcile_effect(
            workspace_id=workspace,
            effect_id=effect_id,
            resolution="voided",
            notes="Second reconciliation attempt",
        )


# ---------------------------------------------------------------------------
# 5. ApprovalModule B00 Contract Integration
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_approval_module_b00_contract(
    manager: ApprovalPolicyManager,
    context: ExecutionContext,
) -> None:
    module = ApprovalModule(manager=manager)

    # Create approval via module
    res_create = await module.execute(
        context,
        inputs={
            "operation": "create",
            "workspace_id": context.workspace_id,
            "action_name": "approve_budget",
            "required_role": "director",
            "action_payload": {"dept": "AI", "budget": 50000},
        },
        config={},
    )
    assert res_create.status == "success"
    appr_id = res_create.outputs["approval_id"]
    assert res_create.outputs["approval_status"] == "pending"

    # Decide approval via module. B43 review: the deciding role comes from the execution
    # context (the principal running the node), never from the module inputs; an input
    # decided_by_role must agree with it.
    decider = ExecutionContext(
        workspace_id=context.workspace_id,
        principal_id=context.principal_id,
        run_id=context.run_id,
        node_id=context.node_id,
        attributes={"principal_role": "director"},
    )
    res_decide = await module.execute(
        decider,
        inputs={
            "operation": "decide",
            "workspace_id": context.workspace_id,
            "approval_id": appr_id,
            "decided_by_role": "director",
            "decision": "approved",
            "reason": "Q3 budget allocated",
        },
        config={},
    )
    assert res_decide.status == "success"
    assert res_decide.outputs["approval_status"] == "approved"

    # Query approval via module
    res_get = await module.execute(
        context,
        inputs={
            "operation": "get",
            "workspace_id": context.workspace_id,
            "approval_id": appr_id,
        },
        config={},
    )
    assert res_get.status == "success"
    assert res_get.outputs["approval_status"] == "approved"
