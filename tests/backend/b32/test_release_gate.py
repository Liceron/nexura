"""Focused tests for B32 Packaging, Deployment, and Backend Release Gate."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

from nexura_runtime.artifacts import LocalArtifactStore
from nexura_runtime.release import (
    BackupEngine,
    BackupIntegrityError,
    HealthChecker,
    ReleaseDiagnostics,
    WorkflowMigrator,
    get_matrix,
    get_module,
)
from nexura_runtime.runtime import (
    DurableExecutionEngine,
    DurableNodeSpec,
    DurableWorkflowSpec,
    FencingConflictError,
)
from nexura_runtime.storage import SQLiteRepository
from nexura_runtime.workflow import (
    ModuleRegistry,
    SQLiteWorkflowStore,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
)


def test_capability_matrix_completeness() -> None:
    """The capability matrix inventories all 33 modules B00 through B32."""
    matrix = get_matrix()
    assert len(matrix) == 33
    codes = [m["code"] for m in matrix]
    for i in range(33):
        expected_code = f"B{i:02d}"
        assert expected_code in codes

    b32 = get_module("B32")
    assert b32 is not None
    from nexura_runtime.release.matrix import validate_matrix

    assert b32.name == "Packaging, deployment and backend release gate"
    assert validate_matrix(Path.cwd()) == []
    assert b32.status in {"verified", "pending_integration"}


def test_environment_hygiene_and_zero_foreign_imports() -> None:
    """Verify zero parent repo paths, zero Langflow, and zero lfx dependencies."""
    audit = ReleaseDiagnostics.audit_environment()
    assert audit.passed is True
    assert len(audit.foreign_paths_found) == 0
    assert len(audit.unauthorized_modules_found) == 0


def test_cold_import_performance_target() -> None:
    """Core package cold import latency satisfies engineering target."""
    metrics = ReleaseDiagnostics.measure_cold_import()
    assert metrics.cold_import_ms < 500.0, f"Cold import took {metrics.cold_import_ms:.2f}ms"
    assert metrics.resident_memory_mb < 128.0


def test_workflow_migrator_legacy_schema_evolution() -> None:
    """Older linear 'steps' and 'module_name' fixtures migrate to current WorkflowSpec."""
    legacy_payload: dict[str, Any] = {
        "name": "LegacyOrderFlow",
        "steps": [
            {
                "id": "step_lookup",
                "module_name": "order_lookup",
                "approval_required": False,
            },
            {
                "id": "step_approve",
                "module_name": "manager_gate",
                "approval_required": True,
            },
            {
                "id": "step_notify",
                "module_name": "customer_notification",
            },
        ],
        "metadata": {"legacy_version": "0.1.0"},
    }

    migrator = WorkflowMigrator()
    migrated_spec = migrator.migrate_spec_dict(legacy_payload)

    assert isinstance(migrated_spec, WorkflowSpec)
    assert migrated_spec.name == "LegacyOrderFlow"
    assert migrated_spec.entry_node == "step_lookup"
    assert len(migrated_spec.nodes) == 3

    assert migrated_spec.nodes["step_lookup"].handler == "order_lookup"
    assert migrated_spec.nodes["step_lookup"].default_transition == "step_approve"
    assert migrated_spec.nodes["step_approve"].requires_approval is True
    assert migrated_spec.nodes["step_approve"].default_transition == "step_notify"
    assert migrated_spec.terminal_nodes == ["step_notify"]


@pytest.mark.asyncio
async def test_backup_and_restore_recovers_approvals_and_artifacts() -> None:
    """Backup/restore archive recovers durable approval checkpoints and immutable artifacts."""
    with tempfile.TemporaryDirectory(prefix="nexura_b32_backup_test_") as temp_dir_str:
        temp_dir = Path(temp_dir_str)
        source_db = temp_dir / "source.sqlite3"
        source_artifacts = temp_dir / "source_artifacts"
        archive_path = temp_dir / "backup.tar.gz"

        target_db = temp_dir / "restored.sqlite3"
        target_artifacts = temp_dir / "restored_artifacts"

        # 1. Create a workflow run with approval gate in source DB
        spec = WorkflowSpec(
            name="BackupApprovalFlow",
            entry_node="gate",
            nodes={
                "gate": WorkflowNode(
                    id="gate",
                    handler="gate_handler",
                    requires_approval=True,
                )
            },
            terminal_nodes=["gate"],
        )
        registry = ModuleRegistry()
        registry.register("gate_handler", lambda state: {"gated": True})

        store = SQLiteWorkflowStore(source_db)
        engine = WorkflowEngine(spec=spec, registry=registry, store=store)
        run_res = await engine.run(initial_state={"order_id": "ORD-BK-001"})
        assert run_res.status == "paused"
        assert run_res.pending_approval is not None
        approval_id = run_res.pending_approval.approval_id
        await store.close()

        # 2. Store an artifact in source artifact store
        artifact_store = LocalArtifactStore(source_artifacts)
        blob_bytes = b"CONFIDENTIAL_AUDIT_REPORT_CONTENT_SHA256"
        ref = await artifact_store.put(
            workspace_id="ws_audit",
            artifact_id="art_001",
            data=blob_bytes,
            mime_type="text/plain",
        )
        assert ref.artifact_id == "art_001"

        # 3. Create point-in-time backup
        backup_engine = BackupEngine()
        manifest = backup_engine.create_backup(
            db_path=source_db,
            artifact_dir=source_artifacts,
            target_archive_path=archive_path,
            metadata={"env": "testing", "reason": "pre_migration"},
        )
        assert archive_path.exists()
        assert manifest.artifact_count >= 1

        # 4. Restore into completely fresh locations
        restored_manifest = backup_engine.restore_backup(
            archive_path=archive_path,
            target_db_path=target_db,
            target_artifact_dir=target_artifacts,
        )
        assert restored_manifest.database_hash == manifest.database_hash

        # 5. Verify restored database can claim approval
        restored_store = SQLiteWorkflowStore(target_db)
        claimed = await restored_store.claim_approval(run_res.workflow_run_id, approval_id)
        assert claimed is True
        await restored_store.close()

        # 6. Verify restored artifact
        restored_artifact_store = LocalArtifactStore(target_artifacts)
        restored_data = await restored_artifact_store.get("ws_audit", "art_001")
        assert restored_data == blob_bytes


def test_backup_integrity_tamper_detection() -> None:
    """Tampering with an archive file raises BackupIntegrityError during restore."""
    with tempfile.TemporaryDirectory(prefix="nexura_b32_tamper_test_") as temp_dir_str:
        temp_dir = Path(temp_dir_str)
        source_db = temp_dir / "source.sqlite3"
        archive_path = temp_dir / "tampered.tar.gz"

        backup_engine = BackupEngine()
        backup_engine.create_backup(
            db_path=source_db,
            target_archive_path=archive_path,
        )

        # Corrupt archive bytes
        data = bytearray(archive_path.read_bytes())
        data[len(data) // 2] ^= 0xFF
        archive_path.write_bytes(bytes(data))

        with pytest.raises(BackupIntegrityError):
            backup_engine.restore_backup(
                archive_path=archive_path,
                target_db_path=temp_dir / "restored.sqlite3",
            )


def test_health_and_readiness_probes() -> None:
    """HealthChecker accurately diagnoses liveness and storage readiness."""
    with tempfile.TemporaryDirectory(prefix="nexura_b32_health_test_") as temp_dir_str:
        temp_dir = Path(temp_dir_str)
        db_path = temp_dir / "health.sqlite3"
        artifact_dir = temp_dir / "artifacts"

        checker = HealthChecker(db_path=db_path, artifact_dir=artifact_dir)

        liveness = checker.check_liveness()
        assert liveness.state == "healthy"

        readiness = checker.assess_readiness()
        assert readiness.is_healthy is True
        assert len(readiness.probes) == 3


@pytest.mark.asyncio
async def test_two_worker_contention_and_fencing() -> None:
    """Two concurrent workers contending for execution leases enforce fencing safety."""
    with tempfile.TemporaryDirectory(prefix="nexura_b32_worker_test_") as temp_dir_str:
        temp_dir = Path(temp_dir_str)
        db_path = temp_dir / "contention.sqlite3"

        repo = SQLiteRepository(db_path)
        spec = DurableWorkflowSpec(
            workflow_id="wf_contention",
            entry_node="process",
            nodes={"process": DurableNodeSpec(node_id="process", handler="echo")},
            terminal_nodes=["process"],
        )

        engine = DurableExecutionEngine(repository=repo)
        engine.register_handler(
            "echo", lambda state, *args, **kwargs: {"processed_by": "worker_alpha"}
        )

        # Initialize run
        run_id = "run_contention_001"
        state = await engine.init_run(workspace_id="ws_prod", run_id=run_id, spec=spec)
        assert state.status == "pending"

        # Worker alpha claims run -> acquires lease with fencing_token = 1
        claimed_alpha = await engine.claim_run(
            workspace_id="ws_prod",
            run_id=run_id,
            worker_id="worker_alpha",
            lease_duration=30.0,
        )
        assert claimed_alpha.fencing_token == 1
        assert claimed_alpha.worker_id == "worker_alpha"

        # Worker beta attempts to claim while alpha's lease is active -> FencingConflictError
        with pytest.raises(FencingConflictError):
            await engine.claim_run(
                workspace_id="ws_prod",
                run_id=run_id,
                worker_id="worker_beta",
                lease_duration=30.0,
            )

        # Worker alpha executes step successfully
        step_res = await engine.execute_step(
            workspace_id="ws_prod",
            run_id=run_id,
            worker_id="worker_alpha",
            spec=spec,
        )
        assert step_res.state["processed_by"] == "worker_alpha"
        assert "process" in step_res.completed_nodes


def test_wheel_build_and_clean_environment_install() -> None:
    """Fresh external install of wheel in a clean venv with PYTHONPATH unset succeeds."""
    dist_dir = Path("dist").resolve()
    # The wheel of the version being released (dist/ may also hold older builds), installed
    # with dist/ as a package source so its exact nexura-agents pin resolves offline.
    from nexura_runtime import __version__

    wheel_path = dist_dir / f"nexura_runtime-{__version__}-py3-none-any.whl"
    assert wheel_path.is_file(), f"Build it first: uv build --all-packages ({wheel_path.name})"
    assert (dist_dir / f"nexura_agents-{__version__}-py3-none-any.whl").is_file()

    with tempfile.TemporaryDirectory(prefix="nexura_clean_install_test_") as temp_dir_str:
        venv_dir = Path(temp_dir_str) / "clean_venv"
        # 1. Create clean virtual environment using uv
        subprocess.run(
            ["uv", "venv", str(venv_dir)],
            check=True,
            capture_output=True,
        )

        venv_python = venv_dir / "bin" / "python"

        # 2. Install wheel with dependencies into fresh environment
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)

        res_install = subprocess.run(
            [
                "uv",
                "pip",
                "install",
                str(wheel_path),
                "--find-links",
                str(dist_dir),
                # Same version, new contents: never reuse a wheel cached from an earlier build.
                "--refresh-package",
                "nexura-agents",
                "--refresh-package",
                "nexura-runtime",
                "--python",
                str(venv_python),
            ],
            env=env,
            capture_output=True,
            text=True,
        )
        assert res_install.returncode == 0, f"uv pip install failed:\n{res_install.stderr}"

        # 3. Test clean execution inside fresh environment
        verify_code = (
            "import sys\n"
            "import nexura_runtime\n"
            "from nexura_runtime.models.fake import FakeModel\n"
            "from nexura_runtime.agent import Agent\n"
            "from nexura_runtime.runner import Runner\n"
            "\n"
            "model = FakeModel(responses=['Clean external install verified'])\n"
            "agent = Agent(name='TestAgent', instructions='Helpful test agent', model=model)\n"
            "runner = Runner()\n"
            "res = runner.run(agent, 'Ping')\n"
            "assert res.final_output == 'Clean external install verified'\n"
            "print('EXTERNAL_INSTALL_SUCCESS')\n"
        )

        res_run = subprocess.run(
            [str(venv_python), "-c", verify_code],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert res_run.returncode == 0, f"External run failed:\n{res_run.stderr}"
        assert "EXTERNAL_INSTALL_SUCCESS" in res_run.stdout


def test_hygiene_audit_flags_real_langflow_packages_but_not_our_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import types

    import nexura_runtime.integrations.langflow  # noqa: F401  (our own format reader)

    assert ReleaseDiagnostics.audit_environment().unauthorized_modules_found == []
    monkeypatch.setitem(sys.modules, "lfx.graph", types.ModuleType("lfx.graph"))
    monkeypatch.setitem(sys.modules, "langflow_base", types.ModuleType("langflow_base"))
    found = ReleaseDiagnostics.audit_environment().unauthorized_modules_found
    assert sorted(found) == ["langflow_base", "lfx.graph"]


def test_hygiene_audit_ignores_own_test_dirs_but_flags_outside_langflow_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pytest adds tests/integrations/langflow to sys.path in any checkout, whatever the
    checkout directory is called (the public CI clones into .../nexura/nexura)."""
    own = Path(__file__).resolve().parents[2] / "integrations" / "langflow"
    outside = tmp_path / "site" / "langflow"
    monkeypatch.syspath_prepend(str(own))
    monkeypatch.syspath_prepend(str(outside))
    found = ReleaseDiagnostics.audit_environment().foreign_paths_found
    assert str(outside) in found
    assert str(own) not in found


def test_capability_matrix_validates_in_the_public_tree(tmp_path: Path) -> None:
    """The public export has no planning catalog or reports; test evidence is still required."""
    from nexura_runtime.release.matrix import MODULE_REGISTRY, validate_matrix

    for entry in MODULE_REGISTRY:
        for evidence in (entry.test_path, entry.report_path):
            target = tmp_path / evidence
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch()
    assert validate_matrix(tmp_path) == []
    # Removing one test path is still caught without the internal catalog.
    (tmp_path / MODULE_REGISTRY[0].test_path).unlink()
    assert validate_matrix(tmp_path) == [
        f"Missing evidence: {MODULE_REGISTRY[0].code}: {MODULE_REGISTRY[0].test_path}"
    ]
