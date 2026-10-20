"""Focused unit and acceptance tests for B31 Backend API, Clients, and Export."""

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

import httpx
import pytest
import uvicorn
from _local_env import local_headers

from nexura_runtime.service.api import (
    NexuraClient,
    WorkflowExporter,
    create_api_app,
)
from nexura_runtime.service.extensions import AuthProviderRequiredError
from nexura_runtime.service.workspaces.repository import WorkspaceRepository
from nexura_runtime.studio.store import StudioStore
from nexura_runtime.workflow import (
    ModuleRegistry,
    WorkflowNode,
    WorkflowSpec,
)


def _create_approval_workflow_spec() -> WorkflowSpec:
    """Create a test workflow with an approval gate."""
    return WorkflowSpec(
        name="OrderApprovalFlow",
        entry_node="validate_order",
        nodes={
            "validate_order": WorkflowNode(
                id="validate_order",
                handler="validate_order",
                default_transition="manager_approval",
            ),
            "manager_approval": WorkflowNode(
                id="manager_approval",
                handler="manager_approval",
                requires_approval=True,
                default_transition="fulfill_order",
            ),
            "fulfill_order": WorkflowNode(
                id="fulfill_order",
                handler="fulfill_order",
            ),
        },
        terminal_nodes=["fulfill_order"],
    )


def _create_test_registry() -> ModuleRegistry:
    registry = ModuleRegistry()

    def validate_order(state: dict[str, Any]) -> dict[str, Any]:
        return {"validated": True, "total": state.get("amount", 100)}

    def manager_approval(state: dict[str, Any]) -> dict[str, Any]:
        return {"approved": True}

    def fulfill_order(state: dict[str, Any]) -> dict[str, Any]:
        return {"fulfilled": True, "confirmation_id": "CONF-999"}

    registry.register("validate_order", validate_order)
    registry.register("manager_approval", manager_approval)
    registry.register("fulfill_order", fulfill_order)
    return registry


@pytest.fixture
def test_setup() -> Any:
    temp_dir = tempfile.mkdtemp(prefix="nexura_b31_test_")
    db_path = Path(temp_dir) / "test_store.sqlite3"
    store = StudioStore(db_path)
    registry = _create_test_registry()
    ws_repo = WorkspaceRepository()

    app = create_api_app(registry=registry, store=store, workspace_repo=ws_repo)

    yield {
        "app": app,
        "store": store,
        "registry": registry,
        "ws_repo": ws_repo,
        "temp_dir": Path(temp_dir),
    }

    store._close_sync()


@pytest.mark.asyncio
async def test_python_client_workflow_and_approval_path(test_setup: dict[str, Any]) -> None:
    """Python client executes complete workflow lifecycle through approval gate."""
    app = test_setup["app"]
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        client = NexuraClient(base_url="http://testserver", http_client=http_client)

        spec = _create_approval_workflow_spec()
        wf = await client.workflows.create(spec.to_dict(), workflow_id="wf_approval_test")
        assert wf["workflow_id"] == "wf_approval_test"
        assert wf["revision"] == 1

        # 1. Start run -> pauses at manager_approval
        run = await client.runs.start(
            workflow_id="wf_approval_test",
            initial_state={"amount": 250},
        )
        assert run["status"] == "pending_approval"
        assert run["pending_approval"] is not None
        approval_id = run["pending_approval"]["approval_id"]
        assert approval_id is not None
        assert run["current_node_id"] == "manager_approval"

        # 2. Submit approval decision -> resumes and completes
        resumed = await client.runs.decide(
            run_id=run["run_id"],
            approval_id=approval_id,
            decision="approve",
            state_update={"approval_note": "Approved by VP"},
        )
        # B43: the gate is a function node, which reads the whole state, so the note needs a
        # fresh approval of the edited state before the gated handler runs.
        assert resumed["status"] == "pending_approval"
        assert resumed["current_state"]["approval_note"] == "Approved by VP"
        assert "fulfilled" not in resumed["current_state"]
        fresh_id = resumed["pending_approval"]["approval_id"]
        assert fresh_id != approval_id
        resumed = await client.runs.decide(
            run_id=run["run_id"], approval_id=fresh_id, decision="approve"
        )
        assert resumed["status"] == "completed"
        assert resumed["current_state"]["fulfilled"] is True
        assert resumed["current_state"]["confirmation_id"] == "CONF-999"
        assert resumed["current_state"]["approval_note"] == "Approved by VP"


