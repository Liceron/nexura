"""Keyless approval workflow example demonstrating durable human-in-the-loop gates.

Local Trust Boundary:
- Imported WorkflowSpec definitions only reference pre-registered trusted Python
  callables in the ModuleRegistry. Dynamic imports, eval, and arbitrary code
  execution from workflow JSON payloads are explicitly prohibited.
- The approval API is designed for local trusted callers and operators; it does not
  provide multi-tenant authorization or remote privilege boundaries.

Demonstrates:
1. Exporting and importing WorkflowSpec JSON (for future Studio compatibility).
2. Looking up a synthetic order with Indic (Hindi/English) Unicode content.
3. Pausing before an approval-gated node and emitting a typed PendingApproval request.
4. Recreating the workflow engine and SQLite store instance from the same local DB.
5. Resuming with explicit approval: a state update that changes the gated node's inputs
   is re-gated for a fresh approval, which is then approved and records a synthetic ticket.
6. Printing final outcomes with Hindi/English text intact.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

from nexura_runtime.workflow import (
    ModuleRegistry,
    SQLiteWorkflowStore,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
)

# Synthetic in-memory orders database
SYNTHETIC_ORDERS: dict[str, dict[str, Any]] = {
    "ORD-DELHI-501": {
        "order_id": "ORD-DELHI-501",
        "customer": "अमित वर्मा (Amit Verma)",
        "amount": 4500,
        "item": "स्मार्टफोन डिस्प्ले (Smartphone Display)",
        "issue_notes": (
            "उत्पाद क्षतिग्रस्त मिला। ग्राहक ने पूर्ण वापसी का अनुरोध किया है। "
            "(Product damaged. Full refund requested.)"
        ),
    }
}


def build_registry() -> ModuleRegistry:
    """Register trusted Python handlers for the workflow."""
    registry = ModuleRegistry()

    @registry.register_handler("lookup_order")
    def lookup_order(state: dict[str, Any]) -> dict[str, Any]:
        order_id = state.get("order_id", "")
        order = SYNTHETIC_ORDERS.get(order_id)
        if not order:
            return {"error": f"Order '{order_id}' not found", "found": False}
        return {
            "found": True,
            "customer": order["customer"],
            "amount": order["amount"],
            "item": order["item"],
            "issue_notes": order["issue_notes"],
        }

    @registry.register_handler("process_refund_gate")
    def process_refund_gate(state: dict[str, Any]) -> dict[str, Any]:
        # Gated effect: this only executes AFTER human approval is granted
        return {
            "refund_authorized": True,
            "refund_amount": state.get("amount", 0),
            "authorization_code": "AUTH-REFUND-9901",
        }

    @registry.register_handler("record_ticket")
    def record_ticket(state: dict[str, Any]) -> dict[str, Any]:
        ticket_id = "TICKET-DELHI-8821"
        resolution = (
            f"रिफंड स्वीकृत: ₹{state.get('amount')} ग्राहक {state.get('customer')} को जारी किया गया। "
            f"(Refund approved and processed under auth {state.get('authorization_code')})"
        )
        return {
            "ticket_id": ticket_id,
            "resolution": resolution,
            "status": "RESOLVED_REFUNDED",
        }

    return registry


def build_workflow_spec() -> WorkflowSpec:
    """Construct the WorkflowSpec graph."""
    nodes = {
        "lookup": WorkflowNode(
            id="lookup",
            handler="lookup_order",
            default_transition="approval_gate",
        ),
        "approval_gate": WorkflowNode(
            id="approval_gate",
            handler="process_refund_gate",
            requires_approval=True,  # Execution pauses BEFORE this handler runs
            default_transition="record",
        ),
        "record": WorkflowNode(
            id="record",
            handler="record_ticket",
            default_transition=None,  # Terminal node
        ),
    }

    return WorkflowSpec(
        name="RefundApprovalWorkflow",
        entry_node="lookup",
        nodes=nodes,
        spec_version=1,
        terminal_nodes=["record"],
    )


async def main() -> None:
    print("=" * 70)
    print("Nexura AgentKit - Human-in-the-Loop Approval Workflow Example")
    print("=" * 70)

    registry = build_registry()
    original_spec = build_workflow_spec()

    # 1. Export WorkflowSpec to JSON and import it back (Studio contract verification)
    spec_json = original_spec.to_json(indent=2)
    print("\n1. Exported WorkflowSpec JSON (Studio Compatible):")
    print(spec_json)

    imported_spec = WorkflowSpec.from_json(spec_json)
    assert imported_spec.canonical_hash() == original_spec.canonical_hash()
    print("✓ Imported WorkflowSpec verified (canonical SHA-256 matches).")

    # Use a task temporary directory by default
    with tempfile.TemporaryDirectory(prefix="nexura_workflow_") as tmp_dir:
        db_path = Path(tmp_dir) / "workflow_checkpoints.db"
        print(f"\nUsing temporary SQLite database: {db_path}")

        # 2. Start initial execution with Engine 1
        store1 = SQLiteWorkflowStore(db_path)
        engine1 = WorkflowEngine(imported_spec, registry, store=store1)

        initial_state = {"order_id": "ORD-DELHI-501"}
        print(f"\n2. Starting workflow run for order: {initial_state['order_id']}")
        run_res = await engine1.run(initial_state)

        print(f"Status: {run_res.status.upper()}")
        print(f"Current Node: {run_res.current_node_id}")
        assert run_res.status == "paused"
        assert run_res.pending_approval is not None

        pending = run_res.pending_approval
        print("\n--- Pending Approval Request Generated ---")
        print(f"  Approval ID:     {pending.approval_id}")
        print(f"  Workflow Run ID: {pending.workflow_run_id}")
        print(f"  Gated Node:      {pending.node_id}")
        print(f"  Customer:        {pending.input_state.get('customer')}")
        print(f"  Issue Notes:     {pending.input_state.get('issue_notes')}")
        print(f"  Refund Amount:   ₹{pending.input_state.get('amount')}")

        # Close Engine 1 and store1 to simulate process boundary / restart
        await store1.close()
        print("\nEngine 1 store closed. SQLite checkpoint is ready for a fresh engine instance.")

        # 3. Simulate new operator session: create Engine 2 on same database
        print("\n3. Starting fresh Engine 2 instance on existing SQLite DB...")
        store2 = SQLiteWorkflowStore(db_path)
        engine2 = WorkflowEngine(imported_spec, registry, store=store2)

        # 4. Explicitly approve the pending request with an operator state update
        print("Explicitly approving pending request with supervisor audit note...")
        approval_update = {
            "approved_by": "सुपरवाइज़र (Supervisor) Priya Rao",
            "approval_comment": "क्षतिग्रस्त स्क्रीन सत्यापित। वापसी स्वीकृत। (Screen verified damaged)",
        }

        resume_res = await engine2.resume(
            run_res.workflow_run_id,
            approval_id=pending.approval_id,
            decision="approve",
            state_update=approval_update,
        )

        # The approval covers the inputs the reviewer saw. A function handler reads the
        # whole state, so the audit note changes what the gated node would read: the engine
        # does not carry the approval over and asks again, showing the updated state.
        if resume_res.status == "paused":
            assert resume_res.pending_approval is not None
            fresh = resume_res.pending_approval
            assert fresh.approval_id != pending.approval_id
            assert fresh.input_state["approved_by"] == approval_update["approved_by"]
            print("\nThe state update changed the gated node's inputs, so it was re-gated.")
            print(f"  Fresh Approval ID: {fresh.approval_id}")
            print("Approving the fresh request (it includes the audit note)...")
            resume_res = await engine2.resume(
                run_res.workflow_run_id,
                approval_id=fresh.approval_id,
                decision="approve",
            )

        print(f"\nFinal Execution Status: {resume_res.status.upper()}")
        print(f"Total Workflow Steps: {resume_res.step_count}")
        print("\nFinal State Output (Hindi/English Intact):")
        for key in [
            "customer",
            "item",
            "issue_notes",
            "refund_authorized",
            "authorization_code",
            "approved_by",
            "ticket_id",
            "resolution",
        ]:
            print(f"  {key.ljust(20)}: {resume_res.state.get(key)}")

        assert resume_res.status == "completed"
        assert resume_res.state["refund_authorized"] is True
        assert resume_res.state["ticket_id"] == "TICKET-DELHI-8821"

        await store2.close()
        print("\nStore 2 closed cleanly.")

    print("\nTemporary database cleaned up.")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
