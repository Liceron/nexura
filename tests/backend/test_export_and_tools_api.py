"""Integration and security tests for MCP/OpenAPI tools and runnable SDK export (F12)."""

from __future__ import annotations

import base64
import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from nexura_runtime.service.api.app import create_api_app


@pytest.fixture
def test_app(tmp_path: Path):
    db_file = tmp_path / "test_f12.sqlite3"
    app = create_api_app(db_path=db_file, profile="local-owner")
    return app


@pytest.mark.asyncio
async def test_integrations_status_and_discovery(test_app) -> None:
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 1. Status check
        status_res = await client.get("/api/v1/integrations/status")
        assert status_res.status_code == 200
        status_data = status_res.json()
        assert status_data["mcp"]["available"] is True
        assert status_data["openapi"]["available"] is True
        assert len(status_data["mcp"]["fixtures"]) >= 2
        assert len(status_data["openapi"]["fixtures"]) >= 2

        # 2. Discover MCP tools on math_fixture
        mcp_disc = await client.post(
            "/api/v1/integrations/mcp/discover",
            json={"server_name": "calc", "fixture_id": "math_fixture"},
        )
        assert mcp_disc.status_code == 200
        mcp_tools = mcp_disc.json()["tools"]
        assert len(mcp_tools) == 2
        tool_names = [t["name"] for t in mcp_tools]
        assert "math_add" in tool_names
        assert "math_multiply" in tool_names

        # 3. Test-run MCP math_add
        mcp_run = await client.post(
            "/api/v1/integrations/mcp/test-run",
            json={
                "server_name": "calc",
                "tool_name": "math_add",
                "arguments": {"x": 15, "y": 27},
                "fixture_id": "math_fixture",
            },
        )
        assert mcp_run.status_code == 200
        run_data = mcp_run.json()
        assert run_data["status"] == "success"
        assert run_data["is_fixture"] is True
        assert "Fixture Dry-Run" in run_data["execution_label"]
        assert "42" in str(run_data["output"])

        # 4. Negative test: unknown MCP tool
        unknown_run = await client.post(
            "/api/v1/integrations/mcp/test-run",
            json={
                "server_name": "calc",
                "tool_name": "non_existent_tool",
                "fixture_id": "math_fixture",
            },
        )
        assert unknown_run.status_code == 404

        # 5. Negative test: stdio path traversal command
        unsafe_cmd = await client.post(
            "/api/v1/integrations/mcp/discover",
            json={
                "transport_type": "stdio",
                "command": "../malicious_binary",
            },
        )
        assert unsafe_cmd.status_code == 400
        assert "PATH_TRAVERSAL_DETECTED" in unsafe_cmd.text


