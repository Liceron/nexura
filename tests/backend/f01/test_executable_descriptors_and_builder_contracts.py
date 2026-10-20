"""Tests for F01: Executable descriptors and builder contracts.

Verifies:
1. Public-interface evidence: Module descriptor discovery, typed graph validation,
   persisted execution of versioned BaseModule with ExecutionContext, config/input/output
   mappings, and downloadable bundle export.
2. Negative Case 1: Unknown, unregistered, and version-incompatible modules rejected;
   descriptor listings expose no hidden callables or secrets.
3. Negative Case 2: Schema-driven config validation, input/output port mappings, and
   route transitions return node- and field-specific structured errors.
4. Negative Case 3: Multiple instances of a module maintain distinct node IDs,
   distinct configurations, and separate lifecycle event emissions; mandatory approvals
   cannot be bypassed or disabled.
5. Presentation metadata separation: Canvas coordinates and editor viewports are
   persisted across reload/export without mutating canonical executable digests.
6. Capabilities discovery and persistent SSE live event stream contract.
7. Python NexuraClient compatibility.
"""

from __future__ import annotations

import io
import zipfile
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from nexura_runtime.contracts.base import BaseModule
from nexura_runtime.contracts.context import ExecutionContext
from nexura_runtime.contracts.manifest import ModuleManifest
from nexura_runtime.contracts.result import ModuleResult

from nexura_runtime import __version__ as CURRENT_SDK_VERSION
from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.api.client import NexuraClient
from nexura_runtime.studio.builtin_modules import get_default_registry
from nexura_runtime.studio.store import StudioStore
from nexura_runtime.workflow import WorkflowNode, WorkflowSpec


@pytest.fixture
async def app_fixture(tmp_path: Any) -> Any:
    """Create disposable local-owner service app with isolated database."""
    db_file = tmp_path / "test_f01.sqlite3"
    store = StudioStore(db_file)
    registry = get_default_registry()

    app = create_api_app(
        registry=registry,
        store=store,
        db_path=db_file,
        profile="local-owner",
        approval_required_handlers=frozenset({"synthetic_ticket_creator"}),
    )
    return app, store, db_file


