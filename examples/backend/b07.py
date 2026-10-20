"""B07 Standalone Example: Approval Policies and Effect Reconciliation.

Demonstrates:
1. Pausing an order financial action requiring human/compliance approval.
2. Closing and reopening persistent SQLite storage to verify durability.
3. Rejecting an approval request and proving zero side-effects are dispatched.
4. Approving a separate run with an immutable audit record and executing the effect.
5. Tracking external mutations with EffectReconciler and handling a lost response
   (indeterminate state).
6. Explicit operator reconciliation of the indeterminate effect.
7. Executing approval management via B01 CLI interface.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

from nexura_runtime.contracts import ExecutionContext

from nexura_runtime.modules.cli import _run_command
from nexura_runtime.modules.registry import VersionedModuleRegistry
from nexura_runtime.runtime.approvals import (
    ApprovalDecisionRecord,
    ApprovalGate,
    ApprovalModule,
    ApprovalPolicyManager,
    ApprovalRequiredError,
)
from nexura_runtime.runtime.effects import EffectReconciler


async def main() -> None:
    print("=== B07: Approval Policies and Effect Reconciliation Demonstration ===\n")

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "approvals_and_effects.db"

        # -------------------------------------------------------------
        # 1. Initialize Approval Manager & Gate
        # -------------------------------------------------------------
        print("1. Initializing ApprovalPolicyManager with SQLite storage...")
        manager = ApprovalPolicyManager(db_path)
        gate = ApprovalGate(manager, default_expiry_seconds=3600.0)
        reconciler = EffectReconciler(db_path)

        dispatched_effects: list[dict[str, Any]] = []

        async def simulated_wire_transfer(amount: float, beneficiary: str) -> dict[str, Any]:
            """External side-effect that must NEVER run without confirmed approval."""
            print(
                f"  [DISPATCHED EFFECT] Wire transfer ${amount} to {beneficiary} "
                "successfully executed!"
            )
            record = {"amount": amount, "beneficiary": beneficiary, "status": "completed"}
            dispatched_effects.append(record)
            return record

        # -------------------------------------------------------------
        # 2. Run 1: High-value order paused for approval
        # -------------------------------------------------------------
        workspace = "ws_mumbai_trade"
        context_run1 = ExecutionContext(
            workspace_id=workspace,
            principal_id="trader_vikram",
            run_id="order_run_001",
            node_id="wire_transfer_step",
            attempt_id="att_1",
        )
        order_payload_1 = {
            "amount": 750000.0,
            "currency": "INR",
            "beneficiary": "Supplier Pvt Ltd",
        }

        print("\n2. Run 1: Initiating high-value transfer ($750,000 INR)...")
        appr_id_1: str = ""
        try:
            await gate.execute_gated_action(
                context_run1,
                action_name="wire_transfer",
                payload=order_payload_1,
                handler=lambda: simulated_wire_transfer(750000.0, "Supplier Pvt Ltd"),
                required_role="compliance_officer",
            )
        except ApprovalRequiredError as exc:
            appr_id_1 = str(exc.details["approval_id"])
            print(f"  PAUSED: Action paused! Created Approval Request: {appr_id_1}")
            print(f"  Required Role: {exc.details['required_role']}")
            print(f"  Payload Digest: {exc.details['payload_digest']}")

        assert len(dispatched_effects) == 0, "Zero effects must be dispatched before approval!"

        # -------------------------------------------------------------
        # 3. Simulate process restart / reopen storage
        # -------------------------------------------------------------
        print("\n3. Reopening SQLite storage to verify persistence across restarts...")
        reopened_manager = ApprovalPolicyManager(db_path)
        reopened_gate = ApprovalGate(reopened_manager)
        fetched_req1 = reopened_manager.get_request(workspace, appr_id_1)
        assert fetched_req1 is not None
        assert fetched_req1.status == "pending"
        print(
            f"  Durably restored approval request: {fetched_req1.approval_id} "
            f"(Status: {fetched_req1.status})"
        )

        # -------------------------------------------------------------
        # 4. Reject Run 1 and verify zero side-effects dispatched
        # -------------------------------------------------------------
        print("\n4. Compliance officer rejects Run 1...")
        rejection_decision = ApprovalDecisionRecord(
            approval_id=appr_id_1,
            workspace_id=workspace,
            decided_by_principal_id="officer_priya",
            decided_by_role="compliance_officer",
            decision="rejected",
            reason="Beneficiary account failed AML screening",
            expected_payload_digest=fetched_req1.payload_digest,
        )
        rejected_req = reopened_manager.submit_decision(workspace, appr_id_1, rejection_decision)
        print(f"  Approval status updated to: {rejected_req.status}")
        assert rejected_req.status == "rejected"

        # Attempt to resume Run 1 with rejected approval
        print("  Attempting to execute Run 1 after rejection...")
        try:
            await reopened_gate.execute_gated_action(
                context_run1,
                action_name="wire_transfer",
                payload=order_payload_1,
                handler=lambda: simulated_wire_transfer(750000.0, "Supplier Pvt Ltd"),
                approval_id=appr_id_1,
            )
        except Exception as exc:
            print(f"  Execution blocked as expected: {exc}")

        assert len(dispatched_effects) == 0, (
            "Zero effects must be dispatched for rejected requests!"
        )

        # -------------------------------------------------------------
        # 5. Run 2: Separate order, approved and executed
        # -------------------------------------------------------------
        context_run2 = ExecutionContext(
            workspace_id=workspace,
            principal_id="trader_vikram",
            run_id="order_run_002",
            node_id="wire_transfer_step",
            attempt_id="att_1",
        )
        order_payload_2 = {
            "amount": 250000.0,
            "currency": "INR",
            "beneficiary": "Verified Partner Ltd",
        }

        print("\n5. Run 2: Initiating approved transfer ($250,000 INR)...")
        appr_id_2: str = ""
        try:
            await reopened_gate.execute_gated_action(
                context_run2,
                action_name="wire_transfer",
                payload=order_payload_2,
                handler=lambda: simulated_wire_transfer(250000.0, "Verified Partner Ltd"),
                required_role="compliance_officer",
            )
        except ApprovalRequiredError as exc:
            appr_id_2 = str(exc.details["approval_id"])
            print(f"  PAUSED: Action paused! Created Approval Request: {appr_id_2}")

        req2 = reopened_manager.get_request(workspace, appr_id_2)
        assert req2 is not None

        # Record Approval Decision
        approval_decision = ApprovalDecisionRecord(
            approval_id=appr_id_2,
            workspace_id=workspace,
            decided_by_principal_id="officer_priya",
            decided_by_role="compliance_officer",
            decision="approved",
            reason="Valid KYC and verified beneficiary documentation",
            expected_payload_digest=req2.payload_digest,
        )
        reopened_manager.submit_decision(workspace, appr_id_2, approval_decision)
        print(f"  Decision submitted: APPROVED by {approval_decision.decided_by_principal_id}")

        # Resume Run 2 with approved token
        print("  Resuming Run 2 with approved token...")
        run2_result = await reopened_gate.execute_gated_action(
            context_run2,
            action_name="wire_transfer",
            payload=order_payload_2,
            handler=lambda: simulated_wire_transfer(250000.0, "Verified Partner Ltd"),
            approval_id=appr_id_2,
        )
        print(f"  Run 2 result: {run2_result}")
        assert len(dispatched_effects) == 1, "Exactly one side effect must be dispatched!"

        # -------------------------------------------------------------
        # 6. Effect Reconciliation: Lost Response & Manual Resolution
        # -------------------------------------------------------------
        print("\n6. Demonstrating Effect Tracking and Indeterminate Reconciliation...")
        effect_id = "eff_tx_9011"
        idempotency_key = "idem_order_002_tx"

        # Record pre-dispatch intent
        intent = reconciler.record_intent(
            workspace_id=workspace,
            effect_id=effect_id,
            run_id="order_run_002",
            action_name="wire_transfer",
            idempotency_key=idempotency_key,
            payload=order_payload_2,
        )
        print(f"  Pre-dispatch intent recorded: {intent.effect_id} (Status: {intent.status})")

        # Simulate timeout / lost response during remote call
        print("  [Simulated event] Network dropped after packet send; response was lost!")
        indeterminate_effect = reconciler.record_indeterminate(
            workspace, effect_id, reason="Gateway socket read timeout"
        )
        print(f"  Effect status marked: {indeterminate_effect.status}")
        assert indeterminate_effect.status == "indeterminate"

        # Operator reconciles with banking audit confirmation
        reconciled = reconciler.reconcile_effect(
            workspace_id=workspace,
            effect_id=effect_id,
            resolution="confirmed",
            notes="Bank transaction ref TXN998823 confirmed on ledger statement",
        )
        print(f"  Effect explicitly reconciled! Final Status: {reconciled.status}")
        print(f"  Notes: {reconciled.reconciliation_notes}")
        assert reconciled.status == "reconciled"

        # -------------------------------------------------------------
        # 7. Invocation through B01 Module CLI
        # -------------------------------------------------------------
        print("\n7. Executing approval query via B01 CLI interface:")
        registry = VersionedModuleRegistry()
        approval_module = ApprovalModule(manager=reopened_manager)
        registry.register(approval_module)

        cli_args = argparse.Namespace(
            module_id="nexura.runtime.approvals",
            version="1.0.0",
            input=json.dumps(
                {
                    "operation": "get",
                    "workspace_id": workspace,
                    "approval_id": appr_id_2,
                }
            ),
            config="{}",
            workspace=workspace,
            principal="auditor_raj",
            run_id="audit_run_01",
            node_id="cli_audit_node",
        )
        cli_code = await _run_command(registry, cli_args)
        assert cli_code == 0, "CLI execution failed"
        print("CLI Execution: SUCCESS (code 0)")

    print("\n=== B07 Example Completed Successfully ===")


if __name__ == "__main__":
    asyncio.run(main())
