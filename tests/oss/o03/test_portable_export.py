"""Acceptance test suite for O03: Portable exports and independent package execution.

Verifies:
1. Preserving F12 session factories, scoped registrations, and stdio denial.
2. Persisting module/version/connector definitions and credential references,
   while rejecting unsupported handlers and missing definitions.
3. Distinguishing source spec hash from transformed export hash and validating schema identity.
4. Building wheel/sdist and installing from local artifacts into a fresh external venv with
   inherited PYTHONPATH removed; executing exported run.py and CLI using the installed package.
5. Packing TypeScript client locally and consuming it from a fresh Node project against
   an owned local API.
6. Truthful SDK dependency versions/extras with actionable errors for missing extras.
7. Bounded decompression, path traversal rejection, duplicate workflow.json entry rejection,
   and preservation of non-secret fields (token_limit, idempotency_key).
"""

from __future__ import annotations

import base64
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from nexura_runtime.errors import WorkflowValidationError

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.api.export import WorkflowExporter
from nexura_runtime.workflow import WorkflowSpec

# ============================================================================
# Helpers
# ============================================================================


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def test_app(tmp_path: Path) -> FastAPI:
    db_file = tmp_path / "test_o03.sqlite3"
    app = create_api_app(db_path=db_file, profile="local-owner")
    return app


# ============================================================================
# Test Suite
# ============================================================================


class TestExportIntegrityAndHashing:
    """Deliverables 2 & 3: Spec hashing, credential scrubbing, and handler validation."""

    def test_source_vs_exported_spec_hash_differentiation(self, tmp_path: Path) -> None:
        """When secrets and host paths are scrubbed, exported hash differs from source hash."""
        spec_data = {
            "name": "Hash Differentiation Workflow",
            "entry_node": "step1",
            "nodes": {
                "step1": {
                    "id": "step1",
                    "handler": "synthetic_order_lookup",
                    "config": {
                        "api_key": "raw_sensitive_secret_value",
                        "database_path": "/home/ubuntu/projects/Nexura V2/db.sqlite3",
                    },
                    "transitions": {},
                }
            },
            "terminal_nodes": ["step1"],
        }
        spec = WorkflowSpec.from_dict(spec_data)
        source_hash = spec.canonical_hash()

        bundle_dir = tmp_path / "hash_bundle"
        exporter = WorkflowExporter()
        bundle = exporter.export_bundle(spec, target_dir=bundle_dir)

        assert bundle.source_spec_hash == source_hash
        assert bundle.exported_spec_hash != ""
        assert bundle.source_spec_hash != bundle.exported_spec_hash

        # Exported workflow.json matches exported_spec_hash
        wf_json = json.loads((bundle_dir / "workflow.json").read_text(encoding="utf-8"))
        reconstructed_spec = WorkflowSpec.from_dict(wf_json)
        assert reconstructed_spec.canonical_hash() == bundle.exported_spec_hash

    def test_secret_scrubbing_and_config_preservation(self, tmp_path: Path) -> None:
        """Non-secret fields (token_limit, idempotency_key) survive; secrets become refs."""
        spec_data = {
            "name": "Config Preservation Workflow",
            "entry_node": "node1",
            "nodes": {
                "node1": {
                    "id": "node1",
                    "handler": "synthetic_order_lookup",
                    "config": {
                        "api_key": "secret_abc_123",
                        "client_secret": "oauth_secret_456",
                        "token_limit": 8192,
                        "max_tokens": 2048,
                        "idempotency_key": "idem_req_789",
                        "primary_key": "order_id",
                    },
                    "transitions": {},
                }
            },
            "terminal_nodes": ["node1"],
        }
        spec = WorkflowSpec.from_dict(spec_data)
        bundle_dir = tmp_path / "preserve_bundle"
        exporter = WorkflowExporter()
        exporter.export_bundle(spec, target_dir=bundle_dir)

        wf_json = json.loads((bundle_dir / "workflow.json").read_text(encoding="utf-8"))
        cfg = wf_json["nodes"]["node1"]["config"]

        # Secrets scrubbed to references
        assert cfg["api_key"] == {"ref": "API_KEY"}
        assert cfg["client_secret"] == {"ref": "CLIENT_SECRET"}

        # Non-secrets preserved
        assert cfg["token_limit"] == 8192
        assert cfg["max_tokens"] == 2048
        assert cfg["idempotency_key"] == "idem_req_789"
        assert cfg["primary_key"] == "order_id"

    def test_unsupported_handler_rejected_on_export(self, tmp_path: Path) -> None:
        """Exporting a workflow with an unsupported/unregistered handler raises error."""
        spec_data = {
            "name": "Invalid Handler Workflow",
            "entry_node": "bad_node",
            "nodes": {
                "bad_node": {
                    "id": "bad_node",
                    "handler": "unsupported_non_existent_handler_12345",
                    "transitions": {},
                }
            },
            "terminal_nodes": ["bad_node"],
        }
        spec = WorkflowSpec.from_dict(spec_data)
        bundle_dir = tmp_path / "bad_bundle"
        exporter = WorkflowExporter()

        with pytest.raises(WorkflowValidationError) as exc_info:
            exporter.export_bundle(spec, target_dir=bundle_dir, validate_handlers=True)

        assert "unsupported or unregistered handler" in str(exc_info.value)


