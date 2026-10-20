"""Standalone demonstration for Module B25: Webhooks, Schedules, and Durable Triggers.

Runs 100% keylessly and offline:
  uv run python examples/backend/b25.py
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from nexura_runtime.contracts.context import ExecutionContext

from nexura_runtime.service.triggers import (
    InvalidWebhookSignatureError,
    JobClaimConflictError,
    SqliteTriggerStore,
    TriggerManager,
    TriggerServiceModule,
    WebhookEvent,
    compute_webhook_signature,
)


def main() -> None:
    print("=== Module B25: Webhooks, Schedules, and Durable Triggers ===")

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_file = tf.name

    try:
        # 1. Initialize Persistent Trigger Store & Manager
        print("\n1. Initializing persistent SQLite trigger store on disk...")
        store1 = SqliteTriggerStore(db_path=db_file)
        mgr1 = TriggerManager(store=store1)
        print(f"  Initialized database at: {db_file}")

        # 2. Register Webhook Endpoint Pinned to Workflow Release
        print("\n2. Registering signed webhook endpoint for order fulfillment...")
        webhook_secret = "whsec_live_order_stream_2026_xyz"
        endpoint_id = "ep_orders"
        wh_cfg = mgr1.register_webhook(
            endpoint_id=endpoint_id,
            workspace_id="ws_ecommerce",
            workflow_id="order_fulfillment",
            release_id="rel_order_v1_0",
            secret=webhook_secret,
            replay_window_seconds=300,
        )
        print(f"  Registered endpoint '{wh_cfg.endpoint_id}' pinned to '{wh_cfg.release_id}'")

        # 3. Webhook Delivery & Deduplication
        print("\n3. Ingesting signed webhook event...")
        now = time.time()
        order_payload = {
            "order_id": "ORD-2026-X1",
            "customer": "Rohan Gupta",
            "total_inr": 4999.0,
            "items": ["smart_gateway_hub", "sensor_node"],
        }
        valid_signature = compute_webhook_signature(webhook_secret, now, order_payload)

        event = WebhookEvent(
            event_id="evt_order_882190",
            endpoint_id=endpoint_id,
            payload=order_payload,
            timestamp=now,
            signature=valid_signature,
        )

        is_new, order_job = mgr1.handle_webhook(event, current_time=now)
        print(f"  Webhook accepted: is_new={is_new}, Job ID={order_job.job_id}")
        assert is_new is True

        # Test deduplication: deliver identical event again
        is_new_dup, dup_job = mgr1.handle_webhook(event, current_time=now + 2)
        print(f"  Duplicate webhook delivery: is_new={is_new_dup}, Job ID={dup_job.job_id}")
        assert is_new_dup is False
        assert dup_job.job_id == order_job.job_id
        print("  [SUCCESS] Idempotent deduplication delivered exactly one accepted job.")

        # Test forged signature rejection
        try:
            mgr1.handle_webhook(
                WebhookEvent(
                    event_id="evt_forged_attempt",
                    endpoint_id=endpoint_id,
                    payload=order_payload,
                    timestamp=now,
                    signature="forged_bad_hmac_hex",
                ),
                current_time=now,
            )
            raise AssertionError("Forged signature must be rejected!")
        except InvalidWebhookSignatureError:
            print("  [SUCCESS] Forged webhook signature was rejected with zero jobs created.")

        # 4. Register Timezone-Aware Schedule
        print("\n4. Registering scheduled synthetic order reconciliation in Asia/Kolkata...")
        t_sim = 1770000000.0  # Simulation timestamp
        sched = mgr1.register_schedule(
            schedule_id="sched_recon_daily",
            workspace_id="ws_ecommerce",
            workflow_id="order_reconciliation",
            release_id="rel_recon_v1_0",
            interval_seconds=60.0,
            timezone="Asia/Kolkata",
            missed_run_policy="fire_once",
            payload={"action": "reconcile_orders"},
            start_time=t_sim,
        )
        print(f"  Schedule '{sched.schedule_id}' registered. Next fire: {sched.next_run_at}")

        # 5. Simulate Service Shutdown & Restart
        print("\n5. Simulating service shutdown and restart with missed triggers...")
        store1.close()
        print("  Service stopped.")

        # Service restarts 300 seconds later (5 missed intervals)
        t_restart = t_sim + 300.0
        store2 = SqliteTriggerStore(db_path=db_file)
        mgr2 = TriggerManager(store=store2)
        print("  Service restarted. Reopening persistent SQLite store...")

        # Reconcile missed runs
        catchup_jobs = mgr2.reconcile_missed_runs(current_time=t_restart)
        print(f"  Catchup jobs enqueued on restart: {len(catchup_jobs)}")
        assert len(catchup_jobs) == 1  # policy='fire_once'
        sched_after = mgr2.get_schedule("sched_recon_daily")
        assert sched_after.next_run_at >= t_restart
        print(f"  Schedule next fire advanced into future: {sched_after.next_run_at}")

        # Verify both webhook job and scheduled job survived in the database
        pending_jobs = store2.list_pending_jobs("ws_ecommerce")
        print(f"  Total pending durable jobs in database: {len(pending_jobs)}")
        assert len(pending_jobs) == 2  # 1 from webhook, 1 from schedule
        print("  [SUCCESS] Webhook job and scheduled catchup job survived restart.")

        # 6. Atomic Competing Worker Job Claims
        print("\n6. Demonstrating atomic worker claims and race prevention...")
        job_to_claim = pending_jobs[0].job_id

        # Worker Alpha claims job
        claimed_alpha = mgr2.claim_job(job_to_claim, worker_id="worker_alpha")
        print(f"  Worker Alpha claimed job '{job_to_claim}': status={claimed_alpha.status}")
        assert claimed_alpha.status == "claimed"

        # Worker Bravo attempts to claim same job -> Conflict!
        try:
            mgr2.claim_job(job_to_claim, worker_id="worker_bravo")
            raise AssertionError("Competing worker must not claim an already-claimed job!")
        except JobClaimConflictError as exc:
            print(f"  [SUCCESS] Worker Bravo claim rejected: {exc}")

        # Complete the claimed job
        store2.complete_job(job_to_claim)
        completed_job = store2.get_job(job_to_claim)
        assert completed_job is not None and completed_job.status == "completed"
        print(f"  Job '{job_to_claim}' completed successfully.")

        # 7. B00/B01 Module Contract Invocation
        print("\n7. Executing TriggerServiceModule via Python contract...")
        module = TriggerServiceModule(manager=mgr2)
        ctx = ExecutionContext(
            run_id="run_b25_demo",
            node_id="trig_node",
            workspace_id="ws_ecommerce",
        )
        mod_res = asyncio.run(
            module.invoke(
                ctx,
                inputs={
                    "operation": "claim_job",
                    "job_id": pending_jobs[1].job_id,
                    "worker_id": "worker_contract",
                },
            )
        )
        print(f"  Module Status: {mod_res.status}, Claimed By: {mod_res.outputs['claimed_by']}")
        assert mod_res.status == "success"

        # 8. B01 CLI Subprocess Invocation
        print("\n8. Invoking TriggerServiceModule via B01 CLI subprocess...")
        cmd = [
            sys.executable,
            "-m",
            "nexura_runtime.modules",
            "run",
            "nexura.service.triggers",
            "--input",
            json.dumps(
                {
                    "operation": "register_webhook",
                    "endpoint_id": "ep_cli_test",
                    "workspace_id": "ws_ecommerce",
                    "workflow_id": "cli_workflow",
                    "release_id": "rel_v1",
                    "secret": "cli_sec",
                }
            ),
        ]
        cli_proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        print(f"  CLI Exit Code: {cli_proc.returncode}")
        print(f"  CLI Output: {cli_proc.stdout.strip()}")
        assert cli_proc.returncode == 0
        cli_json = json.loads(cli_proc.stdout)
        assert cli_json["status"] == "success"

        store2.close()
        print("\n=== All B25 Demonstration Steps Completed Successfully ===")

    finally:
        Path(db_file).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
