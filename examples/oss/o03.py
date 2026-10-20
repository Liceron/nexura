"""O03 Example: Portable Bundle Export and Independent Execution.

Demonstrates:
1. Designing a workflow with sensitive secrets, non-secret parameters, and connectors.
2. Exporting a self-contained, scrubbed bundle via WorkflowExporter:
   - Secrets replaced with CredentialReference placeholders (e.g. {"ref": "API_KEY"}).
   - Non-secret limits preserved (token_limit=4096, idempotency_key="tx_123").
   - Host paths scrubbed to portable relative paths.
   - Distinct source spec hash and exported spec hash recorded.
   - Automated connectors.json and self-contained run.py generated.
3. Executing the exported run.py in an independent subprocess with environment variables.
4. Verifying standalone execution and connector resolution with zero live credentials.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from nexura_runtime.service.api.export import WorkflowExporter
from nexura_runtime.workflow import WorkflowNode, WorkflowSpec


def main() -> None:
    print("=== O03: Portable Bundle Export & Standalone Execution Demo ===")

    with tempfile.TemporaryDirectory() as temp_dir:
        bundle_dir = Path(temp_dir) / "portable_payout_workflow"

        # 1. Define workflow specification
        # Includes secrets that MUST be scrubbed, non-secrets that MUST be preserved,
        # and an offline MCP math connector.
        spec = WorkflowSpec(
            name="portable_payout_pipeline",
            entry_node="fetch_order",
            terminal_nodes=["calculate_fee"],
            nodes={
                "fetch_order": WorkflowNode(
                    id="fetch_order",
                    handler="synthetic_order_lookup",
                    config={
                        "api_key": "super_secret_production_key_never_leak_999",
                        "password": "db_auth_password_do_not_publish",
                        "database_path": "/home/ubuntu/projects/Nexura V2/live_db.sqlite3",
                        "token_limit": 4096,
                        "max_tokens": 1024,
                        "idempotency_key": "payout_idem_001",
                        "primary_key": "order_id",
                    },
                    default_transition="calculate_fee",
                ),
                "calculate_fee": WorkflowNode(
                    id="calculate_fee",
                    handler="mcp.calc.math_add",
                    input_mapping={"x": 50, "y": 4500},
                    output_mapping={"result": "total_with_fee"},
                ),
            },
        )

        print("\n1. Source Workflow Spec:")
        print(f"   Name: {spec.name}")
        print(f"   Nodes: {list(spec.nodes.keys())}")
        source_hash = spec.canonical_hash()
        print(f"   Source Spec Canonical Hash: {source_hash}")

        # 2. Export standalone bundle
        print("\n2. Exporting portable bundle...")
        exporter = WorkflowExporter()
        bundle = exporter.export_bundle(
            spec=spec,
            target_dir=bundle_dir,
            workflow_id="payout-portable-01",
        )

        print(f"   Export Target: {bundle.target_dir}")
        print(f"   Exported Spec Canonical Hash: {bundle.exported_spec_hash}")
        print(f"   Hash Mutation Detected: {bundle.source_spec_hash != bundle.exported_spec_hash}")
        print(f"   Generated Bundle Files: {', '.join(bundle.files)}")

        # 3. Inspect scrubbed workflow.json
        print("\n3. Inspecting scrubbed workflow.json:")
        wf_json = json.loads((bundle_dir / "workflow.json").read_text(encoding="utf-8"))
        node_cfg = wf_json["nodes"]["fetch_order"]["config"]

        print(f"   api_key -> {node_cfg['api_key']}")
        print(f"   password -> {node_cfg['password']}")
        print(f"   database_path -> {node_cfg['database_path']}")
        print(f"   token_limit (preserved) -> {node_cfg['token_limit']}")
        print(f"   idempotency_key (preserved) -> {node_cfg['idempotency_key']}")

        assert node_cfg["api_key"] == {"ref": "API_KEY"}
        assert node_cfg["password"] == {"ref": "PASSWORD"}
        assert node_cfg["database_path"] == "workflow.sqlite3"
        assert node_cfg["token_limit"] == 4096
        assert node_cfg["idempotency_key"] == "payout_idem_001"

        # 4. Inspect generated connectors.json
        print("\n4. Inspecting connectors.json:")
        conn_json = json.loads((bundle_dir / "connectors.json").read_text(encoding="utf-8"))
        print(f"   Connector kind: {conn_json[0]['kind']}")
        print(f"   Server name: {conn_json[0]['server_name']}")
        print(f"   Tools: {conn_json[0]['tools']}")

        # 5. Execute run.py in an isolated subprocess with environment variables
        print("\n5. Executing standalone run.py in subprocess...")
        runner_file = bundle_dir / "run.py"

        env = os.environ.copy()
        env["API_KEY"] = "dynamically_injected_vault_secret"
        env["PASSWORD"] = "dynamically_injected_db_pass"

        proc = subprocess.run(
            [
                sys.executable,
                str(runner_file),
                '{"order_id": "ORD-DELHI-501"}',
                "--print-package-location",
            ],
            cwd=str(bundle_dir),
            capture_output=True,
            text=True,
            env=env,
        )

        print("--- Subprocess STDOUT ---")
        print(proc.stdout.strip())
        if proc.stderr:
            print("--- Subprocess STDERR ---")
            print(proc.stderr.strip())

        assert proc.returncode == 0
        assert "Status: completed" in proc.stdout
        assert "4550" in proc.stdout
        print("\n=== O03 Demonstration Completed Successfully ===")


if __name__ == "__main__":
    main()
