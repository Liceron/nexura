"""Example B31: Backend API, Python/TypeScript Clients, and Standalone Workflow Export.

Demonstrates:
1. Ephemeral FastAPI backend service (`create_api_app`) with multi-tenant workspaces.
2. Python `NexuraClient` executing workflows with approval gates and SSE event cursors.
3. Explicit run cancellation semantics.
4. Standalone portable bundle export (`WorkflowExporter`) with secret/path scrubbing and execution.
5. TypeScript client interop via Node.js (`node --experimental-strip-types`).
6. Zero external APIs, zero paid credits, zero credentials required.
"""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import uvicorn

from nexura_runtime.service.api import (
    NexuraClient,
    WorkflowExporter,
    create_api_app,
)
from nexura_runtime.studio.builtin_modules import get_default_registry
from nexura_runtime.studio.store import StudioStore
from nexura_runtime.workflow import ModuleRegistry, WorkflowNode, WorkflowSpec

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def build_approval_workflow() -> WorkflowSpec:
    return WorkflowSpec(
        name="ProcurementWorkflow",
        entry_node="verify_request",
        nodes={
            "verify_request": WorkflowNode(
                id="verify_request",
                handler="verify_request",
                default_transition="director_approval",
            ),
            "director_approval": WorkflowNode(
                id="director_approval",
                handler="director_approval",
                requires_approval=True,
                default_transition="issue_po",
            ),
            "issue_po": WorkflowNode(
                id="issue_po",
                handler="issue_po",
            ),
        },
        terminal_nodes=["issue_po"],
    )


def build_demo_registry() -> ModuleRegistry:
    registry = get_default_registry()

    def verify_request(state: dict[str, Any]) -> dict[str, Any]:
        amount = state.get("amount", 25000)
        return {"verified": True, "amount": amount, "item": state.get("item", "Workstation")}

    def director_approval(state: dict[str, Any]) -> dict[str, Any]:
        return {"director_approved": True}

    def issue_po(state: dict[str, Any]) -> dict[str, Any]:
        return {"po_number": "PO-2026-IND-001", "status": "ISSUED"}

    def validate_order(state: dict[str, Any]) -> dict[str, Any]:
        return {"validated": True, "amount": state.get("amount", 100)}

    def manager_approval(state: dict[str, Any]) -> dict[str, Any]:
        return {"manager_approved": True}

    def fulfill_order(state: dict[str, Any]) -> dict[str, Any]:
        return {"fulfilled": True, "confirmation": "CONF-OK"}

    registry.register("verify_request", verify_request)
    registry.register("director_approval", director_approval)
    registry.register("issue_po", issue_po)
    registry.register("validate_order", validate_order)
    registry.register("manager_approval", manager_approval)
    registry.register("fulfill_order", fulfill_order)
    return registry