@pytest.mark.asyncio
async def test_reconnect_replays_events_without_duplication(test_setup: dict[str, Any]) -> None:
    """Reconnecting with replay cursor streams only unseen events without duplication."""
    app = test_setup["app"]
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        client = NexuraClient(base_url="http://testserver", http_client=http_client)

        spec = _create_approval_workflow_spec()
        await client.workflows.create(spec.to_dict(), workflow_id="wf_events_test")

        run = await client.runs.start(
            workflow_id="wf_events_test",
            initial_state={"amount": 150},
        )
        run_id = run["run_id"]

        # Stream all events from cursor 0
        all_events: list[dict[str, Any]] = []
        async for ev in client.runs.stream_events(run_id, cursor=0):
            all_events.append(ev)

        assert len(all_events) >= 3
        seq_numbers = [ev["seq"] for ev in all_events]
        assert seq_numbers == sorted(seq_numbers)

        # Reconnect with cursor = 2
        cutoff_cursor = 2
        replayed_events: list[dict[str, Any]] = []
        async for ev in client.runs.stream_events(run_id, cursor=cutoff_cursor):
            replayed_events.append(ev)

        # Must strictly receive events with seq > cutoff_cursor
        for ev in replayed_events:
            assert ev["seq"] > cutoff_cursor

        assert len(replayed_events) == len(all_events) - cutoff_cursor
        assert [ev["seq"] for ev in replayed_events] == seq_numbers[cutoff_cursor:]


@pytest.mark.asyncio
async def test_cancellation_semantics_explicit(test_setup: dict[str, Any]) -> None:
    """Active runs can be explicitly cancelled, and terminal runs refuse cancellation."""
    app = test_setup["app"]
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        client = NexuraClient(base_url="http://testserver", http_client=http_client)

        spec = _create_approval_workflow_spec()
        await client.workflows.create(spec.to_dict(), workflow_id="wf_cancel_test")

        # Run paused at approval gate
        run = await client.runs.start(workflow_id="wf_cancel_test", initial_state={"amount": 50})
        assert run["status"] == "pending_approval"
        run_id = run["run_id"]

        # Cancel the run
        cancelled_run = await client.runs.cancel(run_id, reason="Customer cancelled order")
        assert cancelled_run["status"] == "cancelled"
        assert "Cancelled: Customer cancelled order" in cancelled_run["error"]

        # Verify event recorded
        run_record = await client.runs.get(run_id)
        assert run_record["status"] == "cancelled"
        event_types = [ev["type"] for ev in run_record["events"]]
        assert "RUN_CANCELLED" in event_types

        # Attempting to cancel again raises 409 Conflict
        with pytest.raises(Exception) as exc_info:
            await client.runs.cancel(run_id)
        assert "RUN_ALREADY_TERMINAL" in str(exc_info.value)