@pytest.mark.asyncio
async def test_openapi_discovery_and_test_run(test_app) -> None:
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 1. Discover petstore fixture
        pet_disc = await client.post(
            "/api/v1/integrations/openapi/discover",
            json={"fixture_id": "petstore_fixture", "prefix": "petstore"},
        )
        assert pet_disc.status_code == 200
        ops = pet_disc.json()["operations"]
        assert len(ops) >= 2
        op_ids = [op["operation_id"] for op in ops]
        assert "getPetById" in op_ids
        assert "addPet" in op_ids

        # 2. Test-run getPetById
        test_run = await client.post(
            "/api/v1/integrations/openapi/test-run",
            json={
                "operation_id": "getPetById",
                "prefix": "petstore",
                "fixture_id": "petstore_fixture",
                "inputs": {"petId": "123"},
                "mock_response": {"status_code": 200, "json": {"id": "123", "name": "Rover"}},
            },
        )
        assert test_run.status_code == 200
        res = test_run.json()
        assert res["status"] == "success"
        assert res["status_code"] == 200
        assert res["body"]["name"] == "Rover"
        assert res["is_fixture"] is True
        assert "Fixture Dry-Run" in res["execution_label"]

        # 3. Negative test: untrusted metadata SSRF URL
        ssrf_res = await client.post(
            "/api/v1/integrations/openapi/discover",
            json={"url": "http://169.254.169.254/latest/meta-data"},
        )
        assert ssrf_res.status_code == 400
        assert "UNTRUSTED_DESTINATION_DENIED" in ssrf_res.text

        # 4. Negative test: untrusted URL path traversal
        traversal_url = await client.post(
            "/api/v1/integrations/openapi/discover",
            json={"url": "http://api.example.com/v1/../../etc"},
        )
        assert traversal_url.status_code == 400
        assert "PATH_TRAVERSAL_DETECTED" in traversal_url.text

        # 5. Negative test: duplicate operation IDs
        dup_spec = {
            "openapi": "3.0.0",
            "info": {"title": "Dup API", "version": "1.0.0"},
            "paths": {
                "/a": {
                    "get": {"operationId": "dupOp", "responses": {"200": {"description": "ok"}}}
                },
                "/b": {
                    "get": {"operationId": "dupOp", "responses": {"200": {"description": "ok"}}}
                },
            },
        }
        dup_res = await client.post(
            "/api/v1/integrations/openapi/discover",
            json={"spec_dict": dup_spec},
        )
        assert dup_res.status_code == 400
        assert "DUPLICATE_OPERATION_ID" in dup_res.text

        # 6. Negative test: unsupported schema features (external ref)
        ext_spec = {
            "openapi": "3.0.0",
            "info": {"title": "Ext API", "version": "1.0.0"},
            "paths": {
                "/user": {
                    "post": {
                        "operationId": "createUser",
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "https://remote.schema/user.json"}
                                }
                            }
                        },
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            },
        }
        ext_res = await client.post(
            "/api/v1/integrations/openapi/discover",
            json={"spec_dict": ext_spec},
        )
        assert ext_res.status_code == 400
        assert "UNSUPPORTED_OPENAPI_FEATURE" in ext_res.text


@pytest.mark.asyncio
async def test_integration_registration(test_app) -> None:
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Register MCP tools
        reg_res = await client.post(
            "/api/v1/integrations/register",
            json={
                "kind": "mcp",
                "server_name_or_prefix": "calc",
                "fixture_id": "math_fixture",
            },
        )
        assert reg_res.status_code == 200
        reg_data = reg_res.json()
        assert reg_data["count"] >= 2
        assert "mcp.calc.math_add" in reg_data["registered_modules"]

        # Check module is visible in descriptors
        mod_res = await client.get("/api/v1/modules")
        assert mod_res.status_code == 200
        modules_list = mod_res.json()["items"]
        mod_ids = [m["module_id"] for m in modules_list]
        assert "mcp.calc.math_add" in mod_ids


