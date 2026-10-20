"""Composite release gate and API handoff verification for F00.

Validates:
1. Cross-resource ownership (credentials, artifacts, indexes, datasets, schedules) rejects
   forged references before any effects are executed.
2. Nested execution budgets, approval pause/resume, and immutable export retain authoritative state.
3. Service event replay semantics and canonical OpenAPI schema baseline generation.
"""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest
from _local_env import local_headers

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.workspaces.repository import WorkspaceRepository
from nexura_runtime.studio.store import StudioStore
from nexura_runtime.workflow import (
    ModuleRegistry,
    WorkflowNode,
    WorkflowSpec,
)


def _setup_service() -> tuple[Any, WorkspaceRepository, StudioStore, str, str, Path]:

    temp_dir = Path(tempfile.mkdtemp(prefix="nexura_f00_"))
    db_path = temp_dir / "store.sqlite3"
    store = StudioStore(db_path)
    ws_repo = WorkspaceRepository(db_path=db_path)

    # Register workspaces
    ws_repo.create_workspace("ws_alpha", "Alpha Workspace", owner_id="local-owner")
    ws_repo.create_workspace("ws_beta", "Beta Workspace", owner_id="local-owner")

    registry = ModuleRegistry()
    registry.register("noop", lambda state: {"status": "ok"})
    registry.register("effect_step", lambda state: {"dispatched_effect": True})

    app = create_api_app(
        registry=registry,
        store=store,
        workspace_repo=ws_repo,
        profile="local-owner",
    )

    workspace_alpha = "ws_alpha"
    workspace_beta = "ws_beta"

    return app, ws_repo, store, workspace_alpha, workspace_beta, temp_dir


@pytest.mark.asyncio
async def test_cross_resource_forged_references_rejected_before_effects() -> None:
    """Forged references to credentials, artifacts, indexes, datasets, and schedules

    owned by Workspace Beta are strictly rejected with 403 before Workspace Alpha
    can execute any side-effects.
    """
    app, ws_repo, store, workspace_alpha, workspace_beta, _ = _setup_service()

    # Beta owns genuine protected resources
    ws_repo.record_resource_ownership("credential", "cred_beta_secret", "ws_beta", "user_beta")
    ws_repo.record_resource_ownership("artifact", "art_beta_blob", "ws_beta", "user_beta")
    ws_repo.record_resource_ownership("index", "idx_beta_vectors", "ws_beta", "user_beta")
    ws_repo.record_resource_ownership("dataset", "ds_beta_eval", "ws_beta", "user_beta")
    ws_repo.record_resource_ownership("schedule", "sched_beta_cron", "ws_beta", "user_beta")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Valid workflow spec for Alpha
        spec = WorkflowSpec(
            name="AlphaExecutionFlow",
            entry_node="step1",
            nodes={"step1": WorkflowNode(id="step1", handler="effect_step")},
            terminal_nodes=["step1"],
        )

        wf_resp = await client.post(
            "/api/v1/workflows",
            headers=local_headers(workspace_alpha),
            json={"workflow_id": "wf_alpha_test", "spec": spec.to_dict()},
        )
        assert wf_resp.status_code == 201

        # 1. Reject forged credential reference in initial_state
        res_cred = await client.post(
            "/api/v1/runs",
            headers=local_headers(workspace_alpha),
            json={
                "workflow_id": "wf_alpha_test",
                "initial_state": {"credential_id": "cred_beta_secret"},
            },
        )
        assert res_cred.status_code == 403
        assert res_cred.json()["detail"]["code"] == "CROSS_WORKSPACE_ACCESS_DENIED"
        assert "cred_beta_secret" in res_cred.json()["detail"]["message"]

        # 2. Reject forged artifact URI in initial_state
        res_art_uri = await client.post(
            "/api/v1/runs",
            headers=local_headers(workspace_alpha),
            json={
                "workflow_id": "wf_alpha_test",
                "initial_state": {"source_doc": "artifact://ws_beta/art_beta_blob"},
            },
        )
        assert res_art_uri.status_code == 403
        assert res_art_uri.json()["detail"]["code"] == "CROSS_WORKSPACE_ACCESS_DENIED"

        # 3. Reject forged index / collection reference
        res_idx = await client.post(
            "/api/v1/runs",
            headers=local_headers(workspace_alpha),
            json={
                "workflow_id": "wf_alpha_test",
                "initial_state": {"index_id": "idx_beta_vectors"},
            },
        )
        assert res_idx.status_code == 403
        assert res_idx.json()["detail"]["code"] == "CROSS_WORKSPACE_ACCESS_DENIED"

        # 4. Reject forged evaluation dataset reference
        res_ds = await client.post(
            "/api/v1/runs",
            headers=local_headers(workspace_alpha),
            json={
                "workflow_id": "wf_alpha_test",
                "initial_state": {"dataset_id": "ds_beta_eval"},
            },
        )
        assert res_ds.status_code == 403
        assert res_ds.json()["detail"]["code"] == "CROSS_WORKSPACE_ACCESS_DENIED"

        # 5. Reject forged schedule reference
        res_sched = await client.post(
            "/api/v1/runs",
            headers=local_headers(workspace_alpha),
            json={
                "workflow_id": "wf_alpha_test",
                "initial_state": {"schedule_id": "sched_beta_cron"},
            },
        )
        assert res_sched.status_code == 403
        assert res_sched.json()["detail"]["code"] == "CROSS_WORKSPACE_ACCESS_DENIED"

        # 6. Reject workflow definition carrying foreign credential in spec metadata
        tainted_spec = WorkflowSpec(
            name="TaintedWorkflow",
            entry_node="step1",
            nodes={"step1": WorkflowNode(id="step1", handler="noop")},
            terminal_nodes=["step1"],
            metadata={"credential_ref": {"ref": "cred_beta_secret"}},
        )
        tainted_wf_res = await client.post(
            "/api/v1/workflows",
            headers=local_headers(workspace_alpha),
            json={"workflow_id": "wf_tainted", "spec": tainted_spec.to_dict()},
        )
        assert tainted_wf_res.status_code == 403
        assert tainted_wf_res.json()["detail"]["code"] == "CROSS_WORKSPACE_ACCESS_DENIED"

        # Verify zero runs were recorded for Alpha during failed attempts
        alpha_runs = await client.get(
            "/api/v1/runs",
            headers=local_headers(workspace_alpha),
        )
        assert alpha_runs.status_code == 200
        assert alpha_runs.json()["total"] == 0

    store._close_sync()