@pytest.mark.asyncio
async def test_public_interface_descriptor_validation_and_module_execution(
    app_fixture: Any,
) -> None:
    """Public-interface evidence: Fetch descriptor, validate, save, execute module via SDK & API."""
    app, store, _ = app_fixture

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. Inspect real registered module descriptor via HTTP
        desc_res = await client.get("/api/v1/modules/nexura.prompt.template")
        assert desc_res.status_code == 200
        desc = desc_res.json()
        assert desc["module_id"] == "nexura.prompt.template"
        assert desc["version"] == "1.0.0"
        assert desc["kind"] == "data_transform"
        assert desc["effect_policy"] == "read"
        assert desc["is_available"] is True
        assert "template" in desc["input_schema"]["properties"]
        assert "default_variables" in desc["config_schema"]["properties"]

        # 2. Build graph with real config and input/output mappings
        spec_dict = {
            "name": "BilingualPromptWorkflow",
            "entry_node": "render_greeting",
            "spec_version": 1,
            "nodes": {
                "render_greeting": {
                    "id": "render_greeting",
                    "handler": "nexura.prompt.template",
                    "module_id": "nexura.prompt.template",
                    "module_version": "1.0.0",
                    "config": {
                        "default_variables": {
                            "organization": "Nexura AgentKit India",
                            "currency": "INR",
                        },
                        "max_output_length": 5000,
                    },
                    "input_mapping": {
                        "template": "greeting_template",
                        "variables": "user_payload",
                    },
                    "output_mapping": {
                        "rendered": "final_output",
                    },
                    "presentation": {
                        "position": {"x": 120, "y": 240},
                        "color": "#3b82f6",
                    },
                    "transitions": {},
                    "default_transition": None,
                    "requires_approval": False,
                }
            },
            "presentation": {
                "viewport": {"x": 0, "y": 0, "zoom": 1.25},
            },
        }

        # 3. Server-side authoritative validation
        val_res = await client.post("/api/v1/workflows/validate", json={"spec": spec_dict})
        assert val_res.status_code == 200
        val_data = val_res.json()
        assert val_data["valid"] is True
        assert val_data["errors"] == []
        assert val_data["node_count"] == 1
        assert val_data["estimated_effect_policy"] == "read"
        assert val_data["canonical_hash"] is not None
        canonical_hash = val_data["canonical_hash"]

        # 4. Save workflow via API
        create_res = await client.post(
            "/api/v1/workflows",
            json={"workflow_id": "wf-prompt-india", "spec": spec_dict},
        )
        assert create_res.status_code == 201
        created = create_res.json()
        assert created["workflow_id"] == "wf-prompt-india"
        assert created["spec_hash"] == canonical_hash
        assert created["spec"]["presentation"]["viewport"]["zoom"] == 1.25
        assert created["spec"]["nodes"]["render_greeting"]["presentation"]["position"]["x"] == 120

        # 5. Execute run via API with multilingual payload
        run_res = await client.post(
            "/api/v1/runs",
            json={
                "workflow_id": "wf-prompt-india",
                "initial_state": {
                    "greeting_template": (
                        "नमस्ते {{ customer_name }}! आपके ऑर्डर का मूल्य "
                        "{{ currency }} {{ order_amount }} है। ({{ organization }})"
                    ),
                    "user_payload": {
                        "customer_name": "विक्रम साराभाई (Vikram Sarabhai)",
                        "order_amount": 7500,
                    },
                },
            },
        )
        assert run_res.status_code == 201
        run_data = run_res.json()
        assert run_data["status"] == "completed"
        final_state = run_data["current_state"]
        assert "final_output" in final_state
        assert "नमस्ते विक्रम साराभाई (Vikram Sarabhai)!" in final_state["final_output"]
        assert "INR 7500" in final_state["final_output"]
        assert "Nexura AgentKit India" in final_state["final_output"]

        # Verify module lifecycle events were recorded
        events_res = await client.get(f"/api/v1/runs/{run_data['run_id']}/events")
        assert events_res.status_code == 200
        events_text = events_res.text
        assert "MODULE_INVOKE_STARTED" in events_text
        assert "MODULE_INVOKE_COMPLETED" in events_text
        assert "render_greeting" in events_text

        # 6. Verify downloadable export endpoint
        dl_res = await client.get("/api/v1/workflows/wf-prompt-india/export/download")
        assert dl_res.status_code == 200
        assert dl_res.headers["content-type"] == "application/zip"
        assert (
            'attachment; filename="workflow_wf-prompt-india.zip"'
            in dl_res.headers["content-disposition"]
        )
        zip_bytes = dl_res.content
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            namelist = zf.namelist()
            assert "workflow.json" in namelist
            assert "run.py" in namelist


@pytest.mark.asyncio
async def test_negative_cases_unknown_incompatible_and_secret_redaction(app_fixture: Any) -> None:
    """Negative Case 1: Unknown/incompatible modules cannot run; descriptor listing redaction."""
    app, _, _ = app_fixture

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # A. Unknown module cannot validate or run
        bad_spec = {
            "name": "UnknownModuleGraph",
            "entry_node": "bad_node",
            "nodes": {
                "bad_node": {
                    "id": "bad_node",
                    "handler": "com.fraud.nonexistent.module",
                    "transitions": {},
                }
            },
        }
        val_res = await client.post("/api/v1/workflows/validate", json={"spec": bad_spec})
        assert val_res.status_code == 200
        val_data = val_res.json()
        assert val_data["valid"] is False
        assert len(val_data["errors"]) >= 1
        err = val_data["errors"][0]
        assert err["node_id"] == "bad_node"
        assert err["field"] == "handler"
        assert err["code"] == "UNKNOWN_HANDLER"

        # Attempt to create workflow with unknown handler fails with 422
        create_res = await client.post("/api/v1/workflows", json={"spec": bad_spec})
        assert create_res.status_code == 422
        assert create_res.json()["detail"]["code"] == "VALIDATION_ERROR"

        # B. Version-incompatible module
        class FutureIncompatibleModule(BaseModule):
            manifest = ModuleManifest(
                module_id="nexura.future.unsupported",
                version="2.0.0",
                kind="data_transform",
                title="Future Module",
                description="Requires SDK version 99.0.0",
                min_sdk_version="99.0.0",
            )

            async def invoke(
                self, ctx: ExecutionContext, inputs: dict[str, Any], cfg: dict[str, Any]
            ) -> ModuleResult:
                return ModuleResult.success(outputs={})

        # Register into local versioned registry directly to test graph validator
        from nexura_runtime.modules.registry import RegisteredModule, default_registry

        default_registry._modules[("nexura.future.unsupported", "2.0.0")] = RegisteredModule(
            manifest=FutureIncompatibleModule.manifest,
            target=FutureIncompatibleModule,
        )

        try:
            incompat_spec = {
                "name": "IncompatibleGraph",
                "entry_node": "fut_node",
                "nodes": {
                    "fut_node": {
                        "id": "fut_node",
                        "handler": "nexura.future.unsupported",
                        "module_id": "nexura.future.unsupported",
                        "module_version": "2.0.0",
                        "transitions": {},
                    }
                },
            }
            val_incompat = await client.post(
                "/api/v1/workflows/validate", json={"spec": incompat_spec}
            )
            assert val_incompat.status_code == 200
            incompat_errors = val_incompat.json()["errors"]
            assert any(e["code"] == "INCOMPATIBLE_SDK_VERSION" for e in incompat_errors)
        finally:
            default_registry._modules.pop(("nexura.future.unsupported", "2.0.0"), None)

        # C. Descriptor security checks
        list_res = await client.get("/api/v1/modules")
        assert list_res.status_code == 200
        all_modules = list_res.json()["items"]
        for m in all_modules:
            # Descriptors must never expose raw callable functions or Python references
            assert "execute" not in m
            assert "invoke" not in m
            assert "handler_fn" not in m
            # No credentials or secrets in output
            assert "api_key" not in m
            assert "secret" not in m

        # Querying an unknown module returns 404
        get_unknown = await client.get("/api/v1/modules/nonexistent.module.xyz")
        assert get_unknown.status_code == 404
        assert get_unknown.json()["detail"]["code"] == "MODULE_NOT_FOUND"