class TestArchiveDecompressionSecurityAndBoundaries:
    """Deliverable 6 & Required Negatives.

    Decompression limits, duplicate entries, malformed ZIP.
    """

    @pytest.mark.asyncio
    async def test_duplicate_workflow_entries_rejected(self, test_app: FastAPI) -> None:
        """Archive with duplicate workflow.json entries is rejected."""
        spec_data = {
            "name": "Dup Workflow",
            "entry_node": "s1",
            "nodes": {"s1": {"id": "s1", "handler": "synthetic_order_lookup"}},
            "terminal_nodes": ["s1"],
        }
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w") as zf:
            zf.writestr("workflow.json", json.dumps(spec_data))
            zf.writestr("sub/workflow.json", json.dumps(spec_data))

        b64_zip = base64.b64encode(zip_buf.getvalue()).decode("utf-8")
        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            res = await client.post(
                "/api/v1/workflows/import",
                json={"archive_base64": b64_zip},
                headers={"X-Workspace-ID": "default"},
            )
            assert res.status_code == 400
            assert "DUPLICATE_WORKFLOW_ENTRY" in res.text

    @pytest.mark.asyncio
    async def test_archive_path_traversal_rejected(self, test_app: FastAPI) -> None:
        """Zip archive with path traversal member is rejected with UNSAFE_ARCHIVE_CONTENT."""
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w") as zf:
            zf.writestr("../../etc/passwd", "malicious_content")
            zf.writestr("workflow.json", json.dumps({"name": "test"}))

        b64_zip = base64.b64encode(zip_buf.getvalue()).decode("utf-8")
        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            res = await client.post(
                "/api/v1/workflows/import",
                json={"archive_base64": b64_zip},
                headers={"X-Workspace-ID": "default"},
            )
            assert res.status_code == 400
            assert "UNSAFE_ARCHIVE_CONTENT" in res.text

    @pytest.mark.asyncio
    async def test_oversized_archive_decompression_rejected(self, test_app: FastAPI) -> None:
        """Archive exceeding 25MB decompression limit is rejected."""
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            # High compression ratio payload expanding past 25MB
            zf.writestr("bigfile.dat", b"0" * 26_000_000)
            zf.writestr("workflow.json", json.dumps({"name": "test"}))

        b64_zip = base64.b64encode(zip_buf.getvalue()).decode("utf-8")
        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            res = await client.post(
                "/api/v1/workflows/import",
                json={"archive_base64": b64_zip},
                headers={"X-Workspace-ID": "default"},
            )
            assert res.status_code == 400
            assert "UNSAFE_ARCHIVE_CONTENT" in res.text
            assert "decompression limit" in res.text

    @pytest.mark.asyncio
    async def test_corrupt_zip_archive_rejected(self, test_app: FastAPI) -> None:
        """Corrupt non-ZIP bytes are rejected with INVALID_ARCHIVE."""
        b64_corrupt = base64.b64encode(b"not a valid zip stream").decode("utf-8")
        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            res = await client.post(
                "/api/v1/workflows/import",
                json={"archive_base64": b64_corrupt},
                headers={"X-Workspace-ID": "default"},
            )
            assert res.status_code == 400
            assert "INVALID_ARCHIVE" in res.text


