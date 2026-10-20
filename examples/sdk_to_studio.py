"""SDK-to-Studio integration example demonstrating custom module registration and execution.

Demonstrates:
1. Registering custom trusted Python handlers in a dedicated ModuleRegistry.
2. Defining and validating an approval-gated WorkflowSpec.
3. Instantiating a local Studio FastAPI application via create_app(registry=..., db_path=...).
4. Interacting programmatically with Studio's REST API endpoints (workflows, runs, approvals).
5. Pausing at the approval gate, granting supervisor approval, and verifying final state.
6. Completely keyless and self-contained; runs in a task-temporary directory.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

import httpx
from httpx import ASGITransport
from nexura_studio import create_app  # pip install nexura-studio

from nexura_runtime.workflow import (
    HandlerOutput,
    ModuleRegistry,
    WorkflowNode,
    WorkflowSpec,
)


def build_custom_registry() -> ModuleRegistry:
    """Build a custom ModuleRegistry with domain-specific trusted handlers."""
    registry = ModuleRegistry()

    @registry.register_handler("validate_customer_cart")
    def validate_cart(state: dict[str, Any]) -> dict[str, Any]:
        customer = state.get("customer", "Guest Customer")
        cart_total = float(state.get("cart_total", 0))
        discount_code = state.get("discount_code", "")
        return {
            "customer": customer,
            "cart_total": cart_total,
            "discount_code": discount_code,
            "cart_validated": True,
        }

    @registry.register_handler("discount_policy_router")
    def discount_router(state: dict[str, Any]) -> HandlerOutput:
        cart_total = float(state.get("cart_total", 0))
        discount_code = state.get("discount_code", "")

        # High discount (> ₹1000) or VIP code requires human approval
        if discount_code == "VIP-SPECIAL" or cart_total >= 5000:
            return HandlerOutput(
                state_update={
                    "discount_rate": 0.25,
                    "requires_manual_approval": True,
                    "routing_note": "High-value discount requires supervisor approval.",
                },
                branch="manual_approval",
            )
        return HandlerOutput(
            state_update={
                "discount_rate": 0.05,
                "requires_manual_approval": False,
                "routing_note": "Standard 5% discount applied automatically.",
            },
            branch="auto_apply",
        )

    @registry.register_handler("apply_discount_gate")
    def apply_discount_gate(state: dict[str, Any]) -> dict[str, Any]:
        # Gated effect: only executes after human operator approval
        cart_total = float(state.get("cart_total", 0))
        discount_rate = float(state.get("discount_rate", 0))
        savings = round(cart_total * discount_rate, 2)
        final_price = round(cart_total - savings, 2)

        return {
            "savings": savings,
            "final_price": final_price,
            "discount_approved": True,
            "approval_code": "AUTH-DISCOUNT-8822",
        }

    @registry.register_handler("finalize_checkout")
    def finalize_checkout(state: dict[str, Any]) -> dict[str, Any]:
        customer = state.get("customer", "Customer")
        final_price = state.get("final_price", state.get("cart_total"))
        receipt_id = "REC-DELHI-4401"
        confirmation = (
            f"आदेश स्वीकृत (Order Confirmed): ₹{final_price} charged to {customer}. "
            f"रसीद संख्या: {receipt_id}"
        )
        return {
            "receipt_id": receipt_id,
            "confirmation": confirmation,
            "order_status": "CONFIRMED",
        }

    return registry


def build_custom_workflow() -> WorkflowSpec:
    """Build an approval-gated e-commerce discount workflow."""
    nodes = {
        "validate": WorkflowNode(
            id="validate",
            handler="validate_customer_cart",
            default_transition="route_discount",
        ),
        "route_discount": WorkflowNode(
            id="route_discount",
            handler="discount_policy_router",
            transitions={
                "manual_approval": "approval_gate",
                "auto_apply": "checkout",
            },
            default_transition="approval_gate",
        ),
        "approval_gate": WorkflowNode(
            id="approval_gate",
            handler="apply_discount_gate",
            requires_approval=True,  # Execution pauses BEFORE this node runs
            default_transition="checkout",
        ),
        "checkout": WorkflowNode(
            id="checkout",
            handler="finalize_checkout",
            default_transition=None,  # Terminal
        ),
    }

    return WorkflowSpec(
        name="CartDiscountApprovalWorkflow",
        entry_node="validate",
        nodes=nodes,
        spec_version=1,
        terminal_nodes=["checkout"],
    )


async def main() -> None:
    print("=" * 70)
    print("Nexura AgentKit - Custom SDK-to-Studio Integration Example")
    print("======================================================================")

    # 1. Initialize custom registry and workflow
    custom_registry = build_custom_registry()
    custom_spec = build_custom_workflow()
    print(f"1. Registered custom handlers: {list(custom_registry._handlers.keys())}")
    print(f"   Created workflow spec: '{custom_spec.name}' (Entry: '{custom_spec.entry_node}')")

    with tempfile.TemporaryDirectory(prefix="studio_example_") as tmp_dir:
        db_path = Path(tmp_dir) / "custom_studio.sqlite3"
        print(f"\n2. Creating Studio app instance on SQLite: {db_path}")

        # 2. Create the Studio application with custom registry
        app = create_app(registry=custom_registry, db_path=db_path)

        # Studio accepts loopback hosts and peers only, and every state-changing request
        # carries the per-session token (a browser page reads it from Studio's own origin).
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1",
            headers={"X-Nexura-Session": app.state.session_token},
        ) as client:
            # 3. Verify custom modules are exposed via Studio REST API
            mod_res = await client.get("/api/v1/modules")
            assert mod_res.status_code == 200
            registered_names = [m["name"] for m in mod_res.json()["modules"]]
            print(f"✓ Studio API reported {len(registered_names)} modules: {registered_names}")
            assert "apply_discount_gate" in registered_names

            # 4. Save custom workflow into Studio
            wf_res = await client.post(
                "/api/v1/workflows",
                json={
                    "workflow_id": "wf-custom-discount",
                    "spec": custom_spec.to_dict(),
                },
            )
            assert wf_res.status_code == 201
            wf_data = wf_res.json()
            print(
                f"✓ Workflow saved in Studio: ID='{wf_data['workflow_id']}', "
                f"Revision={wf_data['revision']}"
            )

            # 5. Start an execution run requiring approval
            initial_state = {
                "customer": "राजेश कुमार (Rajesh Kumar)",
                "cart_total": 7500,
                "discount_code": "VIP-SPECIAL",
            }
            print(f"\n3. Starting execution run for customer: {initial_state['customer']}")
            print(
                f"   Cart Total: ₹{initial_state['cart_total']} | "
                f"Code: {initial_state['discount_code']}"
            )

            run_res = await client.post(
                "/api/v1/runs",
                json={
                    "workflow_id": "wf-custom-discount",
                    "initial_state": initial_state,
                },
            )
            assert run_res.status_code == 201
            run_data = run_res.json()
            run_id = run_data["run_id"]
            print(f"✓ Run created: {run_id} | Status: {run_data['status'].upper()}")

            assert run_data["status"] == "paused"
            pending = run_data["pending_approval"]
            assert pending is not None
            print("\n--- Approval Gate Reached in Studio ---")
            print(f"  Approval ID:  {pending['approval_id']}")
            print(f"  Gated Node:   {pending['node_id']}")
            print(f"  Discount:     {run_data['current_state'].get('discount_rate') * 100}%")
            print(f"  Route Note:   {run_data['current_state'].get('routing_note')}")
            assert "savings" not in run_data["current_state"], "Gated effect executed prematurely!"

            # 6. Resolve approval via Studio decision endpoint
            print("\n4. Granting supervisor approval through Studio API...")
            supervisor_update = {
                "approved_by": "सुपरवाइज़र अनीता (Supervisor Anita)",
                "audit_notes": "VIP ग्राहक के लिए 25% छूट स्वीकृत। (Approved 25% discount)",
            }
            decide_res = await client.post(
                f"/api/v1/runs/{run_id}/decide",
                json={
                    "approval_id": pending["approval_id"],
                    "decision": "approve",
                    "state_update": supervisor_update,
                },
            )
            assert decide_res.status_code == 200
            final_run = decide_res.json()
            # The supervisor's notes edit the state the gated node reads, so the edited state
            # needs its own approval (an approval covers only what the approver was shown).
            assert final_run["status"] == "paused"
            print("✓ Edited state needs a fresh approval; approving the edited inputs")
            decide_res = await client.post(
                f"/api/v1/runs/{run_id}/decide",
                json={
                    "approval_id": final_run["pending_approval"]["approval_id"],
                    "decision": "approve",
                },
            )
            assert decide_res.status_code == 200
            final_run = decide_res.json()
            print(f"✓ Run resumed and completed: Status={final_run['status'].upper()}")
            assert final_run["status"] == "completed"

            print("\n--- Final Order Summary ---")
            state_out = final_run["current_state"]
            print(f"  Customer:      {state_out['customer']}")
            print(f"  Original:      ₹{state_out['cart_total']}")
            print(f"  Savings:       ₹{state_out['savings']}")
            print(f"  Final Price:   ₹{state_out['final_price']}")
            print(f"  Approved By:   {state_out['approved_by']}")
            print(f"  Confirmation:  {state_out['confirmation']}")

            # Verify lifecycle events
            print(f"\n5. Total Lifecycle Events Recorded: {len(final_run['events'])}")
            for ev in final_run["events"]:
                node_str = f" (node: {ev['node_id']})" if ev.get("node_id") else ""
                print(f"   [{ev['seq']}] {ev['type']}{node_str}")

        await app.state.store.close()
        print("\nStudio store closed cleanly.")

    print("\nTemporary database cleaned up.")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