async def async_main() -> None:
    print("=" * 70)
    print("Nexura AgentKit - B31 Backend API, Clients & Standalone Export Demo")
    print("=" * 70)

    with tempfile.TemporaryDirectory(prefix="nexura_b31_demo_") as temp_dir:
        temp_path = Path(temp_dir)
        db_path = temp_path / "studio.sqlite3"

        # 1. Start ephemeral FastAPI server
        store = StudioStore(db_path=db_path)
        registry = build_demo_registry()
        app = create_api_app(store=store, registry=registry)

        port = find_free_port()
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()

        # Wait for server ready
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.05)

        base_url = f"http://127.0.0.1:{port}"
        print(f"[1] Ephemeral FastAPI Service listening at {base_url}")

        try:
            # 2. Python Client Workflow Execution
            client = NexuraClient(base_url=base_url, workspace_id="enterprise_ops")
            spec = build_approval_workflow()
            wf = await client.workflows.create(spec.to_dict(), workflow_id="wf_procurement")
            print(f"[2] Registered workflow '{wf['workflow_id']}' (rev: {wf['revision']})")

            # Start run
            run_snapshot = await client.runs.start(
                workflow_id="wf_procurement",
                initial_state={"amount": 75000, "item": "AI Server Cluster"},
            )
            run_id = run_snapshot["run_id"]
            print(f"[3] Initiated run {run_id} -> status: {run_snapshot['status']}")
            assert run_snapshot["status"] == "pending_approval"
            assert run_snapshot["pending_approval"] is not None

            approval_id = run_snapshot["pending_approval"]["approval_id"]
            print(f"    Pending approval ID: {approval_id}")

            # Replay events stream
            events = [e async for e in client.runs.stream_events(run_id, cursor=0)]
            event_types = [e.get("type") for e in events]
            print(f"    SSE Stream captured {len(events)} events (types: {event_types})")

            # Submit approval decision
            resumed_run = await client.runs.decide(
                run_id,
                approval_id=approval_id,
                decision="approve",
                state_update={"director_comment": "Approved within Q4 capital budget"},
            )
            print(f"[4] Resumed run after approval -> status: {resumed_run['status']}")
            # The comment edits the state the gated node reads, so the director approves the
            # edited state as a fresh request (an approval covers only what was shown).
            assert resumed_run["status"] == "pending_approval"
            resumed_run = await client.runs.decide(
                run_id,
                approval_id=resumed_run["pending_approval"]["approval_id"],
                decision="approve",
            )
            print(f"    Approved the edited state -> status: {resumed_run['status']}")
            assert resumed_run["status"] == "completed"
            assert resumed_run["current_state"]["po_number"] == "PO-2026-IND-001"
            print(f"    Final State: {resumed_run['current_state']}")

            # 3. Explicit Cancellation Demonstration
            run_to_cancel = await client.runs.start(
                workflow_id="wf_procurement",
                initial_state={"amount": 999999},
            )
            cancelled_run = await client.runs.cancel(
                run_to_cancel["run_id"],
                reason="Budget frozen by compliance audit",
            )
            print(
                f"[5] Explicit cancellation -> status: {cancelled_run['status']} "
                f"(error: {cancelled_run['error']})"
            )
            assert cancelled_run["status"] == "cancelled"

            # 4. Standalone Bundle Export & External Execution
            print("[6] Exporting workflow to standalone portable bundle...")
            bundle_dir = temp_path / "exported_procurement_bundle"
            export_spec = WorkflowSpec(
                name="PortableOrderLookup",
                entry_node="lookup",
                nodes={
                    "lookup": WorkflowNode(
                        id="lookup",
                        handler="synthetic_order_lookup",
                    )
                },
                terminal_nodes=["lookup"],
                metadata={
                    "api_key": "sk-secret-token-do-not-leak",
                    "host_source_path": "/home/ubuntu/projects/nexura-standalone-sdk/secret_data",
                },
            )
            exporter = WorkflowExporter()
            bundle = exporter.export_bundle(
                export_spec, target_dir=bundle_dir, workflow_id="portable_bundle"
            )
            print(f"    Export bundle written to: {bundle.target_dir}")
            print(f"    Generated bundle files: {bundle.files}")

            # Verify secret scrubbing
            wf_json = (bundle_dir / "workflow.json").read_text(encoding="utf-8")
            assert "sk-secret-token-do-not-leak" not in wf_json
            assert "/home/ubuntu/projects/nexura-standalone-sdk" not in wf_json
            parsed_meta = json.loads(wf_json).get("metadata", {})
            assert parsed_meta.get("api_key") == {"ref": "API_KEY"}
            print("    Credentials and local paths scrubbed successfully.")

            # Run standalone bundle
            print("    Executing exported bundle run.py in external directory...")
            res = subprocess.run(
                [sys.executable, str(bundle_dir / "run.py"), '{"order_id": "ORD-MUMBAI-202"}'],
                cwd=str(bundle_dir),
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert res.returncode == 0
            print(f"    Standalone execution output:\n{res.stdout.strip()}")

            # 5. TypeScript Client Interoperability
            print("[7] Testing TypeScript Client Interop via Node.js...")
            ts_runner = PROJECT_ROOT / "clients/typescript/test_runner.ts"
            ts_res = subprocess.run(
                ["node", "--experimental-strip-types", str(ts_runner), base_url],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert ts_res.returncode == 0, f"TS client failed:\n{ts_res.stderr}"
            print("    TypeScript client executed and verified successfully.")

            await client.close()
            print("=" * 70)
            print("B31 Verification Complete: API, Clients, and Export fully operational.")
            print("=" * 70)
        finally:
            server.should_exit = True
            thread.join(timeout=2.0)


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