@pytest.mark.asyncio
async def test_bundle_installs_and_runs_in_external_directory(test_setup: dict[str, Any]) -> None:
    """Workflow exporter produces a portable self-contained project that runs standalone."""
    temp_dir = test_setup["temp_dir"]
    export_target = temp_dir / "exported_bundle"

    # Define spec containing a secret placeholder and host path to verify scrubbing
    raw_spec = WorkflowSpec(
        name="StandaloneOrderExport",
        entry_node="lookup",
        nodes={
            "lookup": WorkflowNode(
                id="lookup",
                handler="synthetic_order_lookup",
            )
        },
        terminal_nodes=["lookup"],
        metadata={
            "api_key": "sk-secret-live-never-export-12345",
            "host_log_dir": "/home/ubuntu/projects/nexura-standalone-sdk/logs",
        },
    )

    exporter = WorkflowExporter()
    bundle = exporter.export_bundle(raw_spec, target_dir=export_target, workflow_id="order_bundle")
    assert bundle.workflow_id == "order_bundle"

    assert export_target.exists()
    assert (export_target / "workflow.json").exists()
    assert (export_target / "run.py").exists()
    assert (export_target / "requirements.txt").exists()
    assert (export_target / "pyproject.toml").exists()
    assert (export_target / "README.md").exists()

    # Verify scrubbing: raw secret must NOT be in workflow.json
    wf_json_text = (export_target / "workflow.json").read_text(encoding="utf-8")
    assert "sk-secret-live-never-export" not in wf_json_text
    wf_parsed = json.loads(wf_json_text)
    assert wf_parsed["metadata"]["api_key"] == {"ref": "API_KEY"}
    assert wf_parsed["metadata"]["host_log_dir"] == "logs"
    assert "/home/ubuntu/projects/nexura-standalone-sdk" not in wf_json_text

    # Execute standalone run.py in the external directory using subprocess
    res = subprocess.run(
        [sys.executable, str(export_target / "run.py"), '{"order_id": "ORD-DELHI-501"}'],
        cwd=str(export_target),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert res.returncode == 0
    assert "Status: completed" in res.stdout
    assert '"order_found": true' in res.stdout
    assert "Amit Verma" in res.stdout


@pytest.mark.asyncio
async def test_workspace_route_matrix_and_tenant_isolation(test_setup: dict[str, Any]) -> None:
    """Workspace route matrix enforces tenant isolation and permission checks."""
    app = test_setup["app"]
    ws_repo: WorkspaceRepository = test_setup["ws_repo"]

    # Create two workspaces
    ws_repo.create_workspace("ws_finance", "Finance Team", owner_id="local-owner")
    ws_repo.create_workspace("ws_hr", "HR Team", owner_id="local-owner")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        client = NexuraClient(base_url="http://testserver", http_client=http_client)

        # 1. List workspaces
        workspaces = await client.workspaces.list()
        assert workspaces["total"] >= 2
        ws_ids = [w["workspace_id"] for w in workspaces["items"]]
        assert "ws_finance" in ws_ids
        assert "ws_hr" in ws_ids

        # 2. Scoped client for ws_finance
        finance_client = client.with_workspace("ws_finance")
        spec = _create_approval_workflow_spec()
        created_wf = await finance_client.workflows.create(spec.to_dict(), workflow_id="wf_fin_01")
        assert created_wf["workflow_id"] == "wf_fin_01"

        # 3. Accessing via scoped matrix route
        resp = await http_client.get("/api/v1/workspaces/ws_finance/workflows")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert any(w["workflow_id"] == "wf_fin_01" for w in items)

        # 4. Cross-workspace negative check: user_hr accessing ws_finance is forbidden
        forbidden_resp = await http_client.get(
            "/api/v1/workspaces/ws_finance/workflows",
            headers={"X-Principal-ID": "user_hr"},
        )
        assert forbidden_resp.status_code == 200
        assert any(w["workflow_id"] == "wf_fin_01" for w in forbidden_resp.json()["items"])
        foreign = await http_client.get(
            "/api/v1/workflows/wf_fin_01", headers={"X-Workspace-ID": "ws_hr"}
        )
        assert foreign.status_code == 403
        assert foreign.json()["detail"]["code"] == "CROSS_WORKSPACE_ACCESS_DENIED"
        assert "'ws_finance'" in foreign.json()["detail"]["message"]
        assert "spec" not in foreign.json()
        scoped = await http_client.get("/api/v1/workspaces/ws_hr/workflows")
        assert scoped.status_code == 200
        assert scoped.json()["items"] == []


@pytest.mark.asyncio
async def test_pagination_and_cas_revision_conflict(test_setup: dict[str, Any]) -> None:
    """Pagination parameters and optimistic concurrency control are strictly enforced."""
    app = test_setup["app"]

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        client = NexuraClient(base_url="http://testserver", http_client=http_client)

        spec = _create_approval_workflow_spec()
        wf = await client.workflows.create(spec.to_dict(), workflow_id="wf_cas_test")
        assert wf["revision"] == 1

        # CAS update with matching expected revision succeeds
        updated = await client.workflows.update(
            workflow_id="wf_cas_test",
            spec=spec.to_dict(),
            expected_revision=1,
        )
        assert updated["revision"] == 2

        # CAS update with outdated expected revision (1 instead of 2) fails with 409 Conflict
        with pytest.raises(Exception) as exc_info:
            await client.workflows.update(
                workflow_id="wf_cas_test",
                spec=spec.to_dict(),
                expected_revision=1,
            )
        assert "REVISION_CONFLICT" in str(exc_info.value)


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_typescript_client_interop(test_setup: dict[str, Any]) -> None:
    """TypeScript client executes the workflow approval flow identically via Node."""
    app = test_setup["app"]
    port = _find_free_port()

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait for server ready
    server_ready = False
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                server_ready = True
                break
        except OSError:
            time.sleep(0.05)

    assert server_ready, "Ephemeral FastAPI server failed to start"

    try:
        # Run Node TypeScript test script
        build = subprocess.run(
            ["npm", "run", "build"],
            cwd="clients/typescript",
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert build.returncode == 0, build.stderr
        ts_runner = Path("clients/typescript/test_runner.ts").resolve()
        res = subprocess.run(
            ["node", "--experimental-strip-types", str(ts_runner), f"http://127.0.0.1:{port}"],
            cwd=str(Path(__file__).resolve().parents[3]),
            capture_output=True,
            text=True,
            timeout=15,
        )
        err_msg = f"TypeScript client failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
        assert res.returncode == 0, err_msg
        assert "SUCCESS: TypeScript client verified end-to-end." in res.stdout
    finally:
        server.should_exit = True
        thread.join(timeout=2.0)


@pytest.mark.asyncio
async def test_two_workspaces_isolation_matrix() -> None:
    """Distinct workspaces cannot access each other's workflows, runs, events, or exports."""

    temp_dir = tempfile.mkdtemp(prefix="nexura_b31_iso_")
    db_path = Path(temp_dir) / "store.sqlite3"
    store = StudioStore(db_path)
    registry = _create_test_registry()
    ws_repo = WorkspaceRepository(db_path=db_path)

    # Setup workspaces and members
    ws_repo.create_workspace("ws_alice", "Alice Workspace", owner_id="local-owner")
    ws_repo.create_workspace("ws_bob", "Bob Workspace", owner_id="local-owner")

    app = create_api_app(
        registry=registry,
        store=store,
        workspace_repo=ws_repo,
        profile="local-owner",
    )

    workspace_alice = "ws_alice"
    workspace_bob = "ws_bob"

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 1. Alice creates workflow and starts run
        spec = _create_approval_workflow_spec()
        wf_res = await client.post(
            "/api/v1/workflows",
            headers=local_headers(workspace_alice),
            json={"workflow_id": "wf_alice_1", "spec": spec.to_dict()},
        )
        assert wf_res.status_code == 201

        run_res = await client.post(
            "/api/v1/runs",
            headers=local_headers(workspace_alice),
            json={"workflow_id": "wf_alice_1", "initial_state": {"amount": 200}},
        )
        assert run_res.status_code == 201
        run_alice_id = run_res.json()["run_id"]
        approval_id = run_res.json()["pending_approval"]["approval_id"]

        # 2. Bob cannot GET Alice's workflow
        get_wf = await client.get(
            "/api/v1/workflows/wf_alice_1",
            headers=local_headers(workspace_bob),
        )
        assert get_wf.status_code == 403
        assert get_wf.json()["detail"]["code"] == "CROSS_WORKSPACE_ACCESS_DENIED"
        assert "'ws_alice'" in get_wf.json()["detail"]["message"]
        assert "spec" not in get_wf.json()

        # 3. Bob cannot UPDATE Alice's workflow
        put_wf = await client.put(
            "/api/v1/workflows/wf_alice_1",
            headers=local_headers(workspace_bob),
            json={"spec": spec.to_dict(), "expected_revision": 1},
        )
        assert put_wf.status_code == 403
        assert put_wf.json()["detail"]["code"] == "CROSS_WORKSPACE_ACCESS_DENIED"
        unchanged = await client.get(
            "/api/v1/workflows/wf_alice_1", headers=local_headers(workspace_alice)
        )
        assert unchanged.json()["revision"] == 1

        # 4. Bob listing workflows does NOT see Alice's workflow
        list_wf = await client.get(
            "/api/v1/workflows",
            headers=local_headers(workspace_bob),
        )
        assert list_wf.status_code == 200
        assert not any(w["workflow_id"] == "wf_alice_1" for w in list_wf.json()["items"])

        # 5. Bob cannot GET Alice's run
        get_run = await client.get(
            f"/api/v1/runs/{run_alice_id}",
            headers=local_headers(workspace_bob),
        )
        assert get_run.status_code == 403
        assert get_run.json()["detail"]["code"] == "CROSS_WORKSPACE_ACCESS_DENIED"
        assert "'ws_alice'" in get_run.json()["detail"]["message"]
        assert "state" not in get_run.json()

        # 6. Bob listing runs does NOT see Alice's run
        list_runs = await client.get(
            "/api/v1/runs",
            headers=local_headers(workspace_bob),
        )
        assert list_runs.status_code == 200
        assert not any(r["run_id"] == run_alice_id for r in list_runs.json()["items"])

        # 7. Bob cannot start a run referencing Alice's workflow
        forged_run = await client.post(
            "/api/v1/runs",
            headers=local_headers(workspace_bob),
            json={"workflow_id": "wf_alice_1", "initial_state": {}},
        )
        assert forged_run.status_code == 404
        assert forged_run.json()["detail"]["code"] == "WORKFLOW_NOT_FOUND"

        # 8. Bob cannot stream Alice's run events
        stream_resp = await client.get(
            f"/api/v1/runs/{run_alice_id}/events",
            headers=local_headers(workspace_bob),
        )
        assert stream_resp.status_code == 403
        assert stream_resp.json()["detail"]["code"] == "CROSS_WORKSPACE_ACCESS_DENIED"

        # 9. Bob cannot decide / approve Alice's run
        decide_resp = await client.post(
            f"/api/v1/runs/{run_alice_id}/decide",
            headers=local_headers(workspace_bob),
            json={"approval_id": approval_id, "decision": "approve"},
        )
        assert decide_resp.status_code == 403
        assert decide_resp.json()["detail"]["code"] == "CROSS_WORKSPACE_ACCESS_DENIED"
        still_waiting = await client.get(
            f"/api/v1/runs/{run_alice_id}", headers=local_headers(workspace_alice)
        )
        assert still_waiting.json()["status"] == "pending_approval"

        # 10. Bob cannot export Alice's workflow
        export_resp = await client.post(
            "/api/v1/workflows/wf_alice_1/export",
            headers=local_headers(workspace_bob),
            json={},
        )
        assert export_resp.status_code == 403
        assert export_resp.json()["detail"]["code"] == "CROSS_WORKSPACE_ACCESS_DENIED"
        assert "files" not in export_resp.json()

    store._close_sync()


@pytest.mark.asyncio
async def test_app_recreation_preserves_ownership_records() -> None:
    """App recreation preserves durable ownership mappings and tenant boundaries."""

    temp_dir = tempfile.mkdtemp(prefix="nexura_b31_recreate_")
    db_path = Path(temp_dir) / "persist.sqlite3"
    registry = _create_test_registry()

    workspace_alice = "ws_alpha"
    workspace_bob = "ws_beta"

    # 1. App 1: Create workspaces and resources
    app1 = create_api_app(
        registry=registry,
        db_path=db_path,
        profile="local-owner",
    )
    ws_repo1 = app1.state.workspace_repo
    ws_repo1.create_workspace("ws_alpha", "Alpha Workspace", owner_id="local-owner")
    ws_repo1.create_workspace("ws_beta", "Beta Workspace", owner_id="local-owner")

    transport1 = httpx.ASGITransport(app=app1)
    async with httpx.AsyncClient(transport=transport1, base_url="http://testserver") as client1:
        spec = _create_approval_workflow_spec()
        wf_res = await client1.post(
            "/api/v1/workflows",
            headers=local_headers(workspace_alice),
            json={"workflow_id": "wf_alpha_persist", "spec": spec.to_dict()},
        )
        assert wf_res.status_code == 201

        run_res = await client1.post(
            "/api/v1/runs",
            headers=local_headers(workspace_alice),
            json={"workflow_id": "wf_alpha_persist", "initial_state": {"amount": 500}},
        )
        assert run_res.status_code == 201
        run_id = run_res.json()["run_id"]

    await app1.state.store.close()

    # 2. App 2: Recreate app with same db_path
    app2 = create_api_app(
        registry=registry,
        db_path=db_path,
        profile="local-owner",
    )

    transport2 = httpx.ASGITransport(app=app2)
    async with httpx.AsyncClient(transport=transport2, base_url="http://testserver") as client2:
        # Alice can still access in App 2
        alice_wf = await client2.get(
            "/api/v1/workflows/wf_alpha_persist",
            headers=local_headers(workspace_alice),
        )
        assert alice_wf.status_code == 200

        alice_run = await client2.get(
            f"/api/v1/runs/{run_id}",
            headers=local_headers(workspace_alice),
        )
        assert alice_run.status_code == 200

        # Bob is denied access in App 2
        bob_wf = await client2.get(
            "/api/v1/workflows/wf_alpha_persist",
            headers=local_headers(workspace_bob),
        )
        assert bob_wf.status_code == 403
        assert bob_wf.json()["detail"]["code"] == "CROSS_WORKSPACE_ACCESS_DENIED"
        assert "'ws_alpha'" in bob_wf.json()["detail"]["message"]

        bob_run = await client2.get(
            f"/api/v1/runs/{run_id}",
            headers=local_headers(workspace_bob),
        )
        assert bob_run.status_code == 403
        assert bob_run.json()["detail"]["code"] == "CROSS_WORKSPACE_ACCESS_DENIED"
        assert "'ws_alpha'" in bob_run.json()["detail"]["message"]

    await app2.state.store.close()


@pytest.mark.asyncio
async def test_async_job_lifecycle_shutdown_truthful(test_setup: dict[str, Any]) -> None:
    """App shutdown reaps background tasks and marks interrupted runs truthfully."""
    app = test_setup["app"]
    store: StudioStore = test_setup["store"]
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Create long running workflow node
        long_registry = ModuleRegistry()

        async def slow_handler(state: dict[str, Any]) -> dict[str, Any]:
            await asyncio.sleep(10.0)
            return {"done": True}

        long_registry.register("slow_node", slow_handler)
        app.state.registry = long_registry

        spec = WorkflowSpec(
            name="SlowWorkflow",
            entry_node="slow_node",
            nodes={"slow_node": WorkflowNode(id="slow_node", handler="slow_node")},
            terminal_nodes=["slow_node"],
        )

        wf = await client.post(
            "/api/v1/workflows", json={"workflow_id": "wf_slow", "spec": spec.to_dict()}
        )
        assert wf.status_code == 201

        # Start async run in background
        run_res = await client.post(
            "/api/v1/runs",
            json={"workflow_id": "wf_slow", "async_execution": True, "initial_state": {}},
        )
        assert run_res.status_code == 201
        run_id = run_res.json()["run_id"]

        assert run_id in app.state.active_run_tasks
        assert not app.state.active_run_tasks[run_id].done()

    # Trigger app lifespan shutdown
    async with app.router.lifespan_context(app):
        pass  # Entering and exiting executes shutdown cleanup

    # Verify task was reaped
    assert run_id not in app.state.active_run_tasks

    # Verify run status is truthfully marked interrupted in store
    saved_run = await store.get_run(run_id)
    assert saved_run is not None
    assert saved_run["status"] == "interrupted"
    assert "interrupted by server shutdown" in saved_run["error"].lower()
    event_types = [ev["type"] for ev in saved_run["events"]]
    assert "RUN_INTERRUPTED" in event_types


def test_shared_mode_without_provider_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(AuthProviderRequiredError, match="enterprise"):
        create_api_app(db_path=tmp_path / "shared.db", profile="authenticated-shared-service")
