"""HTTP route integration tests for Nexura Studio.

Tests cover:
- Module descriptions & metadata
- Workflow CRUD & CAS revision conflicts
- Graph validation & untrusted module rejection
- Run execution & approval pauses before side effects
- Resumption with approval & zero-effect rejection
- Atomic duplicate decision protection (409 Conflict)
- Spec edit isolation (runs bind to creation snapshot)
- Persisted results survival across server restarts
- Interrupted run marking upon process restart
- Static UI asset serving
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import httpx
import pytest
from httpx import ASGITransport
from nexura_studio.server import create_app

from nexura_runtime.studio.builtin_modules import get_default_workflow_spec


@pytest.fixture
def temp_db_path() -> Any:
    with tempfile.TemporaryDirectory(prefix="studio_test_") as tmp_dir:
        yield Path(tmp_dir) / "studio_test.sqlite3"


@pytest.fixture
def app_and_client(temp_db_path: Path) -> Any:
    app = create_app(db_path=temp_db_path)
    transport = ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://testserver")
    yield app, client, temp_db_path


@pytest.mark.asyncio
async def test_modules_endpoint(app_and_client: Any) -> None:
    _, client, _ = app_and_client
    async with client:
        res = await client.get("/api/v1/modules")
        assert res.status_code == 200
        data = res.json()
        assert "modules" in data
        mod_names = [m["name"] for m in data["modules"]]
        assert "synthetic_order_lookup" in mod_names
        assert "synthetic_router" in mod_names
        assert "synthetic_ticket_creator" in mod_names
        assert "synthetic_fake_agent" in mod_names

        # Verify synthetic disclaimer is explicitly reported
        for mod in data["modules"]:
            assert "disclaimer" in mod
            assert "Synthetic" in mod["disclaimer"] or "Custom" in mod["disclaimer"]


@pytest.mark.asyncio
async def test_workflow_crud_and_revision_conflicts(app_and_client: Any) -> None:
    _, client, _ = app_and_client
    async with client:
        # Seeded template should already be present
        res = await client.get("/api/v1/workflows")
        assert res.status_code == 200
        workflows = res.json()["workflows"]
        assert len(workflows) >= 1
        seeded_id = workflows[0]["workflow_id"]

        # Retrieve specific workflow
        res_get = await client.get(f"/api/v1/workflows/{seeded_id}")
        assert res_get.status_code == 200
        wf = res_get.json()
        assert wf["revision"] == 1
        assert wf["name"] == "OrderSupportWorkflow"
        assert "spec_hash" in wf

        # Update with matching expected revision
        spec_copy = dict(wf["spec"])
        spec_copy["name"] = "RenamedSupportWorkflow"
        res_put = await client.put(
            f"/api/v1/workflows/{seeded_id}",
            json={"expected_revision": 1, "spec": spec_copy},
        )
        assert res_put.status_code == 200
        updated = res_put.json()
        assert updated["revision"] == 2
        assert updated["name"] == "RenamedSupportWorkflow"

        # Stale update with old expected_revision=1 must return 409 Conflict
        res_stale = await client.put(
            f"/api/v1/workflows/{seeded_id}",
            json={"expected_revision": 1, "spec": spec_copy},
        )
        assert res_stale.status_code == 409
        err = res_stale.json()["error"]
        assert err["code"] == "WORKFLOW_CONFLICT"
        assert "expected revision 1, found 2" in err["message"]


@pytest.mark.asyncio
async def test_untrusted_module_name_rejected(app_and_client: Any) -> None:
    _, client, _ = app_and_client
    async with client:
        malicious_spec = {
            "name": "MaliciousWorkflow",
            "entry_node": "eval_node",
            "nodes": {
                "eval_node": {
                    "id": "eval_node",
                    "handler": "os.system",  # Not in ModuleRegistry!
                    "transitions": {},
                    "default_transition": None,
                    "requires_approval": False,
                }
            },
            "terminal_nodes": ["eval_node"],
        }
        # Validation endpoint rejects
        val_res = await client.post(
            "/api/v1/workflows/validate",
            json={"spec": malicious_spec},
        )
        assert val_res.status_code == 400
        assert "unknown handler 'os.system'" in val_res.json()["error"]["message"]

        # Creation endpoint rejects
        create_res = await client.post(
            "/api/v1/workflows",
            json={"spec": malicious_spec},
        )
        assert create_res.status_code == 400
        assert "unknown handler 'os.system'" in create_res.json()["error"]["message"]


@pytest.mark.asyncio
async def test_run_pending_before_approval_and_resume(app_and_client: Any) -> None:
    _, client, _ = app_and_client
    async with client:
        # Start a run with the default order-support template
        res_start = await client.post(
            "/api/v1/runs",
            json={
                "workflow_id": "wf-order-support",
                "initial_state": {"order_id": "ORD-DELHI-501"},
            },
        )
        assert res_start.status_code == 201
        run = res_start.json()
        run_id = run["run_id"]
        assert run["status"] == "paused"
        assert run["current_node_id"] == "approval_gate"
        assert run["pending_approval"] is not None
        approval_id = run["pending_approval"]["approval_id"]

        # Crucial check: gated handler synthetic_ticket_creator has NOT run yet
        assert "ticket_id" not in run["current_state"]

        # Verify ordered events include APPROVAL_REQUIRED
        ev_types = [e["type"] for e in run["events"]]
        assert "RUN_STARTED" in ev_types
        assert "NODE_STARTED" in ev_types
        assert "NODE_COMPLETED" in ev_types
        assert "APPROVAL_REQUIRED" in ev_types

        # Explicitly approve the pending request
        res_approve = await client.post(
            f"/api/v1/runs/{run_id}/decide",
            json={
                "approval_id": approval_id,
                "decision": "approve",
                "state_update": {"approved_by": "सुपरवाइज़र प्रिया (Supervisor Priya)"},
            },
        )
        assert res_approve.status_code == 200
        resumed = res_approve.json()
        # B43: the gate is a function node, which reads the whole state, so the approver's
        # state_update needs a fresh approval: the run pauses again showing the edit, and
        # the gated handler has still not run. Approving that fresh request completes it.
        assert resumed["status"] == "paused"
        assert "ticket_id" not in resumed["current_state"]
        assert resumed["current_state"]["approved_by"] == "सुपरवाइज़र प्रिया (Supervisor Priya)"
        fresh_id = resumed["pending_approval"]["approval_id"]
        assert fresh_id != approval_id
        res_approve = await client.post(
            f"/api/v1/runs/{run_id}/decide",
            json={"approval_id": fresh_id, "decision": "approve"},
        )
        assert res_approve.status_code == 200
        resumed = res_approve.json()
        assert resumed["status"] == "completed"

        # Now the gated effect executed
        assert "ticket_id" in resumed["current_state"]
        assert resumed["current_state"]["approved_by"] == "सुपरवाइज़र प्रिया (Supervisor Priya)"
        assert "agent_response" in resumed["current_state"]
        # Indic Unicode survived intact
        assert "रिफंड स्वीकृत" in resumed["current_state"]["resolution"]


@pytest.mark.asyncio
async def test_rejection_executes_zero_gated_effects(app_and_client: Any) -> None:
    _, client, _ = app_and_client
    async with client:
        res_start = await client.post(
            "/api/v1/runs",
            json={
                "workflow_id": "wf-order-support",
                "initial_state": {"order_id": "ORD-DELHI-501"},
            },
        )
        run = res_start.json()
        run_id = run["run_id"]
        approval_id = run["pending_approval"]["approval_id"]

        # Reject the pending approval
        res_reject = await client.post(
            f"/api/v1/runs/{run_id}/decide",
            json={"approval_id": approval_id, "decision": "reject"},
        )
        assert res_reject.status_code == 200
        rejected = res_reject.json()
        assert rejected["status"] == "rejected"
        assert "Approval rejected" in rejected["error"]

        # Zero gated effects executed: ticket_id was never created!
        assert "ticket_id" not in rejected["current_state"]
        assert "agent_response" not in rejected["current_state"]


@pytest.mark.asyncio
async def test_duplicate_decision_rejected(app_and_client: Any) -> None:
    _, client, _ = app_and_client
    async with client:
        res_start = await client.post(
            "/api/v1/runs",
            json={
                "workflow_id": "wf-order-support",
                "initial_state": {"order_id": "ORD-DELHI-501"},
            },
        )
        run = res_start.json()
        run_id = run["run_id"]
        approval_id = run["pending_approval"]["approval_id"]

        # First approval succeeds
        res1 = await client.post(
            f"/api/v1/runs/{run_id}/decide",
            json={"approval_id": approval_id, "decision": "approve"},
        )
        assert res1.status_code == 200
        assert res1.json()["status"] == "completed"

        # Duplicate approval attempt on terminal run must be rejected with 409
        res2 = await client.post(
            f"/api/v1/runs/{run_id}/decide",
            json={"approval_id": approval_id, "decision": "approve"},
        )
        assert res2.status_code == 409
        err = res2.json()["error"]
        assert err["code"] in ("WORKFLOW_TERMINAL", "APPROVAL_ALREADY_CLAIMED")


@pytest.mark.asyncio
async def test_spec_edit_isolation(app_and_client: Any) -> None:
    """Verifies that editing a workflow definition does not alter past/in-flight runs."""
    _, client, _ = app_and_client
    async with client:
        # Start a run with workflow v1
        res_start = await client.post(
            "/api/v1/runs",
            json={
                "workflow_id": "wf-order-support",
                "initial_state": {"order_id": "ORD-DELHI-501"},
            },
        )
        run_id = res_start.json()["run_id"]
        approval_id = res_start.json()["pending_approval"]["approval_id"]

        # Now mutate the workflow definition: change transition or add node
        wf_res = await client.get("/api/v1/workflows/wf-order-support")
        spec = wf_res.json()["spec"]
        spec["name"] = "MutatedOrderSupportWorkflow"
        # Modify the gated node's default transition to a non-existent node
        spec["nodes"]["approval_gate"]["default_transition"] = "fake_mutated_node"
        spec["nodes"]["fake_mutated_node"] = {
            "id": "fake_mutated_node",
            "handler": "synthetic_fake_agent",
            "transitions": {},
            "default_transition": None,
            "requires_approval": False,
        }
        spec["terminal_nodes"].append("fake_mutated_node")

        await client.put(
            "/api/v1/workflows/wf-order-support",
            json={"expected_revision": 1, "spec": spec},
        )

        # Now resume the earlier run: it MUST use its bound snapshot spec
        # (which originally targeted agent_assist)
        res_resume = await client.post(
            f"/api/v1/runs/{run_id}/decide",
            json={"approval_id": approval_id, "decision": "approve"},
        )
        assert res_resume.status_code == 200
        resumed = res_resume.json()
        assert resumed["status"] == "completed"
        # The run completed at agent_assist (its original bound spec), not fake_mutated_node!
        assert resumed["spec"]["name"] == "OrderSupportWorkflow"


@pytest.mark.asyncio
async def test_persistence_across_app_recreation(temp_db_path: Path) -> None:
    """Verifies checkpoints and runs survive closing and recreating the application."""
    # App Instance 1
    app1 = create_app(db_path=temp_db_path)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app1), base_url="http://testserver"
    ) as client1:
        res1 = await client1.post(
            "/api/v1/runs",
            json={
                "workflow_id": "wf-order-support",
                "initial_state": {"order_id": "ORD-DELHI-501"},
            },
        )
        run_id = res1.json()["run_id"]
        approval_id = res1.json()["pending_approval"]["approval_id"]
        assert res1.json()["status"] == "paused"

    # Close App 1 store
    await app1.state.store.close()

    # App Instance 2 connected to same SQLite DB file
    app2 = create_app(db_path=temp_db_path)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app2), base_url="http://testserver"
    ) as client2:
        # Check run state is preserved
        get_res = await client2.get(f"/api/v1/runs/{run_id}")
        assert get_res.status_code == 200
        run_data = get_res.json()
        assert run_data["status"] == "paused"
        assert run_data["pending_approval"]["approval_id"] == approval_id

        # Resume run in App 2
        res2 = await client2.post(
            f"/api/v1/runs/{run_id}/decide",
            json={"approval_id": approval_id, "decision": "approve"},
        )
        assert res2.status_code == 200
        assert res2.json()["status"] == "completed"

    await app2.state.store.close()


@pytest.mark.asyncio
async def test_interrupted_run_recovery_on_startup(temp_db_path: Path) -> None:
    """Verifies runs left in 'running' state are safely marked 'interrupted' upon restart."""
    import sqlite3

    # Seed an orphaned running record directly in the DB
    app_pre = create_app(db_path=temp_db_path)
    await app_pre.state.store.close()

    conn = sqlite3.connect(str(temp_db_path))
    spec = get_default_workflow_spec()
    with conn:
        conn.execute(
            """
            INSERT INTO nexura_studio_runs (
                run_id, workflow_id, spec_hash, spec_json, status, current_node_id,
                input_state_json, current_state_json, step_count, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "run-orphaned-99",
                "wf-order-support",
                spec.canonical_hash(),
                spec.to_json(),
                "running",
                "lookup",
                "{}",
                "{}",
                1,
                1000.0,
                1000.0,
            ),
        )
    conn.close()

    # Start new app instance
    app_post = create_app(db_path=temp_db_path)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app_post), base_url="http://testserver"
    ) as client:
        res = await client.get("/api/v1/runs/run-orphaned-99")
        assert res.status_code == 200
        run = res.json()
        assert run["status"] == "interrupted"
        assert "interrupted by server restart" in run["error"]

        # Verify event was recorded
        ev_types = [e["type"] for e in run["events"]]
        assert "RUN_INTERRUPTED" in ev_types

    await app_post.state.store.close()


@pytest.mark.asyncio
async def test_static_ui_served(app_and_client: Any) -> None:
    _, client, _ = app_and_client
    async with client:
        res_root = await client.get("/")
        assert res_root.status_code == 200
        assert "text/html" in res_root.headers.get("content-type", "")
        assert "Nexura Studio" in res_root.text

        res_css = await client.get("/static/styles.css")
        assert res_css.status_code == 200
        assert "text/css" in res_css.headers.get("content-type", "")

        res_js = await client.get("/static/app.js")
        assert res_js.status_code == 200
        assert "javascript" in res_js.headers.get("content-type", "")