@pytest.mark.asyncio
async def test_negative_cases_config_mappings_and_unsupported_routes(app_fixture: Any) -> None:
    """Negative Case 2: Incorrect config, invalid input/output mappings, unsupported route."""
    app, _, _ = app_fixture

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # A. Invalid config (wrong type for max_output_length: string instead of int)
        bad_config_spec = {
            "name": "BadConfigWorkflow",
            "entry_node": "node1",
            "nodes": {
                "node1": {
                    "id": "node1",
                    "handler": "nexura.prompt.template",
                    "config": {
                        "max_output_length": "this_is_not_an_integer",
                    },
                    "transitions": {},
                }
            },
        }
        val_res = await client.post("/api/v1/workflows/validate", json={"spec": bad_config_spec})
        assert val_res.status_code == 200
        errors = val_res.json()["errors"]
        cfg_err = next((e for e in errors if e["code"] == "INVALID_CONFIG"), None)
        assert cfg_err is not None
        assert cfg_err["node_id"] == "node1"
        assert "config" in cfg_err["field"]

        # B. Invalid input port mapping
        bad_input_mapping_spec = {
            "name": "BadInputMappingWorkflow",
            "entry_node": "node1",
            "nodes": {
                "node1": {
                    "id": "node1",
                    "handler": "nexura.prompt.template",
                    "input_mapping": {
                        "nonexistent_input_port": "some_state_key",
                    },
                    "transitions": {},
                }
            },
        }
        val_input = await client.post(
            "/api/v1/workflows/validate", json={"spec": bad_input_mapping_spec}
        )
        assert val_input.status_code == 200
        errors = val_input.json()["errors"]
        in_err = next((e for e in errors if e["code"] == "INVALID_INPUT_MAPPING"), None)
        assert in_err is not None
        assert in_err["node_id"] == "node1"
        assert in_err["field"] == "input_mapping.nonexistent_input_port"

        # C. Invalid output port mapping
        bad_output_mapping_spec = {
            "name": "BadOutputMappingWorkflow",
            "entry_node": "node1",
            "nodes": {
                "node1": {
                    "id": "node1",
                    "handler": "nexura.prompt.template",
                    "output_mapping": {
                        "nonexistent_output_port": "some_target_state",
                    },
                    "transitions": {},
                }
            },
        }
        val_out = await client.post(
            "/api/v1/workflows/validate", json={"spec": bad_output_mapping_spec}
        )
        assert val_out.status_code == 200
        errors = val_out.json()["errors"]
        out_err = next((e for e in errors if e["code"] == "INVALID_OUTPUT_MAPPING"), None)
        assert out_err is not None
        assert out_err["node_id"] == "node1"
        assert out_err["field"] == "output_mapping.nonexistent_output_port"

        # D. Unsupported route / transition targeting non-existent node
        bad_route_spec = {
            "name": "BadRouteWorkflow",
            "entry_node": "node1",
            "nodes": {
                "node1": {
                    "id": "node1",
                    "handler": "nexura.prompt.template",
                    "transitions": {
                        "failed_branch": "phantom_target_node",
                    },
                }
            },
        }
        val_route = await client.post("/api/v1/workflows/validate", json={"spec": bad_route_spec})
        assert val_route.status_code == 200
        errors = val_route.json()["errors"]
        route_err = next((e for e in errors if e["code"] == "UNSUPPORTED_ROUTE"), None)
        assert route_err is not None
        assert route_err["node_id"] == "node1"
        assert route_err["field"] == "transitions.failed_branch"