@pytest.mark.asyncio
async def test_export_and_download_security(test_app, tmp_path: Path) -> None:
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 1. Create a workflow with sensitive secrets and host paths
        spec = {
            "name": "Export Security Test Workflow",
            "entry_node": "start_node",
            "nodes": {
                "start_node": {
                    "id": "start_node",
                    "handler": "synthetic_order_lookup",
                    "config": {
                        "api_key": "raw_sensitive_token_super_secret_123",
                        "password": "my_db_password_456",
                        "database_path": "/home/ubuntu/projects/Nexura V2/db.sqlite3",
                    },
                    "transitions": {},
                }
            },
            "terminal_nodes": ["start_node"],
        }
        create_res = await client.post(
            "/api/v1/workflows",
            json={"spec": spec, "name": "Secure Export Workflow"},
            headers={"X-Workspace-ID": "default"},
        )
        assert create_res.status_code == 201
        wf_id = create_res.json()["workflow_id"]

        # 2. Negative test: target_dir with path traversal
        unsafe_export = await client.post(
            f"/api/v1/workflows/{wf_id}/export",
            json={"target_dir": "../../etc/unsafe_path"},
            headers={"X-Workspace-ID": "default"},
        )
        assert unsafe_export.status_code == 400
        assert "PATH_TRAVERSAL_DETECTED" in unsafe_export.text

        # 3. Valid export to designated temp directory
        target_dir = tmp_path / "exported_bundle"
        export_res = await client.post(
            f"/api/v1/workflows/{wf_id}/export",
            json={"target_dir": str(target_dir)},
            headers={"X-Workspace-ID": "default"},
        )
        assert export_res.status_code == 200
        bundle = export_res.json()
        assert "workflow.json" in bundle["files"]
        assert "README.md" in bundle["files"]

        # Verify exported files:
        wf_json_path = target_dir / "workflow.json"
        wf_json_data = json.loads(wf_json_path.read_text(encoding="utf-8"))
        node_cfg = wf_json_data["nodes"]["start_node"]["config"]

        # Verify secrets are scrubbed to credential references
        assert "raw_sensitive_token" not in json.dumps(wf_json_data)
        assert "my_db_password" not in json.dumps(wf_json_data)
        assert node_cfg["api_key"] == {"ref": "API_KEY"}
        assert node_cfg["password"] == {"ref": "PASSWORD"}

        # Verify host database and parent workspace path scrubbed
        assert "/home/ubuntu/projects/Nexura V2" not in json.dumps(wf_json_data)
        assert node_cfg["database_path"] == "workflow.sqlite3"

        # Verify README.md contains credential reference instructions
        readme_text = (target_dir / "README.md").read_text(encoding="utf-8")
        assert "API_KEY" in readme_text
        assert "PASSWORD" in readme_text
        assert "Python" in readme_text
        assert "nexura-runtime" in readme_text

        # 4. Download zip archive and inspect archive safety
        dl_res = await client.get(
            f"/api/v1/workflows/{wf_id}/export/download",
            headers={"X-Workspace-ID": "default"},
        )
        assert dl_res.status_code == 200
        assert dl_res.headers["content-type"] == "application/zip"
        zip_bytes = dl_res.content

        with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
            file_names = zf.namelist()
            assert "workflow.json" in file_names
            assert "run.py" in file_names
            assert "requirements.txt" in file_names
            for f in file_names:
                assert ".." not in f
                assert not f.startswith("/")

            # Verify no secret leaked into downloaded zip workflow.json
            dl_wf_content = zf.read("workflow.json").decode("utf-8")
            assert "raw_sensitive_token" not in dl_wf_content


@pytest.mark.asyncio
async def test_import_workflow_and_archive_security(test_app, tmp_path: Path) -> None:
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 1. Create a safe zip bundle to import
        spec_data = {
            "name": "Re-Imported Safe Workflow",
            "entry_node": "step1",
            "nodes": {
                "step1": {
                    "id": "step1",
                    "handler": "synthetic_order_lookup",
                    "transitions": {},
                }
            },
            "terminal_nodes": ["step1"],
        }
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("workflow.json", json.dumps(spec_data))
            zf.writestr("requirements.txt", "nexura-runtime>=0.3.0\n")
            zf.writestr("README.md", "# Re-import test\n")

        zip_b64 = base64.b64encode(zip_buf.getvalue()).decode("utf-8")

        # 2. Re-import via API
        import_res = await client.post(
            "/api/v1/workflows/import",
            json={"archive_base64": zip_b64, "name": "Re-Imported Workflow"},
            headers={"X-Workspace-ID": "default"},
        )
        assert import_res.status_code == 201
        imported_wf = import_res.json()
        assert imported_wf["name"] == "Re-Imported Workflow"
        assert imported_wf["spec"]["name"] == "Re-Imported Workflow"

        # 3. Negative test: malicious zip slip with path traversal
        bad_buf = io.BytesIO()
        with zipfile.ZipFile(bad_buf, "w") as zf:
            zf.writestr("../../etc/evil.txt", "malicious payload")
            zf.writestr("workflow.json", json.dumps(spec_data))

        bad_b64 = base64.b64encode(bad_buf.getvalue()).decode("utf-8")
        bad_res = await client.post(
            "/api/v1/workflows/import",
            json={"archive_base64": bad_b64},
            headers={"X-Workspace-ID": "default"},
        )
        assert bad_res.status_code == 400
        assert "UNSAFE_ARCHIVE_CONTENT" in bad_res.text

        # 4. Negative test: malicious zip with executable file
        exe_buf = io.BytesIO()
        with zipfile.ZipFile(exe_buf, "w") as zf:
            zf.writestr("exploit.exe", "fake_binary")
            zf.writestr("workflow.json", json.dumps(spec_data))

        exe_b64 = base64.b64encode(exe_buf.getvalue()).decode("utf-8")
        exe_res = await client.post(
            "/api/v1/workflows/import",
            json={"archive_base64": exe_b64},
            headers={"X-Workspace-ID": "default"},
        )
        assert exe_res.status_code == 400
        assert "UNSAFE_ARCHIVE_CONTENT" in exe_res.text