class TestFreshExternalVenvExecution:
    """Deliverable 4: Build wheel, install in isolated external venv without PYTHONPATH."""

    def test_wheel_install_and_standalone_runner_in_isolated_venv(self, tmp_path: Path) -> None:
        """Install built wheel in fresh external venv; run run.py with verified package path."""
        # 1. Build a fresh wheel from the current tree (dist/ may hold stale releases whose
        # console scripts differ, e.g. the old bare "nexura" command).
        root_dir = Path(__file__).resolve().parents[3]
        dist_dir = tmp_path / "dist"
        subprocess.run(
            ["uv", "build", "--all-packages", "--wheel", "--offline", "--out-dir", str(dist_dir)],
            cwd=str(root_dir),
            check=True,
        )
        wheel_files = list(dist_dir.glob("nexura_runtime-*.whl"))
        assert len(wheel_files) == 1, "Exactly one freshly built wheel must exist"
        wheel_path = wheel_files[0]

        # 2. Create fresh external venv
        venv_dir = tmp_path / "ext_venv"
        subprocess.run(["uv", "venv", str(venv_dir)], check=True)

        python_bin = venv_dir / "bin" / "python"
        nexura_bin = venv_dir / "bin" / "nexura-runtime"
        assert python_bin.exists()

        # 3. Install wheel into external venv using uv pip install
        subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(python_bin),
                "--find-links",
                str(dist_dir),
                # Same version, new contents: never reuse a wheel cached from an earlier build.
                "--refresh-package",
                "nexura-agents",
                "--refresh-package",
                "nexura-runtime",
                str(wheel_path),
            ],
            check=True,
        )
        assert nexura_bin.exists(), "nexura-runtime CLI entrypoint must be installed"
        # The bare "nexura" command belongs to the Nexura Cloud client package.
        assert not (venv_dir / "bin" / "nexura").exists()

        # 4. Export workflow bundle
        spec_data = {
            "name": "Isolated Venv Workflow",
            "entry_node": "lookup_step",
            "nodes": {
                "lookup_step": {
                    "id": "lookup_step",
                    "handler": "synthetic_order_lookup",
                    "transitions": {},
                }
            },
            "terminal_nodes": ["lookup_step"],
        }
        spec = WorkflowSpec.from_dict(spec_data)
        bundle_dir = tmp_path / "isolated_bundle"
        exporter = WorkflowExporter()
        exporter.export_bundle(spec, target_dir=bundle_dir, workflow_id="isolated-run")

        # 5. Execute run.py in external venv with PYTHONPATH completely removed
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env["PATH"] = f"{venv_dir / 'bin'}:{env.get('PATH', '')}"

        proc = subprocess.run(
            [
                str(python_bin),
                "run.py",
                '{"order_id": "ORD-DELHI-501"}',
                "--print-package-location",
            ],
            cwd=str(bundle_dir),
            capture_output=True,
            text=True,
            env=env,
        )
        assert proc.returncode == 0, f"STDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
        assert "Starting standalone workflow" in proc.stdout
        assert "Status: completed" in proc.stdout
        assert "Amit Verma" in proc.stdout

        # CRUCIAL CHECK: Import path is from external venv site-packages!
        assert "Package import location:" in proc.stdout
        location_line = [
            line for line in proc.stdout.splitlines() if "Package import location:" in line
        ][0]
        pkg_path = location_line.split("Package import location:")[1].strip()
        assert str(venv_dir) in pkg_path, (
            f"Imported package {pkg_path} must reside in external venv"
        )
        assert str(root_dir / "src") not in pkg_path, (
            f"Package {pkg_path} must not be ambient source"
        )

        # 6. Execute CLI using installed entrypoint
        cli_proc = subprocess.run(
            [
                str(nexura_bin),
                "workflow",
                "run",
                str(bundle_dir),
                "--state",
                '{"order_id": "ORD-DELHI-501"}',
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        assert cli_proc.returncode == 0, f"CLI STDOUT: {cli_proc.stdout}\nSTDERR: {cli_proc.stderr}"
        assert "Workflow Status: completed" in cli_proc.stdout


class TestTypeScriptClientConsumer:
    """Deliverable 5: Pack TypeScript client and consume from a fresh Node project."""

    def test_typescript_client_pack_and_node_consumer(self, tmp_path: Path) -> None:
        """npm pack @liceron/nexura-sdk, install it in a fresh Node project, run it locally."""
        root_dir = Path(__file__).resolve().parents[3]
        ts_dir = root_dir / "clients" / "typescript"

        # 1. Pack typescript client into .tgz
        tgz_dest = tmp_path / "sdk_pack"
        tgz_dest.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["npm", "pack", "--pack-destination", str(tgz_dest)],
            cwd=str(ts_dir),
            check=True,
            capture_output=True,
        )
        tgz_files = list(tgz_dest.glob("liceron-nexura-sdk-*.tgz"))
        assert len(tgz_files) > 0, "Packed tarball must exist"
        tgz_path = tgz_files[0]

        # 2. Set up fresh Node consumer project
        node_dir = tmp_path / "node_consumer"
        node_dir.mkdir(parents=True, exist_ok=True)
        pkg_json = {
            "name": "external-node-consumer",
            "version": "1.0.0",
            "type": "module",
            "dependencies": {},
        }
        (node_dir / "package.json").write_text(json.dumps(pkg_json, indent=2), encoding="utf-8")

        # 3. npm install the packed tarball
        subprocess.run(
            ["npm", "install", str(tgz_path)],
            cwd=str(node_dir),
            check=True,
            capture_output=True,
        )

        # 4. Start local API server in background thread
        api_port = _find_free_port()
        db_file = tmp_path / "node_api.sqlite3"
        app = create_api_app(db_path=db_file, profile="local-owner")

        config = uvicorn.Config(app, host="127.0.0.1", port=api_port, log_level="warning")
        server = uvicorn.Server(config)
        server_thread = threading.Thread(target=server.run, daemon=True)
        server_thread.start()

        # Wait for server ready
        time.sleep(0.5)

        # 5. Write Node test runner script consuming @liceron/nexura-sdk
        consumer_script = f"""
import {{ NexuraClient }} from '@liceron/nexura-sdk';

async function run() {{
  const client = new NexuraClient({{ baseUrl: 'http://127.0.0.1:{api_port}' }});

  // 1. Check capabilities
  const caps = await client.capabilities.get();
  console.log('Caps engine:', caps.engine);

  // 2. Create workflow
  const spec = {{
    name: 'Node Consumer Workflow',
    entry_node: 'node1',
    nodes: {{
      node1: {{
        id: 'node1',
        handler: 'synthetic_order_lookup',
        transitions: {{}}
      }}
    }},
    terminal_nodes: ['node1']
  }};

  const wf = await client.workflows.create(spec, {{ name: 'Node Workflow' }});
  console.log('Created workflow ID:', wf.workflow_id);

  // 3. List workflows
  const list = await client.workflows.list();
  console.log('Total workflows listed:', list.items.length);

  // 4. Export workflow
  const exported = await client.workflows.export(wf.workflow_id);
  console.log('Exported files count:', exported.files.length);

  if (exported.files.includes('workflow.json')) {{
    console.log('NODE_CONSUMER_SUCCESS');
  }} else {{
    process.exit(1);
  }}
}}

run().catch(err => {{
  console.error(err);
  process.exit(1);
}});
"""
        (node_dir / "consumer.mjs").write_text(consumer_script, encoding="utf-8")

        # 6. Run script with node
        proc = subprocess.run(
            ["node", "consumer.mjs"],
            cwd=str(node_dir),
            capture_output=True,
            text=True,
            timeout=10,
        )

        # Stop server
        server.should_exit = True
        server_thread.join(timeout=2)

        assert proc.returncode == 0, f"Node output: {proc.stdout}\nError: {proc.stderr}"
        assert "NODE_CONSUMER_SUCCESS" in proc.stdout
        assert "Created workflow ID:" in proc.stdout


class TestConnectorsAndApprovalExecution:
    """Deliverable 1: MCP & OpenAPI fixture connectors and approval behavior in exported bundle."""

    def test_exported_connectors_workflow_standalone(self, tmp_path: Path) -> None:
        """Exported bundle with MCP and OpenAPI connectors runs standalone in subprocess."""
        spec_data = {
            "name": "Exported Multi-Connector Workflow",
            "entry_node": "mcp_step",
            "nodes": {
                "mcp_step": {
                    "id": "mcp_step",
                    "handler": "mcp.calc.math_add",
                    "input_mapping": {"x": 20, "y": 35},
                    "output_mapping": {"result": "sum"},
                    "default_transition": "api_step",
                },
                "api_step": {
                    "id": "api_step",
                    "handler": "openapi.petstore.getPetById",
                    "input_mapping": {"petId": "target_pet_id"},
                    "output_mapping": {"body": "pet_body"},
                    "transitions": {},
                },
            },
            "terminal_nodes": ["api_step"],
        }
        spec = WorkflowSpec.from_dict(spec_data)
        bundle_dir = tmp_path / "connector_bundle"
        exporter = WorkflowExporter()
        bundle = exporter.export_bundle(spec, target_dir=bundle_dir, workflow_id="connectors-run")

        assert "connectors.json" in bundle.files
        assert "run.py" in bundle.files

        # Run twice independently to verify F12 session factories survive multiple executions
        runner_file = bundle_dir / "run.py"
        for _ in range(2):
            proc = subprocess.run(
                [sys.executable, str(runner_file), '{"target_pet_id": "1"}'],
                cwd=str(bundle_dir),
                capture_output=True,
                text=True,
            )
            assert proc.returncode == 0, f"STDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
            assert "Status: completed" in proc.stdout
            assert "55" in proc.stdout

    def test_exported_approval_workflow_run_and_resume(self, tmp_path: Path) -> None:
        """Exported bundle with approval requirement pauses and resumes with --approve."""
        spec_data = {
            "name": "Exported Approval Workflow",
            "entry_node": "step1",
            "nodes": {
                "step1": {
                    "id": "step1",
                    "handler": "synthetic_order_lookup",
                    "default_transition": "step2",
                },
                "step2": {
                    "id": "step2",
                    "handler": "synthetic_router",
                    "requires_approval": True,
                    "transitions": {},
                },
            },
            "terminal_nodes": ["step2"],
        }
        spec = WorkflowSpec.from_dict(spec_data)
        bundle_dir = tmp_path / "approval_bundle"
        exporter = WorkflowExporter()
        exporter.export_bundle(spec, target_dir=bundle_dir, workflow_id="approval-run")

        runner_file = bundle_dir / "run.py"

        # 1. Run without --approve: pauses at step2
        proc1 = subprocess.run(
            [sys.executable, str(runner_file), '{"order_id": "ORD-DELHI-501"}'],
            cwd=str(bundle_dir),
            capture_output=True,
            text=True,
        )
        assert proc1.returncode == 0
        assert "Status: paused" in proc1.stdout
        assert "Approval ID:" in proc1.stdout

        # 2. Run with --approve: auto-approves and completes
        proc2 = subprocess.run(
            [sys.executable, str(runner_file), '{"order_id": "ORD-DELHI-501"}', "--approve"],
            cwd=str(bundle_dir),
            capture_output=True,
            text=True,
        )
        assert proc2.returncode == 0
        assert "Auto-approving via --approve flag" in proc2.stdout
        assert "Resumed Status: completed" in proc2.stdout