@pytest.mark.asyncio
async def test_nested_budgets_approval_resume_and_immutable_export() -> None:
    """Authoritative execution state survives approval pause/resume and produces clean export."""
    app, ws_repo, store, workspace_alpha, _, temp_dir = _setup_service()

    spec = WorkflowSpec(
        name="BudgetApprovalFlow",
        entry_node="step_pre",
        nodes={
            "step_pre": WorkflowNode(
                id="step_pre",
                handler="pre_handler",
                default_transition="gate",
            ),
            "gate": WorkflowNode(
                id="gate",
                handler="gate_handler",
                requires_approval=True,
                default_transition="step_post",
            ),
            "step_post": WorkflowNode(
                id="step_post",
                handler="post_handler",
            ),
        },
        terminal_nodes=["step_post"],
    )

    registry = app.state.registry
    registry.register("pre_handler", lambda s: {"tokens_used": 150, "budget_remaining": 850})
    registry.register("gate_handler", lambda s: {"gated": True})
    registry.register(
        "post_handler",
        lambda s: {
            "tokens_used": s.get("tokens_used", 0) + 200,
            "budget_remaining": s.get("budget_remaining", 1000) - 200,
            "completed": True,
        },
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Create workflow
        create_res = await client.post(
            "/api/v1/workflows",
            headers=local_headers(workspace_alpha),
            json={"workflow_id": "wf_budget_gate", "spec": spec.to_dict()},
        )
        assert create_res.status_code == 201

        # Start execution
        start_res = await client.post(
            "/api/v1/runs",
            headers=local_headers(workspace_alpha),
            json={
                "workflow_id": "wf_budget_gate",
                "initial_state": {"budget_limit": 1000},
            },
        )
        assert start_res.status_code == 201
        run_data = start_res.json()
        assert run_data["status"] == "pending_approval"
        run_id = run_data["run_id"]
        approval_id = run_data["pending_approval"]["approval_id"]

        # Checkpoint state is authoritative
        assert run_data["current_state"]["tokens_used"] == 150
        assert run_data["current_state"]["budget_remaining"] == 850

        # Approve and resume
        decide_res = await client.post(
            f"/api/v1/runs/{run_id}/decide",
            headers=local_headers(workspace_alpha),
            json={
                "approval_id": approval_id,
                "decision": "approve",
                "state_update": {"approved_by": "lead_supervisor"},
            },
        )
        assert decide_res.status_code == 200
        # B43: the gate is a function node, which reads the whole state, so a state_update
        # changes what it would run on: the approval does not carry over and the run asks
        # again with the updated state. Approving that fresh request completes the run.
        regated = decide_res.json()
        assert regated["status"] == "pending_approval"
        fresh_id = regated["pending_approval"]["approval_id"]
        assert fresh_id != approval_id
        assert regated["current_state"]["approved_by"] == "lead_supervisor"
        decide_res = await client.post(
            f"/api/v1/runs/{run_id}/decide",
            headers=local_headers(workspace_alpha),
            json={"approval_id": fresh_id, "decision": "approve"},
        )
        assert decide_res.status_code == 200
        final_run = decide_res.json()
        assert final_run["status"] == "completed"
        assert final_run["current_state"]["tokens_used"] == 350
        assert final_run["current_state"]["budget_remaining"] == 650
        assert final_run["current_state"]["approved_by"] == "lead_supervisor"

        # Export immutable bundle
        export_dir = temp_dir / "snapshot"
        export_res = await client.post(
            "/api/v1/workflows/wf_budget_gate/export",
            headers=local_headers(workspace_alpha),
            json={"target_dir": str(export_dir)},
        )
        assert export_res.status_code == 200
        bundle = export_res.json()
        assert bundle["workflow_id"] == "wf_budget_gate"
        assert len(bundle["spec_hash"]) == 64
        assert "workflow.json" in bundle["files"]
        # OSS local export returns the chosen directory. It remains a snapshot even
        # when the saved workflow changes after export.
        assert Path(bundle["bundle_path"]) == export_dir
        snapshot = {name: (export_dir / name).read_bytes() for name in bundle["files"]}
        exported_spec = WorkflowSpec.from_dict(json.loads(snapshot["workflow.json"]))
        assert exported_spec.canonical_hash() == bundle["spec_hash"]
        download = await client.get(
            "/api/v1/workflows/wf_budget_gate/export/download",
            headers=local_headers(workspace_alpha),
        )
        assert download.status_code == 200
        with zipfile.ZipFile(io.BytesIO(download.content)) as zf:
            assert {"workflow.json", "run.py"} <= set(zf.namelist())
            assert zf.read("workflow.json") == snapshot["workflow.json"]
        updated = await client.put(
            "/api/v1/workflows/wf_budget_gate",
            headers=local_headers(workspace_alpha),
            json={
                "spec": {**spec.to_dict(), "name": "Changed after export"},
                "expected_revision": 1,
            },
        )
        assert updated.status_code == 200, updated.text
        assert {name: (export_dir / name).read_bytes() for name in snapshot} == snapshot

    store._close_sync()


@pytest.mark.asyncio
async def test_composite_event_replay_and_openapi_baseline() -> None:
    """Verify finite SSE event replay snapshot and extract canonical OpenAPI contract hash."""
    app, ws_repo, store, workspace_alpha, _, _ = _setup_service()

    spec = WorkflowSpec(
        name="EventFlow",
        entry_node="step1",
        nodes={"step1": WorkflowNode(id="step1", handler="noop")},
        terminal_nodes=["step1"],
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Create and run
        await client.post(
            "/api/v1/workflows",
            headers=local_headers(workspace_alpha),
            json={"workflow_id": "wf_event_test", "spec": spec.to_dict()},
        )
        start_res = await client.post(
            "/api/v1/runs",
            headers=local_headers(workspace_alpha),
            json={"workflow_id": "wf_event_test", "initial_state": {}},
        )
        run_id = start_res.json()["run_id"]

        # Stream events via SSE
        events_res = await client.get(
            f"/api/v1/runs/{run_id}/events",
            headers=local_headers(workspace_alpha),
        )
        assert events_res.status_code == 200
        body = events_res.text
        lines = [line for line in body.split("\n") if line.startswith("event: ")]
        assert len(lines) >= 2  # At least RUN_STARTED and RUN_COMPLETED

        # OpenAPI schema hash
        # Local API docs are public; the app schema gives the canonical OpenAPI contract.
        assert (await client.get("/openapi.json")).status_code == 200
        assert (await client.get("/docs")).status_code == 200
        schema = app.openapi()
        canonical_json = json.dumps(schema, sort_keys=True, separators=(",", ":"))
        schema_hash = hashlib.sha256(canonical_json.encode()).hexdigest()
        assert len(schema_hash) == 64

    store._close_sync()