def test_external_runner_execution(tmp_path: Path) -> None:
    """Validate that exported bundle can run standalone in external directory."""
    from nexura_runtime.service.api.export import WorkflowExporter
    from nexura_runtime.workflow import WorkflowSpec

    spec_data = {
        "name": "Standalone Execution Workflow",
        "entry_node": "first",
        "nodes": {
            "first": {
                "id": "first",
                "handler": "synthetic_order_lookup",
                "transitions": {},
            }
        },
        "terminal_nodes": ["first"],
    }
    spec = WorkflowSpec.from_dict(spec_data)
    bundle_dir = tmp_path / "standalone_run_bundle"

    exporter = WorkflowExporter()
    exporter.export_bundle(spec, target_dir=bundle_dir, workflow_id="standalone-test")

    # Run run.py directly using current python executable
    runner_file = bundle_dir / "run.py"
    proc = subprocess.run(
        [sys.executable, str(runner_file), '{"order_id": "ORD-DELHI-501"}'],
        cwd=str(bundle_dir),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert "Starting standalone workflow" in proc.stdout
    assert "Status: completed" in proc.stdout


@pytest.mark.asyncio
async def test_stdio_execution_disabled_security(test_app) -> None:
    """Validate that caller-supplied stdio execution is rejected without executing subprocess
    code."""
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 1. Stdio discovery rejected
        disc_res = await client.post(
            "/api/v1/integrations/mcp/discover",
            json={
                "transport_type": "stdio",
                "command": "python3",
                "args": ["-c", "import sys; sys.exit(0)"],
            },
        )
        assert disc_res.status_code == 403
        assert "STDIO_EXECUTION_DISABLED" in disc_res.text

        # 2. Stdio test-run rejected
        run_res = await client.post(
            "/api/v1/integrations/mcp/test-run",
            json={
                "transport_type": "stdio",
                "command": "python3",
                "tool_name": "any_tool",
            },
        )
        assert run_res.status_code == 403
        assert "STDIO_EXECUTION_DISABLED" in run_res.text


@pytest.mark.asyncio
async def test_workspace_isolation_and_restart_persistence(tmp_path: Path) -> None:
    """Validate workspace isolation for integration tools and durable persistence across
    restarts."""
    db_file = tmp_path / "ws_persist.sqlite3"
    app1 = create_api_app(db_path=db_file, profile="local-owner")
    transport1 = ASGITransport(app=app1)

    async with AsyncClient(transport=transport1, base_url="http://testserver") as client:
        # 1. Register in Workspace Alpha
        reg_alpha = await client.post(
            "/api/v1/integrations/register",
            json={
                "kind": "mcp",
                "server_name_or_prefix": "calc",
                "fixture_id": "math_fixture",
            },
            headers={"X-Workspace-ID": "ws-alpha"},
        )
        assert reg_alpha.status_code == 200
        assert "mcp.calc.math_add" in reg_alpha.json()["registered_modules"]

        # 2. Workspace Alpha sees the module in listing
        list_alpha = await client.get("/api/v1/modules", headers={"X-Workspace-ID": "ws-alpha"})
        alpha_mod_ids = [m["module_id"] for m in list_alpha.json()["items"]]
        assert "mcp.calc.math_add" in alpha_mod_ids

        # 3. Workspace Beta CANNOT see the module in listing
        list_beta = await client.get("/api/v1/modules", headers={"X-Workspace-ID": "ws-beta"})
        beta_mod_ids = [m["module_id"] for m in list_beta.json()["items"]]
        assert "mcp.calc.math_add" not in beta_mod_ids

        # 4. Workspace Beta cannot get descriptor directly (403 CROSS_WORKSPACE_DENIED)
        get_beta = await client.get(
            "/api/v1/modules/mcp.calc.math_add",
            headers={"X-Workspace-ID": "ws-beta"},
        )
        assert get_beta.status_code == 403
        assert "CROSS_WORKSPACE_DENIED" in get_beta.text

        # 5. Workspace Beta cannot overwrite Alpha's module ID (403 CROSS_WORKSPACE_DENIED)
        reg_beta = await client.post(
            "/api/v1/integrations/register",
            json={
                "kind": "mcp",
                "server_name_or_prefix": "calc",
                "fixture_id": "math_fixture",
            },
            headers={"X-Workspace-ID": "ws-beta"},
        )
        assert reg_beta.status_code == 403
        assert "CROSS_WORKSPACE_DENIED" in reg_beta.text

    # 6. Simulate restart: create new app instance from the same database
    app2 = create_api_app(db_path=db_file, profile="local-owner")
    transport2 = ASGITransport(app=app2)

    async with AsyncClient(transport=transport2, base_url="http://testserver") as client:
        # Registration persisted and still isolated to ws-alpha!
        list_alpha_restarted = await client.get(
            "/api/v1/modules", headers={"X-Workspace-ID": "ws-alpha"}
        )
        restarted_alpha_ids = [m["module_id"] for m in list_alpha_restarted.json()["items"]]
        assert "mcp.calc.math_add" in restarted_alpha_ids

        list_beta_restarted = await client.get(
            "/api/v1/modules", headers={"X-Workspace-ID": "ws-beta"}
        )
        restarted_beta_ids = [m["module_id"] for m in list_beta_restarted.json()["items"]]
        assert "mcp.calc.math_add" not in restarted_beta_ids


@pytest.mark.asyncio
async def test_registered_mcp_workflow_repeated_execution(test_app) -> None:
    """Validate that registered MCP tool survives multiple workflow runs without closed-session
    errors."""
    from nexura_runtime.workflow import WorkflowEngine, WorkflowSpec

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        reg_res = await client.post(
            "/api/v1/integrations/register",
            json={
                "kind": "mcp",
                "server_name_or_prefix": "calc",
                "fixture_id": "math_fixture",
            },
        )
        assert reg_res.status_code == 200

    # Build workflow utilizing registered MCP tool
    spec_data = {
        "name": "MCP Math Workflow",
        "entry_node": "add_node",
        "nodes": {
            "add_node": {
                "id": "add_node",
                "handler": "mcp.calc.math_add",
                "input_mapping": {"x": "num1", "y": "num2"},
                "output_mapping": {"result": "sum_out"},
                "transitions": {},
            }
        },
        "terminal_nodes": ["add_node"],
    }
    spec = WorkflowSpec.from_dict(spec_data)
    registry = test_app.state.registry
    engine = WorkflowEngine(spec=spec, registry=registry)

    # First invocation
    res1 = await engine.run(initial_state={"num1": 15, "num2": 27})
    assert res1.status == "completed"
    assert "42" in str(res1.state.get("sum_out"))

    # Second invocation (must succeed cleanly with fresh active session)
    res2 = await engine.run(initial_state={"num1": 100, "num2": 250})
    assert res2.status == "completed"
    assert "350" in str(res2.state.get("sum_out"))


@pytest.mark.asyncio
async def test_archive_malformed_and_boundary_security(test_app) -> None:
    """Validate that corrupt ZIP archives and missing workflow.json return controlled 400 errors."""
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 1. Corrupted non-ZIP bytes
        corrupt_b64 = base64.b64encode(b"this is definitely not a zip file").decode("utf-8")
        corrupt_res = await client.post(
            "/api/v1/workflows/import",
            json={"archive_base64": corrupt_b64},
            headers={"X-Workspace-ID": "default"},
        )
        assert corrupt_res.status_code == 400
        assert "INVALID_ARCHIVE" in corrupt_res.text
        assert "Malformed ZIP archive" in corrupt_res.text

        # 2. Valid zip but missing workflow.json
        empty_zip_buf = io.BytesIO()
        with zipfile.ZipFile(empty_zip_buf, "w") as zf:
            zf.writestr("notes.txt", "No workflow here")
        missing_wf_b64 = base64.b64encode(empty_zip_buf.getvalue()).decode("utf-8")

        missing_res = await client.post(
            "/api/v1/workflows/import",
            json={"archive_base64": missing_wf_b64},
            headers={"X-Workspace-ID": "default"},
        )
        assert missing_res.status_code == 400
        assert "INVALID_ARCHIVE" in missing_res.text
        assert "does not contain workflow.json" in missing_res.text


def test_secret_scrubbing_preserves_non_secret_fields(tmp_path: Path) -> None:
    """Validate that non-secret configuration (token_limit, primary_key) is preserved while
    secrets are scrubbed."""
    from nexura_runtime.service.api.export import WorkflowExporter
    from nexura_runtime.workflow import WorkflowSpec

    spec_data = {
        "name": "Config Preservation Workflow",
        "entry_node": "test_node",
        "nodes": {
            "test_node": {
                "id": "test_node",
                "handler": "synthetic_order_lookup",
                "config": {
                    "api_key": "secret_token_val_123",
                    "password": "my_db_password_456",
                    "token_limit": 5000,
                    "max_tokens": 1024,
                    "primary_key": "order_id",
                    "idempotency_key": "idem_abc",
                },
                "transitions": {},
            }
        },
        "terminal_nodes": ["test_node"],
    }
    spec = WorkflowSpec.from_dict(spec_data)
    bundle_dir = tmp_path / "preservation_bundle"

    exporter = WorkflowExporter()
    bundle = exporter.export_bundle(spec, target_dir=bundle_dir)

    # Inspect exported workflow.json
    wf_json = json.loads((bundle_dir / "workflow.json").read_text(encoding="utf-8"))
    cfg = wf_json["nodes"]["test_node"]["config"]

    # Secrets scrubbed to references
    assert cfg["api_key"] == {"ref": "API_KEY"}
    assert cfg["password"] == {"ref": "PASSWORD"}

    # Non-secret fields preserved untouched
    assert cfg["token_limit"] == 5000
    assert cfg["max_tokens"] == 1024
    assert cfg["primary_key"] == "order_id"
    assert cfg["idempotency_key"] == "idem_abc"

    # Both hashes tracked
    assert bundle.source_spec_hash != ""
    assert bundle.exported_spec_hash != ""


def test_exported_integration_workflow_standalone(tmp_path: Path) -> None:
    """Validate that exported bundle with MCP connectors and env vars executes standalone."""
    import os

    from nexura_runtime.service.api.export import WorkflowExporter
    from nexura_runtime.workflow import WorkflowSpec

    spec_data = {
        "name": "Exported MCP Calculator Workflow",
        "entry_node": "math_node",
        "nodes": {
            "math_node": {
                "id": "math_node",
                "handler": "mcp.calc.math_add",
                "config": {"api_key": "raw_secret_value_xyz"},
                "input_mapping": {"x": 10, "y": 25},
                "output_mapping": {"result": "total"},
                "transitions": {},
            }
        },
        "terminal_nodes": ["math_node"],
    }
    spec = WorkflowSpec.from_dict(spec_data)
    bundle_dir = tmp_path / "integration_bundle"

    exporter = WorkflowExporter()
    bundle = exporter.export_bundle(spec, target_dir=bundle_dir, workflow_id="mcp-calc-export")

    assert "connectors.json" in bundle.files
    assert "run.py" in bundle.files

    # Execute run.py in subprocess with env var set for the scrubbed secret
    runner_file = bundle_dir / "run.py"
    env = os.environ.copy()
    env["API_KEY"] = "configured_secret_for_run"
    proc = subprocess.run(
        [sys.executable, str(runner_file)],
        cwd=str(bundle_dir),
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, f"STDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
    assert "Starting standalone workflow" in proc.stdout
    assert "Status: completed" in proc.stdout
    assert "35" in proc.stdout