@pytest.mark.asyncio
async def test_negative_cases_multi_instance_and_mandatory_approvals(app_fixture: Any) -> None:
    """Negative Case 3: Two module instances distinct; mandatory approvals enforced."""
    app, store, _ = app_fixture

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # A. Mandatory approval enforcement
        # synthetic_ticket_creator requires mandatory approval in this service configuration
        bypassed_approval_spec = {
            "name": "BypassApprovalAttempt",
            "entry_node": "ticket_node",
            "nodes": {
                "ticket_node": {
                    "id": "ticket_node",
                    "handler": "synthetic_ticket_creator",
                    "requires_approval": False,  # Attempting to disable mandatory approval
                    "transitions": {},
                }
            },
        }
        # 1. Validation fails
        val_appr = await client.post(
            "/api/v1/workflows/validate", json={"spec": bypassed_approval_spec}
        )
        assert val_appr.status_code == 200
        errors = val_appr.json()["errors"]
        appr_err = next((e for e in errors if e["code"] == "MANDATORY_APPROVAL_REQUIRED"), None)
        assert appr_err is not None
        assert appr_err["node_id"] == "ticket_node"
        assert appr_err["field"] == "requires_approval"

        # 2. Saving is rejected
        create_appr = await client.post("/api/v1/workflows", json={"spec": bypassed_approval_spec})
        assert create_appr.status_code == 422

        # 3. Direct execution with requires_approval=False halts execution with 422
        direct_run = await client.post(
            "/api/v1/runs",
            json={
                "spec": bypassed_approval_spec,
                "initial_state": {"order_id": "ORD-123"},
            },
        )
        assert direct_run.status_code == 422
        assert direct_run.json()["detail"]["code"] == "APPROVAL_POLICY_REQUIRED"

        # B. Two instances of the same module in one workflow
        multi_instance_spec = {
            "name": "TwoInstancesWorkflow",
            "entry_node": "step_alpha",
            "nodes": {
                "step_alpha": {
                    "id": "step_alpha",
                    "handler": "nexura.prompt.template",
                    "config": {
                        "default_variables": {"tag": "[INSTANCE-ALPHA]"},
                    },
                    "input_mapping": {
                        "template": "template_alpha",
                    },
                    "output_mapping": {
                        "rendered": "output_alpha",
                    },
                    "transitions": {"default": "step_beta"},
                    "default_transition": "step_beta",
                },
                "step_beta": {
                    "id": "step_beta",
                    "handler": "nexura.prompt.template",
                    "config": {
                        "default_variables": {"tag": "[INSTANCE-BETA]"},
                    },
                    "input_mapping": {
                        "template": "template_beta",
                    },
                    "output_mapping": {
                        "rendered": "output_beta",
                    },
                    "transitions": {},
                },
            },
        }

        run_multi = await client.post(
            "/api/v1/runs",
            json={
                "spec": multi_instance_spec,
                "initial_state": {
                    "template_alpha": "Alpha message: {{ tag }}",
                    "template_beta": "Beta message: {{ tag }}",
                },
            },
        )
        assert run_multi.status_code == 201
        multi_data = run_multi.json()
        assert multi_data["status"] == "completed"
        st = multi_data["current_state"]
        # Both ran with distinct configs
        assert st["output_alpha"] == "Alpha message: [INSTANCE-ALPHA]"
        assert st["output_beta"] == "Beta message: [INSTANCE-BETA]"

        # Verify distinct events emitted for each node instance
        events_res = await client.get(f"/api/v1/runs/{multi_data['run_id']}/events")
        assert events_res.status_code == 200
        events_str = events_res.text
        assert "step_alpha" in events_str
        assert "step_beta" in events_str


