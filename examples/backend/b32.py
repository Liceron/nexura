"""Example B32: Packaging, Deployment, and Backend Release Gate.

Demonstrates:
1. Environment hygiene & zero foreign repo dependency audit.
2. Multilingual support flow (Hindi/English) with local FakeModel agent turn.
3. Durable workflow execution with Human-in-the-Loop approval gate.
4. Immutable artifact storage and retrieval.
5. Consistent point-in-time backup and restore recovering approval state and artifacts.
6. Subsystem health and readiness diagnostics.
7. Capability & verification matrix inventory across all 33 modules (B00-B32).
8. Keyless execution with zero external network requests or paid API credits.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

from nexura_runtime.agent import Agent
from nexura_runtime.models.fake import FakeModel
from nexura_runtime.runner import Runner

from nexura_runtime.artifacts import LocalArtifactStore
from nexura_runtime.release import (
    BackupEngine,
    HealthChecker,
    ReleaseDiagnostics,
    get_matrix,
)
from nexura_runtime.workflow import (
    ModuleRegistry,
    SQLiteWorkflowStore,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
)


def build_release_workflow() -> WorkflowSpec:
    return WorkflowSpec(
        name="MultilingualCustomerApprovalFlow",
        entry_node="order_lookup",
        nodes={
            "order_lookup": WorkflowNode(
                id="order_lookup",
                handler="order_lookup",
                default_transition="supervisor_gate",
            ),
            "supervisor_gate": WorkflowNode(
                id="supervisor_gate",
                handler="supervisor_gate",
                requires_approval=True,
                default_transition="dispatch_effect",
            ),
            "dispatch_effect": WorkflowNode(
                id="dispatch_effect",
                handler="dispatch_effect",
            ),
        },
        terminal_nodes=["dispatch_effect"],
    )


async def async_main() -> None:
    print("=" * 75)
    print("Nexura AgentKit - B32 Packaging & Final Backend Release Gate Demo")
    print("=" * 75)

    # 1. Environment Hygiene & Diagnostic Audit
    print("\n[1] Auditing Environment Hygiene & Cold Import Performance...")
    audit = ReleaseDiagnostics.audit_environment()
    assert audit.passed is True, f"Environment hygiene failed: {audit}"
    print("    Environment purity: PASSED (Zero parent repo or legacy framework paths)")

    bench = ReleaseDiagnostics.measure_cold_import()
    print(
        f"    Cold import latency: {bench.cold_import_ms:.2f}ms | "
        f"Resident RSS: {bench.resident_memory_mb:.2f}MB"
    )
    print(f"    Python interpreter: {bench.interpreter} on {bench.platform}")

    with tempfile.TemporaryDirectory(prefix="nexura_b32_demo_") as temp_dir_str:
        temp_dir = Path(temp_dir_str)
        source_db = temp_dir / "primary.sqlite3"
        source_artifacts = temp_dir / "primary_artifacts"
        backup_archive = temp_dir / "release_snapshot.tar.gz"

        restored_db = temp_dir / "restored.sqlite3"
        restored_artifacts = temp_dir / "restored_artifacts"

        # 2. Multilingual Agent Turn (Hindi & English)
        print("\n[2] Executing Multilingual Support Agent (Hindi / English)...")
        hindi_prompt = (
            "नमस्ते, मुझे ऑर्डर #ORD-MUMBAI-501 के लिए रिफंड अनुरोध दर्ज करना है। "
            "(Hello, I need to file a refund request for order #ORD-MUMBAI-501.)"
        )
        fake_response = (
            "नमस्ते! आपका अनुरोध प्राप्त हुआ है। ऑर्डर #ORD-MUMBAI-501 के लिए 4500 रुपये का "
            "रिफंड अनुरोध सत्यापित कर लिया गया है। पर्यवेक्षक की स्वीकृति लंबित है। "
            "(Refund request of INR 4500 verified; awaiting supervisor authorization.)"
        )
        model = FakeModel(responses=[fake_response])
        agent = Agent(
            name="MultilingualSupportAgent",
            instructions="Provide respectful, accurate customer support in Hindi and English.",
            model=model,
        )
        runner = Runner()
        agent_res = await runner.run_async(agent, hindi_prompt)
        print(f"    Customer Prompt:\n      {hindi_prompt}")
        print(f"    Agent Response:\n      {agent_res.final_output}")

        # 3. Durable Workflow Execution with Approval Gate
        print("\n[3] Launching Durable Workflow Execution with Approval Gate...")
        spec = build_release_workflow()
        registry = ModuleRegistry()

        def order_lookup(state: dict[str, Any]) -> dict[str, Any]:
            return {
                "order_id": state.get("order_id", "ORD-MUMBAI-501"),
                "customer": "Amit Verma (अमित वर्मा)",
                "amount": 4500,
                "verified": True,
            }

        def supervisor_gate(state: dict[str, Any]) -> dict[str, Any]:
            return {"authorized": True}

        def dispatch_effect(state: dict[str, Any]) -> dict[str, Any]:
            return {
                "ticket_id": "TICK-2026-IND-909",
                "refund_status": "ISSUED",
                "notes": "Synthetic refund effect dispatched successfully",
            }

        registry.register("order_lookup", order_lookup)
        registry.register("supervisor_gate", supervisor_gate)
        registry.register("dispatch_effect", dispatch_effect)

        store = SQLiteWorkflowStore(source_db)
        engine = WorkflowEngine(spec=spec, registry=registry, store=store)

        initial_state = {"order_id": "ORD-MUMBAI-501", "inquiry": hindi_prompt}
        run_res = await engine.run(initial_state=initial_state)
        print(f"    Workflow Run ID: {run_res.workflow_run_id}")
        print(f"    Status: {run_res.status} (gated before approval)")
        assert run_res.status == "paused"
        assert run_res.pending_approval is not None
        approval_id = run_res.pending_approval.approval_id
        print(f"    Pending Approval ID: {approval_id}")

        # 4. Immutable Artifact Storage
        print("\n[4] Writing Immutable Support Transcript Artifact...")
        artifact_store = LocalArtifactStore(source_artifacts)
        transcript_data = (
            f"CUSTOMER: {hindi_prompt}\nAGENT: {agent_res.final_output}\nAMOUNT: 4500 INR\n"
        ).encode()
        ref = await artifact_store.put(
            workspace_id="ws_support",
            artifact_id="transcript_501",
            data=transcript_data,
            mime_type="text/plain",
            metadata={"order_id": "ORD-MUMBAI-501", "lang": "hi-IN"},
        )
        print(f"    Stored Artifact URI: {ref.uri} | Digest: {ref.digest[:16]}...")
        await store.close()

        # 5. Point-in-Time Backup & Restoration
        print("\n[5] Creating Cryptographic Backup Snapshot & Restoring...")
        backup_engine = BackupEngine()
        manifest = backup_engine.create_backup(
            db_path=source_db,
            artifact_dir=source_artifacts,
            target_archive_path=backup_archive,
            metadata={"release_version": "0.3.0", "target": "production_gate"},
        )
        print(
            f"    Snapshot Archive: {backup_archive} (Archive SHA256: "
            f"{manifest.archive_hash[:16]}...)"
        )
        print(
            f"    Archived Artifacts: {manifest.artifact_count} | "
            f"Database Hash: {manifest.database_hash[:16]}..."
        )

        print("    Restoring into fresh environment...")
        restored_manifest = backup_engine.restore_backup(
            archive_path=backup_archive,
            target_db_path=restored_db,
            target_artifact_dir=restored_artifacts,
        )
        assert restored_manifest.database_hash == manifest.database_hash
        print("    Restoration integrity verified.")

        # 6. Resuming Approval on Restored Database
        print("\n[6] Resuming Approval Decision from Restored Database...")
        restored_store = SQLiteWorkflowStore(restored_db)
        restored_engine = WorkflowEngine(spec=spec, registry=registry, store=restored_store)

        resume_res = await restored_engine.resume(
            workflow_run_id=run_res.workflow_run_id,
            approval_id=approval_id,
            decision="approve",
            state_update={"supervisor_notes": "Approved by Regional Director"},
        )
        print(f"    Resumed Run Status: {resume_res.status}")
        # The notes edit the state the gated node reads, so the edited state needs its own
        # approval (an approval covers only what the approver was shown).
        assert resume_res.status == "paused" and resume_res.pending_approval is not None
        resume_res = await restored_engine.resume(
            workflow_run_id=run_res.workflow_run_id,
            approval_id=resume_res.pending_approval.approval_id,
            decision="approve",
        )
        print(f"    Approved the edited state -> status: {resume_res.status}")
        assert resume_res.status == "completed"
        print(f"    Final State: {resume_res.state}")
        await restored_store.close()

        # Verify restored artifact
        restored_artifact_store = LocalArtifactStore(restored_artifacts)
        restored_bytes = await restored_artifact_store.get("ws_support", "transcript_501")
        assert restored_bytes == transcript_data
        print("    Restored artifact verified with exact bit-for-bit integrity.")

        # 7. Subsystem Health & Readiness Probes
        print("\n[7] Assessing Subsystem Health & Readiness Probes...")
        checker = HealthChecker(db_path=restored_db, artifact_dir=restored_artifacts)
        health = checker.assess_readiness()
        print(f"    System Health State: {health.state.upper()}")
        for probe in health.probes:
            status_symbol = "✓" if probe.state == "healthy" else "✗"
            print(
                f"      [{status_symbol}] {probe.name:<18} : "
                f"{probe.state} ({probe.latency_ms:.2f}ms)"
            )
        assert health.is_healthy is True

        # 8. Capability Matrix Inventory
        print("\n[8] Verifying 33-Module Backend Capability Matrix...")
        all_modules = get_matrix()
        print(f"    Backend Work Packages: {len(all_modules)}/33")
        verified_count = sum(1 for m in all_modules if m["status"] == "verified")
        print(f"    Recorded unit/component verification: {verified_count}/33")
        for level in sorted({str(item["status"]) for item in all_modules}):
            print(f"    {level}: {sum(item['status'] == level for item in all_modules)}")
        assert {item["code"] for item in all_modules} == {f"B{i:02d}" for i in range(33)}

    print("\n" + "=" * 75)
    print("Local backup/recovery demo passed. See recorded evidence for release readiness.")
    print("=" * 75)


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
