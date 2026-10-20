"""Unit and acceptance tests for Module B25: Webhooks, Schedules, and Durable Triggers."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest
from nexura_runtime.contracts.context import ExecutionContext

from nexura_runtime.modules.registry import default_registry
from nexura_runtime.service.triggers import (
    InvalidScheduleError,
    InvalidWebhookSignatureError,
    JobClaimConflictError,
    MemoryTriggerStore,
    ReplayWindowExceededError,
    SqliteTriggerStore,
    TriggerManager,
    TriggerServiceModule,
    WebhookEvent,
    calculate_next_cron_fire,
    compute_webhook_signature,
)


@pytest.fixture
def trigger_manager() -> TriggerManager:
    store = MemoryTriggerStore()
    return TriggerManager(store=store)


class TestWebhookSignaturesAndDeduplication:
    """Acceptance: Duplicate webhook delivers one job, invalid signature zero jobs."""

    def test_duplicate_webhook_delivers_one_accepted_job(
        self,
        trigger_manager: TriggerManager,
    ) -> None:
        secret = "super_secret_webhook_key_2026"
        endpoint_id = "ep_orders"
        trigger_manager.register_webhook(
            endpoint_id=endpoint_id,
            workspace_id="ws_retail",
            workflow_id="order_fulfillment",
            release_id="rel_v1_0",
            secret=secret,
        )

        now = time.time()
        payload = {"order_id": "ORD-9912", "amount": 149.99}
        sig = compute_webhook_signature(secret, now, payload)

        event = WebhookEvent(
            event_id="evt_unique_101",
            endpoint_id=endpoint_id,
            payload=payload,
            timestamp=now,
            signature=sig,
        )

        # 1. First delivery: accepted as new job
        is_new_1, job_1 = trigger_manager.handle_webhook(event, current_time=now)
        assert is_new_1 is True
        assert job_1.workflow_id == "order_fulfillment"
        assert job_1.release_id == "rel_v1_0"
        assert job_1.status == "pending"

        # 2. Second delivery (duplicate event_id): idempotent, delivers same job, is_new=False
        is_new_2, job_2 = trigger_manager.handle_webhook(event, current_time=now + 5)
        assert is_new_2 is False
        assert job_2.job_id == job_1.job_id

        # Verify only 1 pending job exists in queue
        pending = trigger_manager.store.list_pending_jobs("ws_retail")
        assert len(pending) == 1
        assert pending[0].job_id == job_1.job_id

    def test_invalid_signature_zero_jobs(
        self,
        trigger_manager: TriggerManager,
    ) -> None:
        secret = "legitimate_secret_key"
        endpoint_id = "ep_payments"
        trigger_manager.register_webhook(
            endpoint_id=endpoint_id,
            workspace_id="ws_finance",
            workflow_id="payment_processor",
            release_id="rel_v2_1",
            secret=secret,
        )

        now = time.time()
        payload = {"tx_id": "tx_malicious_attempt"}
        bad_sig = "bad_forged_hmac_hex_digest_value"

        event = WebhookEvent(
            event_id="evt_forged_sig",
            endpoint_id=endpoint_id,
            payload=payload,
            timestamp=now,
            signature=bad_sig,
        )

        with pytest.raises(InvalidWebhookSignatureError):
            trigger_manager.handle_webhook(event, current_time=now)

        # Assert zero jobs enqueued
        pending = trigger_manager.store.list_pending_jobs("ws_finance")
        assert len(pending) == 0

    def test_replay_window_exceeded_fails(
        self,
        trigger_manager: TriggerManager,
    ) -> None:
        secret = "replay_protection_secret"
        endpoint_id = "ep_telemetry"
        trigger_manager.register_webhook(
            endpoint_id=endpoint_id,
            workspace_id="ws_iot",
            workflow_id="telemetry_pipe",
            release_id="rel_v1",
            secret=secret,
            replay_window_seconds=120,
        )

        old_timestamp = time.time() - 3600  # 1 hour ago
        payload = {"metric": "temp", "val": 42}
        sig = compute_webhook_signature(secret, old_timestamp, payload)

        event = WebhookEvent(
            event_id="evt_replayed_packet",
            endpoint_id=endpoint_id,
            payload=payload,
            timestamp=old_timestamp,
            signature=sig,
        )

        with pytest.raises(ReplayWindowExceededError):
            trigger_manager.handle_webhook(event)

        pending = trigger_manager.store.list_pending_jobs("ws_iot")
        assert len(pending) == 0


class TestSchedulerRestartAndMissedRuns:
    """Acceptance: scheduler restart catches missed triggers per policy on persistent SQLite."""

    def test_scheduler_restart_catches_missed_triggers_per_policy(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            db_file = tf.name

        try:
            # Phase 1: Initialize persistent SQLite trigger store and configure schedules
            store1 = SqliteTriggerStore(db_path=db_file)
            mgr1 = TriggerManager(store=store1)

            t0 = 1770000000.0  # Fixed simulation timestamp

            # Sched A: policy="fire_once"
            mgr1.register_schedule(
                schedule_id="sched_fire_once",
                workspace_id="ws_prod",
                workflow_id="db_cleanup",
                release_id="rel_1_0",
                interval_seconds=60.0,
                missed_run_policy="fire_once",
                start_time=t0,
            )

            # Sched B: policy="skip"
            mgr1.register_schedule(
                schedule_id="sched_skip",
                workspace_id="ws_prod",
                workflow_id="temp_prune",
                release_id="rel_1_0",
                interval_seconds=60.0,
                missed_run_policy="skip",
                start_time=t0,
            )

            # Sched C: policy="fire_all"
            mgr1.register_schedule(
                schedule_id="sched_fire_all",
                workspace_id="ws_prod",
                workflow_id="sync_events",
                release_id="rel_1_0",
                interval_seconds=60.0,
                missed_run_policy="fire_all",
                start_time=t0,
            )

            store1.close()

            # Phase 2: Simulate service restart at t0 + 250s (missed ~4 intervals of 60s)
            t_restart = t0 + 250.0
            store2 = SqliteTriggerStore(db_path=db_file)
            mgr2 = TriggerManager(store=store2)

            # Reconcile missed runs on restart
            catchup_jobs = mgr2.reconcile_missed_runs(current_time=t_restart)

            jobs_by_sched: dict[str, int] = {}
            for j in catchup_jobs:
                jobs_by_sched[j.trigger_source_id] = jobs_by_sched.get(j.trigger_source_id, 0) + 1

            # Sched A (fire_once): exactly 1 catchup job
            assert jobs_by_sched.get("sched_fire_once") == 1

            # Sched B (skip): 0 catchup jobs
            assert jobs_by_sched.get("sched_skip", 0) == 0

            # Sched C (fire_all): exactly 4 catchup jobs (at t0+60, t0+120, t0+180, t0+240)
            assert jobs_by_sched.get("sched_fire_all") == 4

            # Verify next_run_at advanced into the future (> t_restart) for all
            for s_id in ("sched_fire_once", "sched_skip", "sched_fire_all"):
                sched = mgr2.get_schedule(s_id)
                assert sched.next_run_at >= t_restart

            store2.close()

        finally:
            Path(db_file).unlink(missing_ok=True)


class TestAtomicWorkerClaiming:
    """Acceptance: Two scheduler instances cannot double-enqueue or double-claim."""

    def test_two_workers_cannot_double_claim_job(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            db_file = tf.name

        try:
            store = SqliteTriggerStore(db_path=db_file)
            mgr = TriggerManager(store=store)

            # Register webhook endpoint
            mgr.register_webhook(
                endpoint_id="ep_race",
                workspace_id="ws_ops",
                workflow_id="provision_vm",
                release_id="rel_v1",
                secret="race_sec",
            )

            # Enqueue actual job
            now = time.time()
            sig = compute_webhook_signature("race_sec", now, {"node": "worker-1"})
            _, real_job = mgr.handle_webhook(
                WebhookEvent(
                    event_id="evt_race_1",
                    endpoint_id="ep_race",
                    payload={"node": "worker-1"},
                    timestamp=now,
                    signature=sig,
                )
            )

            # Two competing workers attempt to claim the job
            claimed_by_w1 = mgr.claim_job(real_job.job_id, worker_id="worker_alpha")
            assert claimed_by_w1.claimed_by == "worker_alpha"
            assert claimed_by_w1.status == "claimed"

            # Worker 2 attempts to claim the same job -> fails with JobClaimConflictError
            with pytest.raises(JobClaimConflictError):
                mgr.claim_job(real_job.job_id, worker_id="worker_bravo")

            store.close()

        finally:
            Path(db_file).unlink(missing_ok=True)


class TestDeterministicTimezoneTransitions:
    """Acceptance: Timezone transitions deterministic for Asia/Kolkata and DST-observing zones."""

    def test_asia_kolkata_deterministic_schedule(self) -> None:
        # Schedule: Every day at 09:30 AM in Asia/Kolkata (UTC+05:30)
        # Reference: Oct 6, 2026, 08:00 UTC (13:30 IST)
        t_ref = 1791273600.0  # 2026-10-06 08:00:00 UTC
        cron_expr = "30 9 * * *"  # 9:30 AM daily

        next_fire_ts = calculate_next_cron_fire(
            from_timestamp=t_ref,
            cron_expr=cron_expr,
            timezone_name="Asia/Kolkata",
        )

        # 9:30 AM IST = 04:00 AM UTC the next day (2026-10-07 04:00:00 UTC)
        import zoneinfo
        from datetime import datetime

        tz_kolkata = zoneinfo.ZoneInfo("Asia/Kolkata")
        dt_fire = datetime.fromtimestamp(next_fire_ts, tz=tz_kolkata)
        assert dt_fire.hour == 9
        assert dt_fire.minute == 30
        assert dt_fire.tzname() == "IST"

    def test_america_new_york_dst_transition(self) -> None:
        # Nov 1, 2026: Daylight saving time ends in America/New_York
        # (fall-back at 2:00 AM EDT -> 1:00 AM EST)
        # Cron: At 03:00 AM daily
        # Reference: Oct 31, 2026 at 20:00 UTC
        import zoneinfo
        from datetime import datetime

        tz_ny = zoneinfo.ZoneInfo("America/New_York")
        # Reference time on Oct 31, 2026 23:00 EDT
        dt_ref = datetime(2026, 10, 31, 23, 0, tzinfo=tz_ny)
        t_ref = dt_ref.timestamp()

        # Fire at 3:00 AM on Nov 1, 2026 (which happens after the fall-back to EST)
        next_fire_ts = calculate_next_cron_fire(
            from_timestamp=t_ref,
            cron_expr="0 3 * * *",
            timezone_name="America/New_York",
        )

        dt_fire = datetime.fromtimestamp(next_fire_ts, tz=tz_ny)
        assert dt_fire.day == 1
        assert dt_fire.month == 11
        assert dt_fire.hour == 3
        assert dt_fire.minute == 0
        assert dt_fire.tzname() == "EST"  # Successfully in Standard Time after fall-back

    def test_invalid_cron_syntax_rejected(self) -> None:
        with pytest.raises(InvalidScheduleError):
            calculate_next_cron_fire(
                from_timestamp=1770000000.0,
                cron_expr="* * *",  # Not 5 fields
            )


class TestTriggerServiceModuleContracts:
    """Acceptance: B00/B01 module contract invocation and default registry registration."""

    @pytest.mark.asyncio
    async def test_trigger_service_module_invoke(self) -> None:
        mod = TriggerServiceModule()
        ctx = ExecutionContext(run_id="run_trig_1", node_id="node_trig", workspace_id="ws_billing")

        # 1. Register webhook
        reg_res = await mod.invoke(
            ctx,
            inputs={
                "operation": "register_webhook",
                "endpoint_id": "ep_stripe",
                "workflow_id": "charge_customer",
                "release_id": "rel_v3",
                "secret": "stripe_webhook_sec",
            },
        )
        assert reg_res.status == "success"
        assert reg_res.outputs["endpoint_id"] == "ep_stripe"

        # 2. Ingest signed webhook
        now = time.time()
        sig = compute_webhook_signature("stripe_webhook_sec", now, {"event": "invoice.paid"})
        wh_res = await mod.invoke(
            ctx,
            inputs={
                "operation": "handle_webhook",
                "endpoint_id": "ep_stripe",
                "event_id": "evt_stripe_999",
                "payload": {"event": "invoice.paid"},
                "timestamp": now,
                "signature": sig,
            },
        )
        assert wh_res.status == "success"
        assert wh_res.outputs["status"] == "accepted"
        assert wh_res.outputs["is_new"] is True
        job_id = wh_res.outputs["job_id"]

        # 3. Claim job
        claim_res = await mod.invoke(
            ctx,
            inputs={
                "operation": "claim_job",
                "job_id": job_id,
                "worker_id": "worker_billing_01",
            },
        )
        assert claim_res.status == "success"
        assert claim_res.outputs["status"] == "claimed"
        assert claim_res.outputs["claimed_by"] == "worker_billing_01"

    def test_default_registry_contains_trigger_module(self) -> None:
        assert default_registry.has("nexura.service.triggers")
