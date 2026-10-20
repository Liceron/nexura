"""Tests for O07: Consistent SDK, API, and Studio Behavior.

Verifies:
1. Canonical supported workflow version contract and legacy mapping.
2. Equivalent reference workflow execution across Python, API, Studio, and exported Python.
3. Provider capabilities advertising streaming vs final-only models, plus SSE reconnect cursors.
4. Multi-tenant workspace scoping (foreign workspace cannot execute known module ID).
5. CAS/version conflicts and draft preservation under concurrent edits.
6. Local mock V2 trace export without network or V2 repository mutation.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from nexura_runtime.contracts import BaseModule, ExecutionContext, ModuleManifest, ModuleResult
from nexura_runtime.errors import (
    UnsupportedWorkflowVersionError,
    WorkflowValidationError,
)
from starlette.testclient import TestClient

from nexura_runtime import Agent, FakeModel, Runner
from nexura_runtime.integrations.nexura_v2 import NexuraV2TraceExporter, TraceMetadata
from nexura_runtime.modules.registry import VersionedModuleRegistry
from nexura_runtime.observability.tracer import NexuraTracer
from nexura_runtime.release.migration import WorkflowMigrator
from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.api.export import WorkflowExporter
from nexura_runtime.service.api.validation import validate_workflow_graph
from nexura_runtime.service.auth.errors import CrossWorkspaceAccessError
from nexura_runtime.service.workspaces.repository import WorkspaceRepository
from nexura_runtime.studio.builtin_modules import get_default_registry
from nexura_runtime.studio.store import StudioStore
from nexura_runtime.workflow import (
    HandlerOutput,
    MemoryWorkflowStore,
    ModuleRegistry,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
)

# ---------------------------------------------------------------------------
# Fixtures and Helpers
# ---------------------------------------------------------------------------


def make_reference_spec() -> WorkflowSpec:
    """Create canonical 3-node reference workflow specification."""
    return WorkflowSpec(
        name="OrderFulfillmentWorkflow",
        entry_node="init_order",
        spec_version=1,
        nodes={
            "init_order": WorkflowNode(
                id="init_order",
                handler="init_handler",
                transitions={},
                default_transition="approval_gate",
                requires_approval=False,
            ),
            "approval_gate": WorkflowNode(
                id="approval_gate",
                handler="approval_handler",
                transitions={},
                default_transition="finalize_order",
                requires_approval=True,
            ),
            "finalize_order": WorkflowNode(
                id="finalize_order",
                handler="finalize_handler",
                transitions={},
                default_transition=None,
                requires_approval=False,
            ),
        },
        terminal_nodes=["finalize_order"],
        metadata={"category": "order_fulfillment"},
    )


def register_reference_handlers(registry: ModuleRegistry) -> None:
    """Register reference node handlers in registry."""

    async def init_handler(state: dict[str, Any]) -> HandlerOutput:
        return HandlerOutput(
            state_update={
                "order_id": state.get("order_id", "ORD-101"),
                "amount": state.get("amount", 250),
                "status": "initiated",
            }
        )

    async def approval_handler(state: dict[str, Any]) -> HandlerOutput:
        return HandlerOutput(
            state_update={
                "approval_recorded": True,
                "status": "approved",
            }
        )

    async def finalize_handler(state: dict[str, Any]) -> HandlerOutput:
        return HandlerOutput(
            state_update={
                "finalized": True,
                "status": "completed",
            }
        )

    registry.register("init_handler", init_handler)
    registry.register("approval_handler", approval_handler)
    registry.register("finalize_handler", finalize_handler)


# ---------------------------------------------------------------------------
# Test 1: Canonical Supported Workflow & Version Contract
# ---------------------------------------------------------------------------


class TestCanonicalWorkflowVersionContract:
    """Validate canonical workflow format, version rejection, and legacy migrations."""

    def test_supported_spec_version_1_validates_and_executes(self) -> None:
        spec = make_reference_spec()
        assert spec.spec_version == 1
        spec.validate()
        payload = spec.to_dict()
        assert payload["spec_version"] == 1
        reloaded = WorkflowSpec.from_dict(payload)
        assert reloaded.spec_version == 1

    def test_unsupported_spec_version_rejected_with_actionable_error(self) -> None:
        data = make_reference_spec().to_dict()
        data["spec_version"] = 99

        with pytest.raises(UnsupportedWorkflowVersionError) as exc_info:
            WorkflowSpec.from_dict(data)

        assert "Unsupported workflow spec_version '99'" in str(exc_info.value)
        assert exc_info.value.version == 99
        assert 1 in exc_info.value.supported_versions

    def test_graph_validation_reports_unsupported_spec_version(self) -> None:
        registry = get_default_registry()
        data = make_reference_spec().to_dict()
        data["spec_version"] = 2

        report = validate_workflow_graph(data, registry=registry)
        assert not report.valid
        assert any(e.code == "UNSUPPORTED_SPEC_VERSION" for e in report.errors)

    def test_unknown_module_yields_actionable_error(self) -> None:
        registry = get_default_registry()
        spec = make_reference_spec().to_dict()
        spec["nodes"]["init_order"]["handler"] = "nonexistent_module_xyz"

        report = validate_workflow_graph(spec, registry=registry)
        assert not report.valid
        assert any(e.code == "UNKNOWN_HANDLER" for e in report.errors)

    def test_unavailable_module_yields_actionable_error(self) -> None:
        v_reg = VersionedModuleRegistry()

        class DummyModule(BaseModule):
            manifest = ModuleManifest(
                module_id="optional.torch_embedder",
                version="1.0.0",
                kind="tool",
                title="Torch Embedder",
                description="Requires torch",
                dependency_extra="torch",
            )

            async def invoke(self, ctx: ExecutionContext, inputs: Any, cfg: Any) -> ModuleResult:
                return ModuleResult(status="success")

        v_reg.register(DummyModule)
        # "torch" is not an extra declared by the SDK, so it must not resolve as available.
        entry = v_reg.get_entry("optional.torch_embedder", "1.0.0")
        assert entry.is_available is False

        reg = ModuleRegistry()
        reg._versioned_registry = v_reg

        spec = {
            "name": "TorchWorkflow",
            "entry_node": "n1",
            "spec_version": 1,
            "nodes": {
                "n1": {
                    "id": "n1",
                    "module_id": "optional.torch_embedder",
                    "module_version": "1.0.0",
                    "transitions": {},
                }
            },
        }

        report = validate_workflow_graph(spec, registry=reg)
        assert not report.valid
        unavail = next((e for e in report.errors if e.code == "MODULE_UNAVAILABLE"), None)
        assert unavail is not None
        assert "torch" in unavail.message

    def test_legacy_linear_steps_and_module_name_explicitly_migrated(self) -> None:
        migrator = WorkflowMigrator()
        legacy_data = {
            "name": "LegacyLinearWorkflow",
            "steps": [
                {"id": "step_a", "module_name": "fetch_data"},
                {"id": "step_b", "handler": "process_data", "requires_approval": True},
            ],
        }

        migrated = migrator.migrate_spec_dict(legacy_data)
        assert migrated.spec_version == 1
        assert migrated.entry_node == "step_a"
        assert "step_a" in migrated.nodes
        assert "step_b" in migrated.nodes
        assert migrated.nodes["step_a"].handler == "fetch_data"
        assert migrated.nodes["step_a"].default_transition == "step_b"
        assert migrated.nodes["step_b"].requires_approval is True
        assert migrated.terminal_nodes == ["step_b"]

    def test_legacy_adapter_registered_as_legacy_kind(self) -> None:
        v_reg = VersionedModuleRegistry()

        def my_legacy_func(state: dict[str, Any]) -> dict[str, Any]:
            return {"doubled": state.get("val", 0) * 2}

        v_reg.register_legacy("my_legacy_func", my_legacy_func)
        entry = v_reg.get_entry("legacy.my_legacy_func", "0.1.0")
        assert entry.manifest.kind == "legacy_handler"


# ---------------------------------------------------------------------------
# Test 2: Execution Equivalence Across Python, API, Studio, and Exported Runner
# ---------------------------------------------------------------------------


class TestExecutionEquivalenceAcrossSurfaces:
    """Validate identical state transitions, approvals, and steps across 4 surfaces."""

    @pytest.mark.asyncio
    async def test_reference_workflow_direct_python_execution(self) -> None:
        registry = ModuleRegistry()
        register_reference_handlers(registry)
        spec = make_reference_spec()
        store = MemoryWorkflowStore()
        engine = WorkflowEngine(spec=spec, registry=registry, store=store)

        # 1. Run to approval gate
        run_res = await engine.run(initial_state={"order_id": "ORD-777", "amount": 500})
        assert run_res.status == "paused"
        assert run_res.step_count == 1
        assert run_res.pending_approval is not None
        assert run_res.pending_approval.node_id == "approval_gate"
        assert run_res.state["status"] == "initiated"

        # 2. Resume with approval decision
        resume_res = await engine.resume(
            workflow_run_id=run_res.workflow_run_id,
            approval_id=run_res.pending_approval.approval_id,
            decision="approve",
            state_update={"manager_note": "Approved by Alice"},
        )
        # B43: approval_handler is a function node and reads the whole state, so the manager's
        # note needs a fresh approval of the edited state; approving that one completes the run.
        assert resume_res.status == "paused"
        assert resume_res.pending_approval is not None
        assert resume_res.pending_approval.input_state["manager_note"] == "Approved by Alice"
        assert "approval_recorded" not in resume_res.state
        resume_res = await engine.resume(
            workflow_run_id=run_res.workflow_run_id,
            approval_id=resume_res.pending_approval.approval_id,
            decision="approve",
        )
        assert resume_res.status == "completed"
        assert resume_res.step_count == 3
        assert resume_res.state["finalized"] is True
        assert resume_res.state["manager_note"] == "Approved by Alice"
        assert resume_res.state["status"] == "completed"

    @pytest.mark.asyncio
    async def test_reference_workflow_service_api_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "service.sqlite3"
            registry = ModuleRegistry()
            register_reference_handlers(registry)
            app = create_api_app(registry=registry, db_path=db_path, profile="local-owner")

            with TestClient(app) as client:
                spec_payload = make_reference_spec().to_dict()

                # 1. Create workflow
                wf_resp = client.post(
                    "/api/v1/workflows",
                    json={"name": "OrderFlow", "spec": spec_payload},
                    headers={"X-Workspace-ID": "default"},
                )
                assert wf_resp.status_code == 201
                wf_id = wf_resp.json()["workflow_id"]

                # 2. Start run
                run_resp = client.post(
                    "/api/v1/runs",
                    json={
                        "workflow_id": wf_id,
                        "initial_state": {"order_id": "ORD-777", "amount": 500},
                    },
                    headers={"X-Workspace-ID": "default"},
                )
                assert run_resp.status_code == 201
                run_data = run_resp.json()
                run_id = run_data["run_id"]
                assert run_data["status"] == "pending_approval"
                assert run_data["step_count"] == 1
                assert run_data["pending_approval"]["node_id"] == "approval_gate"
                app_id = run_data["pending_approval"]["approval_id"]

                # 3. Submit approval decision
                decide_resp = client.post(
                    f"/api/v1/runs/{run_id}/decide",
                    json={
                        "approval_id": app_id,
                        "decision": "approve",
                        "state_update": {"manager_note": "Approved by Alice"},
                    },
                    headers={"X-Workspace-ID": "default"},
                )
                assert decide_resp.status_code == 200
                final_data = decide_resp.json()
                # B43: approval_handler is a function node and reads the whole state, so the
                # manager's note needs a fresh approval of the edited state; approving that
                # one completes the run.
                assert final_data["status"] == "pending_approval"
                assert final_data["current_state"]["manager_note"] == "Approved by Alice"
                assert "approval_recorded" not in final_data["current_state"]
                fresh_id = final_data["pending_approval"]["approval_id"]
                assert fresh_id != app_id
                decide_resp = client.post(
                    f"/api/v1/runs/{run_id}/decide",
                    json={"approval_id": fresh_id, "decision": "approve"},
                    headers={"X-Workspace-ID": "default"},
                )
                assert decide_resp.status_code == 200
                final_data = decide_resp.json()
                assert final_data["status"] == "completed"
                assert final_data["step_count"] == 3
                assert final_data["current_state"]["finalized"] is True
                assert final_data["current_state"]["manager_note"] == "Approved by Alice"
                assert final_data["current_state"]["status"] == "completed"

    @pytest.mark.asyncio
    async def test_reference_workflow_studio_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "studio.sqlite3"
            store = StudioStore(db_path)
            registry = ModuleRegistry()
            register_reference_handlers(registry)
            spec = make_reference_spec()

            wf_rec = await store.create_workflow(spec)
            wf_id = wf_rec["workflow_id"]

            run_id = "run-studio-test-1"
            await store.record_run_start(
                run_id=run_id,
                workflow_id=wf_id,
                spec=spec,
                initial_state={"order_id": "ORD-777", "amount": 500},
            )

            engine = WorkflowEngine(
                spec=spec,
                registry=registry,
                store=store.checkpoint_store,
            )

            # Step to pause
            res_1 = await engine.run({"order_id": "ORD-777", "amount": 500}, workflow_run_id=run_id)
            assert res_1.status == "paused"
            assert res_1.step_count == 1
            assert res_1.pending_approval is not None

            # Resume
            res_2 = await engine.resume(
                workflow_run_id=run_id,
                approval_id=res_1.pending_approval.approval_id,
                decision="approve",
                state_update={"manager_note": "Approved by Alice"},
            )
            # B43: approval_handler is a function node and reads the whole state, so the manager's
            # note needs a fresh approval of the edited state; approving that one completes the run.
            assert res_2.status == "paused"
            assert res_2.pending_approval is not None
            assert "approval_recorded" not in res_2.state
            res_2 = await engine.resume(
                workflow_run_id=run_id,
                approval_id=res_2.pending_approval.approval_id,
                decision="approve",
            )
            assert res_2.status == "completed"
            assert res_2.step_count == 3
            assert res_2.state["finalized"] is True
            assert res_2.state["manager_note"] == "Approved by Alice"

            await store.close()

    def test_reference_workflow_exported_runner_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            export_dir = Path(tmp_dir) / "exported_bundle"
            spec = make_reference_spec()

            exporter = WorkflowExporter()
            registry = ModuleRegistry()
            register_reference_handlers(registry)
            bundle = exporter.export_bundle(spec, target_dir=export_dir, registry=registry)
            assert "run.py" in bundle.files
            assert "workflow.json" in bundle.files

            # Ship the custom handlers next to the bundle and name them explicitly.
            (export_dir / "order_handlers.py").write_text(
                textwrap.dedent(
                    """
                    from nexura_runtime.workflow import ModuleRegistry

                    registry = ModuleRegistry()

                    @registry.register_handler("init_handler")
                    async def init_handler(state):
                        return {
                            "order_id": state.get("order_id", "ORD-101"),
                            "amount": state.get("amount", 250),
                            "status": "initiated",
                        }

                    @registry.register_handler("approval_handler")
                    async def approval_handler(state):
                        return {"approval_recorded": True, "status": "approved"}

                    @registry.register_handler("finalize_handler")
                    async def finalize_handler(state):
                        return {"finalized": True, "status": "completed"}
                    """
                ),
                encoding="utf-8",
            )
            run_file = export_dir / "run.py"

            # Execute run.py in subprocess with --approve and --state-update
            state_json = json.dumps({"order_id": "ORD-777", "amount": 500})
            update_json = json.dumps({"manager_note": "Approved by Alice"})
            cmd = [
                sys.executable,
                str(run_file),
                "--handlers",
                "order_handlers:registry",
                state_json,
                "--approve",
                "--state-update",
                update_json,
            ]
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=str(export_dir),
                check=False,
            )
            assert res.returncode == 0, f"run.py failed: {res.stderr}\n{res.stdout}"
            # B43: the edit re-gates the function node; run.py approves the fresh request
            # because its inputs are exactly the operator's --state-update.
            assert "(edited inputs)" in res.stdout
            assert "Status: completed" in res.stdout
            assert "Steps: 3" in res.stdout
            assert '"finalized": true' in res.stdout
            assert '"manager_note": "Approved by Alice"' in res.stdout


# ---------------------------------------------------------------------------
# Test 3: Multi-Tenant Workspace Scoping Across Operations
# ---------------------------------------------------------------------------


class TestMultiTenantWorkspaceScoping:
    """Validate tenant boundaries: foreign workspace cannot execute or inspect foreign resources."""

    @pytest.mark.asyncio
    async def test_foreign_workspace_cannot_execute_known_module_id_even_if_listing_hides(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "service.sqlite3"
            ws_repo = WorkspaceRepository(db_path=db_path)
            ws_repo.create_workspace("ws_alpha", "Alpha Workspace", "user_alpha")
            ws_repo.create_workspace("ws_beta", "Beta Workspace", "user_beta")

            registry = ModuleRegistry()

            # Create a private module owned by ws_beta
            class BetaPrivateModule(BaseModule):
                manifest = ModuleManifest(
                    module_id="custom.beta_secret_processor",
                    version="1.0.0",
                    kind="tool",
                    title="Beta Secret Processor",
                    description="Secret calculations for Beta",
                )

                async def invoke(
                    self, ctx: ExecutionContext, inputs: Any, cfg: Any
                ) -> ModuleResult:
                    return ModuleResult(status="success", outputs={"result": 42})

            beta_mod = BetaPrivateModule()
            beta_mod._workspace_id = "ws_beta"  # type: ignore[attr-defined]
            registry.register("custom.beta_secret_processor", beta_mod)
            if registry.versioned_registry:
                registry.versioned_registry.register(beta_mod)

            ws_repo.record_resource_ownership(
                "integration_tool", "custom.beta_secret_processor", "ws_beta", "user_beta"
            )

            app = create_api_app(
                registry=registry,
                workspace_repo=ws_repo,
                db_path=db_path,
                profile="local-owner",
            )

            with TestClient(app) as client:
                # 1. Workspace Alpha lists modules: beta_secret_processor is HIDDEN
                list_resp = client.get(
                    "/api/v1/modules",
                    headers={"X-Workspace-ID": "ws_alpha"},
                )
                assert list_resp.status_code == 200
                alpha_mods = [m["module_id"] for m in list_resp.json()["items"]]
                assert "custom.beta_secret_processor" not in alpha_mods

                # 2. Alpha creating a workflow that references beta_secret_processor: REJECTED
                spec_with_beta_tool = {
                    "name": "AlphaExploitWorkflow",
                    "entry_node": "n1",
                    "spec_version": 1,
                    "nodes": {
                        "n1": {
                            "id": "n1",
                            "module_id": "custom.beta_secret_processor",
                            "transitions": {},
                        }
                    },
                }
                create_resp = client.post(
                    "/api/v1/workflows",
                    json={"name": "AlphaExploit", "spec": spec_with_beta_tool},
                    headers={"X-Workspace-ID": "ws_alpha"},
                )
                assert create_resp.status_code == 403
                assert "CROSS_WORKSPACE_ACCESS_DENIED" in create_resp.json()["detail"]["code"]

                # 3. Alpha executing an inline run that references beta_secret_processor: REJECTED
                run_resp = client.post(
                    "/api/v1/runs",
                    json={"spec": spec_with_beta_tool},
                    headers={"X-Workspace-ID": "ws_alpha"},
                )
                assert run_resp.status_code == 403
                assert "CROSS_WORKSPACE_ACCESS_DENIED" in run_resp.json()["detail"]["code"]

    @pytest.mark.asyncio
    async def test_direct_workflow_engine_enforces_module_workspace_isolation(self) -> None:
        registry = ModuleRegistry()

        class BetaPrivateModule(BaseModule):
            manifest = ModuleManifest(
                module_id="custom.beta_calc",
                version="1.0.0",
                kind="tool",
                title="Beta Calc",
                description="Beta private math",
            )

            async def invoke(self, ctx: ExecutionContext, inputs: Any, cfg: Any) -> ModuleResult:
                return ModuleResult(status="success")

        beta_mod = BetaPrivateModule()
        beta_mod._workspace_id = "ws_beta"  # type: ignore[attr-defined]
        registry.register("custom.beta_calc", beta_mod)

        spec = WorkflowSpec(
            name="AlphaEngineExploit",
            entry_node="n1",
            nodes={"n1": WorkflowNode(id="n1", handler="custom.beta_calc")},
            spec_version=1,
        )

        engine = WorkflowEngine(
            spec=spec,
            registry=registry,
            workspace_id="ws_alpha",  # Alpha caller
        )

        with pytest.raises(CrossWorkspaceAccessError) as exc_info:
            await engine.run({})

        assert "belongs to workspace 'ws_beta', not 'ws_alpha'" in str(exc_info.value)

    def test_cross_workspace_inspect_export_delete_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "service.sqlite3"
            ws_repo = WorkspaceRepository(db_path=db_path)
            ws_repo.create_workspace("ws_alpha", "Alpha", "user_alpha")
            ws_repo.create_workspace("ws_beta", "Beta", "user_beta")

            registry = ModuleRegistry()
            register_reference_handlers(registry)
            app = create_api_app(
                registry=registry,
                workspace_repo=ws_repo,
                db_path=db_path,
                profile="local-owner",
            )

            with TestClient(app) as client:
                # Create workflow in ws_beta
                wf_resp = client.post(
                    "/api/v1/workflows",
                    json={"name": "BetaWorkflow", "spec": make_reference_spec().to_dict()},
                    headers={"X-Workspace-ID": "ws_beta"},
                )
                assert wf_resp.status_code == 201
                beta_wf_id = wf_resp.json()["workflow_id"]

                # 1. ws_alpha tries to GET beta workflow
                get_resp = client.get(
                    f"/api/v1/workflows/{beta_wf_id}",
                    headers={"X-Workspace-ID": "ws_alpha"},
                )
                assert get_resp.status_code == 403  # local-owner mode; shared mode is 404

                # 2. ws_alpha tries to EXPORT beta workflow
                export_resp = client.post(
                    f"/api/v1/workflows/{beta_wf_id}/export",
                    headers={"X-Workspace-ID": "ws_alpha"},
                )
                assert export_resp.status_code == 403  # local-owner mode; shared mode is 404

                # 3. ws_alpha tries to DELETE beta workflow
                del_resp = client.delete(
                    f"/api/v1/workflows/{beta_wf_id}",
                    headers={"X-Workspace-ID": "ws_alpha"},
                )
                assert del_resp.status_code == 403  # local-owner mode; shared mode is 404


# ---------------------------------------------------------------------------
# Test 4: Provider Capabilities, Streaming, Disconnect/Reconnect & Cancellation
# ---------------------------------------------------------------------------


class TestStreamingDisconnectAndCancellation:
    """Validate provider capability advertisement, SSE reconnect cursor, and cancellation."""

    def test_provider_capabilities_advertising_streaming_and_final_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "service.sqlite3"
            app = create_api_app(db_path=db_path, profile="local-owner")

            with TestClient(app) as client:
                resp = client.get("/api/v1/capabilities")
                assert resp.status_code == 200
                data = resp.json()
                prov_caps = data["provider_capabilities"]
                assert "openai" in prov_caps
                assert prov_caps["openai"]["can_stream"] is True
                assert prov_caps["openai"]["final_only"] is False

                assert "fake_model_final_only" in prov_caps
                assert prov_caps["fake_model_final_only"]["can_stream"] is False
                assert prov_caps["fake_model_final_only"]["final_only"] is True

                # Values come from the adapters themselves, not a hand-written table.
                from nexura_runtime.providers.anthropic.provider import (
                    AnthropicNativeProvider,
                )

                anthropic = AnthropicNativeProvider().capabilities
                assert prov_caps["anthropic"]["can_embed"] is anthropic.can_embed
                assert prov_caps["anthropic"]["can_multimodal"] is anthropic.can_multimodal
                assert prov_caps["sarvam"]["can_tools"] is False

    def test_sse_ordered_event_identities_and_reconnect_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "service.sqlite3"
            registry = ModuleRegistry()
            register_reference_handlers(registry)
            app = create_api_app(registry=registry, db_path=db_path, profile="local-owner")

            with TestClient(app) as client:
                # Start run
                run_resp = client.post(
                    "/api/v1/runs",
                    json={"spec": make_reference_spec().to_dict()},
                    headers={"X-Workspace-ID": "default"},
                )
                run_id = run_resp.json()["run_id"]

                # 1. Fetch full stream (cursor=0)
                full_stream_resp = client.get(
                    f"/api/v1/runs/{run_id}/events?cursor=0",
                    headers={"X-Workspace-ID": "default"},
                )
                assert full_stream_resp.status_code == 200
                lines = [
                    line for line in full_stream_resp.text.split("\n") if line.startswith("id: ")
                ]
                event_ids = [int(line.replace("id: ", "").strip()) for line in lines]
                assert len(event_ids) >= 2
                # Check IDs are strictly increasing
                assert event_ids == sorted(event_ids)

                # 2. Reconnect with cursor: should skip earlier events
                reconnect_cursor = event_ids[0]
                reconnect_resp = client.get(
                    f"/api/v1/runs/{run_id}/events?cursor={reconnect_cursor}",
                    headers={"X-Workspace-ID": "default"},
                )
                assert reconnect_resp.status_code == 200
                reconnect_lines = [
                    line for line in reconnect_resp.text.split("\n") if line.startswith("id: ")
                ]
                reconnect_ids = [int(line.replace("id: ", "").strip()) for line in reconnect_lines]
                assert all(eid > reconnect_cursor for eid in reconnect_ids)

                # 3. Reconnect with Last-Event-ID header
                header_resp = client.get(
                    f"/api/v1/runs/{run_id}/events",
                    headers={"X-Workspace-ID": "default", "Last-Event-ID": str(reconnect_cursor)},
                )
                assert header_resp.status_code == 200
                header_lines = [
                    line for line in header_resp.text.split("\n") if line.startswith("id: ")
                ]
                header_ids = [int(line.replace("id: ", "").strip()) for line in header_lines]
                assert header_ids == reconnect_ids

    def test_cancellation_preserves_uncertainty_and_halts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "service.sqlite3"
            registry = ModuleRegistry()
            register_reference_handlers(registry)
            app = create_api_app(registry=registry, db_path=db_path, profile="local-owner")

            with TestClient(app) as client:
                # Start run (which pauses at approval gate)
                run_resp = client.post(
                    "/api/v1/runs",
                    json={"spec": make_reference_spec().to_dict()},
                    headers={"X-Workspace-ID": "default"},
                )
                run_id = run_resp.json()["run_id"]

                # Cancel active run
                cancel_resp = client.post(
                    f"/api/v1/runs/{run_id}/cancel",
                    json={"reason": "Operator requested stop"},
                    headers={"X-Workspace-ID": "default"},
                )
                assert cancel_resp.status_code == 200
                cancelled_data = cancel_resp.json()
                assert cancelled_data["status"] == "cancelled"
                assert "Operator requested stop" in cancelled_data["error"]

                # Subsequent resume fails because run is terminal
                decide_resp = client.post(
                    f"/api/v1/runs/{run_id}/decide",
                    json={"approval_id": "any_id", "decision": "approve"},
                    headers={"X-Workspace-ID": "default"},
                )
                assert decide_resp.status_code == 409


# ---------------------------------------------------------------------------
# Test 5: CAS/Revision Conflicts & Multi-Tab State Preservation
# ---------------------------------------------------------------------------


class TestCasConflictAndDraftPreservation:
    """Validate optimistic concurrency control (CAS) and draft recovery under multi-tab edits."""

    def test_cas_revision_conflict_preserves_authoritative_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "service.sqlite3"
            registry = ModuleRegistry()
            register_reference_handlers(registry)
            app = create_api_app(registry=registry, db_path=db_path, profile="local-owner")

            with TestClient(app) as client:
                spec = make_reference_spec()
                create_resp = client.post(
                    "/api/v1/workflows",
                    json={"name": "MultiTabFlow", "spec": spec.to_dict()},
                    headers={"X-Workspace-ID": "default"},
                )
                wf_id = create_resp.json()["workflow_id"]
                initial_rev = create_resp.json()["revision"]  # revision 1

                # Tab 1 performs valid update (expected_revision = 1)
                updated_spec_tab1 = make_reference_spec().to_dict()
                updated_spec_tab1["metadata"]["editor"] = "Tab1"
                tab1_resp = client.put(
                    f"/api/v1/workflows/{wf_id}",
                    json={"spec": updated_spec_tab1, "expected_revision": initial_rev},
                    headers={"X-Workspace-ID": "default"},
                )
                assert tab1_resp.status_code == 200
                rev2 = tab1_resp.json()["revision"]  # revision 2

                # Tab 2 attempts update with stale revision 1 (expected_revision = 1)
                updated_spec_tab2 = make_reference_spec().to_dict()
                updated_spec_tab2["metadata"]["editor"] = "Tab2"
                tab2_resp = client.put(
                    f"/api/v1/workflows/{wf_id}",
                    json={"spec": updated_spec_tab2, "expected_revision": initial_rev},
                    headers={"X-Workspace-ID": "default"},
                )
                # Must return 409 Conflict
                assert tab2_resp.status_code == 409
                detail = tab2_resp.json()["detail"]
                assert detail["code"] == "REVISION_CONFLICT"
                assert "Workflow conflict" in detail["message"]

                # Verify authoritative server state is intact (revision 2 from Tab 1)
                get_resp = client.get(
                    f"/api/v1/workflows/{wf_id}",
                    headers={"X-Workspace-ID": "default"},
                )
                assert get_resp.status_code == 200
                current_server_wf = get_resp.json()
                assert current_server_wf["revision"] == rev2
                assert current_server_wf["spec"]["metadata"]["editor"] == "Tab1"


# ---------------------------------------------------------------------------
# Test 6: Local Mock Nexura V2 Trace Contract
# ---------------------------------------------------------------------------


class TestNexuraV2TraceCompatibilityContract:
    """Validate V2 trace export contract with local mock transport."""

    def test_nexura_v2_mock_trace_contract_local_only(self) -> None:
        captured_envelopes: list[dict[str, Any]] = []

        def mock_v2_transport(req: httpx.Request) -> httpx.Response:
            assert req.url.path == "/api/v1/traces"
            assert req.headers.get("x-api-key") == "fake-v2-key"
            body = json.loads(req.content)
            # Ensure valid UUID in body
            UUID(body["id"])
            captured_envelopes.append(body)
            return httpx.Response(201, json={"id": body["id"]})

        tracer = NexuraTracer()
        agent = Agent(
            name="V2TraceCompatibleAgent",
            instructions="Verify span hierarchy and trace id mapping.",
            model=FakeModel(responses=["OK"]),
        )

        model_attrs = {"gen_ai.response.model": "fake-local"}
        with (
            tracer.span("WorkflowRootSpan"),
            tracer.span("ModelExecutionSpan", attributes=model_attrs),
        ):
            runner = Runner()
            res = runner.run(agent, "Test trace input")

        assert res.final_output == "OK"
        spans = tracer.get_spans()
        assert len(spans) >= 2

        with httpx.Client(transport=httpx.MockTransport(mock_v2_transport)) as client:
            exporter = NexuraV2TraceExporter(
                "https://v2.example.mock",
                "fake-v2-key",
                client=client,
            )
            receipt = exporter.export(spans, TraceMetadata(name="O07 Test Trace"))

        assert receipt.attempts == 1
        assert len(captured_envelopes) == 1
        exported_trace = captured_envelopes[0]
        assert str(receipt.trace_id) == exported_trace["id"]
        assert len(exported_trace["spans"]) >= 2


# ---------------------------------------------------------------------------
# Test 7: Chat delivery mode and usage honesty
# ---------------------------------------------------------------------------


class TestChatDeliveryAndUsageContract:
    """Chat replies declare final-only delivery and report only model-reported usage."""

    def test_chat_reply_is_labelled_final_only_with_reported_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            app = create_api_app(db_path=Path(tmp_dir) / "chat.sqlite3", profile="local-owner")
            with TestClient(app) as client:
                resp = client.post(
                    "/api/v1/sessions/o07-chat/chat",
                    json={"message": "calculate 2 + 3", "language": "en"},
                    headers={"X-Workspace-ID": "default"},
                )
                assert resp.status_code == 200
                body = resp.json()
                assert body["delivery"] == "final_only"
                usage = body["usage"]
                # Every key is present; values are model-reported, never defaulted.
                assert set(usage) == {
                    "prompt_tokens",
                    "completion_tokens",
                    "total_tokens",
                    "is_complete",
                }
                assert usage["is_complete"] is True
                assert usage["total_tokens"] == (
                    usage["prompt_tokens"] + usage["completion_tokens"]
                )


# ---------------------------------------------------------------------------
# Test 8: Streamed chat turns (ordered events, cancellation, scope)
# ---------------------------------------------------------------------------


def _parse_sse(text: str) -> list[tuple[int, str, dict[str, Any]]]:
    events = []
    for block in text.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((int(fields["id"]), fields["event"], json.loads(fields["data"])))
    return events


class TestStreamedChat:
    def test_stream_emits_ordered_events_then_the_persisted_response(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            app = create_api_app(db_path=Path(tmp_dir) / "chat.sqlite3", profile="local-owner")
            headers = {"X-Workspace-ID": "default"}
            with TestClient(app) as client:
                with client.stream(
                    "POST",
                    "/api/v1/sessions/o07-stream/chat/stream",
                    json={"message": "calculate 6 * 7"},
                    headers=headers,
                ) as resp:
                    assert resp.status_code == 200
                    assert resp.headers["content-type"].startswith("text/event-stream")
                    events = _parse_sse(resp.read().decode())

                ids = [seq for seq, _, _ in events]
                assert ids == list(range(1, len(ids) + 1))
                types = [data["type"] for _, kind, data in events if kind == "run_event"]
                for earlier, later in [
                    ("RUN_STARTED", "TOOL_STARTED"),
                    ("TOOL_STARTED", "TOOL_COMPLETED"),
                    ("TOOL_COMPLETED", "FINAL_OUTPUT"),
                    ("FINAL_OUTPUT", "RUN_COMPLETED"),
                ]:
                    assert types.index(earlier) < types.index(later)

                last_seq, last_kind, body = events[-1]
                assert last_kind == "chat_completed"
                assert body["delivery"] == "final_only"
                assert "42" in body["reply"]
                assert body["tool_executions"][0]["arguments"] is not None
                assert body["usage"]["is_complete"] is True

                stored = client.get("/api/v1/sessions/o07-stream", headers=headers).json()
                assert stored["revision"] == body["revision"]
                assert [m["role"] for m in stored["messages"]][-1] == "assistant"

    def test_non_streaming_tool_trace_includes_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            app = create_api_app(db_path=Path(tmp_dir) / "chat.sqlite3", profile="local-owner")
            with TestClient(app) as client:
                body = client.post(
                    "/api/v1/sessions/o07-args/chat",
                    json={"message": "calculate 2 + 3"},
                    headers={"X-Workspace-ID": "default"},
                ).json()
                assert body["tool_executions"][0]["arguments"] == {"expression": "2 + 3"}

    @pytest.mark.asyncio
    async def test_cancelled_stream_persists_nothing(self) -> None:
        from nexura_runtime.session import SQLiteSessionStore

        from nexura_runtime.service.api.sessions import ChatRequest, _chat_event_stream

        with tempfile.TemporaryDirectory() as tmp_dir:
            store = SQLiteSessionStore(Path(tmp_dir) / "sessions.sqlite3")
            before = await store.save("o07-cancel", [], expected_revision=0)
            stream = _chat_event_stream(store, "o07-cancel", ChatRequest(message="calculate 1 + 1"))
            first = await anext(stream)
            assert "RUN_STARTED" in first
            await stream.aclose()  # what a client disconnect does to the generator

            after = await store.get("o07-cancel")
            assert after is not None
            assert after.revision == before.revision
            assert after.messages == []
            await store.close()

    def test_stream_rejects_foreign_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            app = create_api_app(db_path=Path(tmp_dir) / "chat.sqlite3", profile="local-owner")
            with TestClient(app) as client:
                client.post(
                    "/api/v1/sessions/o07-owned/chat",
                    json={"message": "hello"},
                    headers={"X-Workspace-ID": "default"},
                )
                resp = client.post(
                    "/api/v1/sessions/o07-owned/chat/stream",
                    json={"message": "hello"},
                    headers={"X-Workspace-ID": "tenant-o07-foreign"},
                )
                assert resp.status_code == 403
                assert resp.json()["detail"]["code"] == "CROSS_WORKSPACE_ACCESS_DENIED"


# ---------------------------------------------------------------------------
# Test 9: Operator-supplied handler sources
# ---------------------------------------------------------------------------


class TestHandlerSources:
    def test_include_from_rejects_bad_targets_and_name_clashes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "o07_dup_handlers.py").write_text(
            "from nexura_runtime.workflow import ModuleRegistry\n"
            "registry = ModuleRegistry()\n"
            "registry.register('synthetic_router', lambda state: {})\n"
            "not_a_registry = 42\n",
            encoding="utf-8",
        )
        monkeypatch.syspath_prepend(str(tmp_path))
        registry = get_default_registry()

        for target, message in [
            ("no_colon_here", "package.module:attribute"),
            ("o07_missing_module:registry", "Cannot load handlers"),
            ("o07_dup_handlers:nope", "Cannot load handlers"),
            ("o07_dup_handlers:not_a_registry", "ModuleRegistry or a callable"),
            ("o07_dup_handlers:registry", "already registered"),
        ]:
            with pytest.raises(WorkflowValidationError, match=message):
                registry.include_from(target)

    def test_cli_run_and_export_accept_handler_sources(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from nexura_runtime.cli.main import main

        (tmp_path / "o07_cli_handlers.py").write_text(
            "def register(registry):\n"
            "    registry.register('init_handler', lambda s: {'status': 'initiated'})\n"
            "    registry.register('approval_handler', lambda s: {})\n"
            "    registry.register('finalize_handler', lambda s: {'finalized': True})\n",
            encoding="utf-8",
        )
        monkeypatch.syspath_prepend(str(tmp_path))
        spec_file = tmp_path / "spec.json"
        spec_file.write_text(json.dumps(make_reference_spec().to_dict()), encoding="utf-8")

        # Without the handlers, export refuses; with them it succeeds.
        assert main(["workflow", "export", str(spec_file), "--out", str(tmp_path / "b1")]) != 0
        assert (
            main(
                [
                    "workflow",
                    "export",
                    str(spec_file),
                    "--out",
                    str(tmp_path / "b2"),
                    "--handlers",
                    "o07_cli_handlers:register",
                ]
            )
            == 0
        )
        # The reference flow pauses at its approval gate, so `run` reports non-completion.
        main(["workflow", "run", str(spec_file), "--handlers", "o07_cli_handlers:register"])
        assert "Workflow Status: paused" in capsys.readouterr().out