@pytest.mark.asyncio
async def test_presentation_metadata_separation_and_hash_stability() -> None:
    """Verify canvas coordinates do not mutate approval snapshots or executable spec identity."""
    node_a = WorkflowNode(
        id="calc",
        handler="mcp.local.calculator",
        transitions={},
        presentation={"position": {"x": 50, "y": 100}, "zoom": 1.0},
    )
    spec_a = WorkflowSpec(
        name="PresentationTest",
        entry_node="calc",
        nodes={"calc": node_a},
        presentation={"viewport": {"x": 0, "y": 0}},
    )

    node_b = WorkflowNode(
        id="calc",
        handler="mcp.local.calculator",
        transitions={},
        presentation={"position": {"x": 800, "y": 600}, "zoom": 2.5, "color": "red"},
    )
    spec_b = WorkflowSpec(
        name="PresentationTest",
        entry_node="calc",
        nodes={"calc": node_b},
        presentation={"viewport": {"x": 500, "y": 300}},
    )

    # 1. Executable canonical digests must be EXACTLY equal despite cosmetic UI differences
    hash_a = spec_a.canonical_hash()
    hash_b = spec_b.canonical_hash()
    assert hash_a == hash_b

    # 2. Serialization retains presentation for persistence
    dict_a = spec_a.to_dict(include_presentation=True)
    assert dict_a["presentation"]["viewport"]["x"] == 0
    assert dict_a["nodes"]["calc"]["presentation"]["position"]["x"] == 50

    # 3. Reloading preserves presentation
    reloaded_a = WorkflowSpec.from_dict(dict_a)
    assert reloaded_a.presentation["viewport"]["x"] == 0
    assert reloaded_a.nodes["calc"].presentation["position"]["x"] == 50
    assert reloaded_a.canonical_hash() == hash_a


@pytest.mark.asyncio
async def test_capabilities_and_live_sse_contract(app_fixture: Any) -> None:
    """Verify capabilities discovery endpoint and persistent SSE live event streaming."""
    app, store, _ = app_fixture

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # A. Capabilities discovery
        cap_res = await client.get("/api/v1/capabilities")
        assert cap_res.status_code == 200
        caps = cap_res.json()
        assert caps["sdk_version"] == CURRENT_SDK_VERSION
        assert caps["active_profile"] == "local-owner"
        assert "local-owner" in caps["supported_profiles"]
        assert caps["capabilities"]["modules"] is True
        assert caps["capabilities"]["validation"] is True
        assert caps["capabilities"]["downloadable_export"] is True
        assert "openai_api_key" in caps["supported_credential_types"]

        # B. Persistent live SSE stream
        # Create an approval-gated run
        gated_spec = {
            "name": "LiveStreamWorkflow",
            "entry_node": "gated_step",
            "nodes": {
                "gated_step": {
                    "id": "gated_step",
                    "handler": "synthetic_ticket_creator",
                    "requires_approval": True,
                    "transitions": {},
                }
            },
        }
        start_res = await client.post("/api/v1/runs", json={"spec": gated_spec})
        assert start_res.status_code == 201
        run_id = start_res.json()["run_id"]

        # Stream with live=true
        stream_res = await client.get(f"/api/v1/runs/{run_id}/events?live=true")
        assert stream_res.status_code == 200
        assert "text/event-stream" in stream_res.headers["content-type"]
        events_content = stream_res.text
        assert "RUN_PENDING_APPROVAL" in events_content


@pytest.mark.asyncio
async def test_python_nexura_client_integration(app_fixture: Any) -> None:
    """Verify external Python client methods for modules, validation, and capabilities."""
    app, _, _ = app_fixture

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as raw_http:
        client = NexuraClient(base_url="http://test", http_client=raw_http)

        # 1. Modules list
        modules_res = await client.modules.list(kind="data_transform")
        assert modules_res["total"] >= 1
        assert any(m["module_id"] == "nexura.prompt.template" for m in modules_res["items"])

        # 2. Module descriptor
        descriptor = await client.modules.get("nexura.prompt.template")
        assert descriptor["module_id"] == "nexura.prompt.template"
        assert descriptor["effect_policy"] == "read"

        # 3. Capabilities
        capabilities = await client.capabilities.get()
        assert capabilities["capabilities"]["modules"] is True

        # 4. Validate graph
        val_res = await client.workflows.validate(
            {
                "name": "ClientTest",
                "entry_node": "n1",
                "nodes": {
                    "n1": {
                        "id": "n1",
                        "handler": "nexura.prompt.template",
                        "transitions": {},
                    }
                },
            }
        )
        assert val_res["valid"] is True
        assert val_res["canonical_hash"] is not None
