"""Integration tests for evaluations, benchmarks, schedules, signed webhooks, and channel outbox."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.triggers.store import SqliteTriggerStore


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> str:
    return str(tmp_path / "test_eval_triggers.sqlite3")


@pytest.fixture
def app(tmp_db_path: str):
    return create_api_app(db_path=tmp_db_path, profile="local-owner")


TRIGGER_TARGETS = (
    "wf_daily_summary",
    "wf_health_sync",
    "wf_test",
    "wf_order_triage",
    "wf_restart",
)


async def _seed_workflows(c: AsyncClient) -> None:
    """Triggers must target existing workflows in the caller workspace."""
    spec = {
        "name": "T",
        "entry_node": "a",
        "nodes": {"a": {"id": "a", "handler": "synthetic_order_lookup"}},
        "terminal_nodes": ["a"],
    }
    for wid in TRIGGER_TARGETS:
        r = await c.post("/api/v1/workflows", json={"workflow_id": wid, "spec": spec})
        assert r.status_code in (200, 201), r.text


@pytest.fixture
async def client(app):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers={"X-Workspace-ID": "default"},
    ) as c:
        await _seed_workflows(c)
        yield c


@pytest.mark.asyncio
async def test_evaluation_datasets_and_run(client: AsyncClient):
    # 1. Verify built-in benchmark dataset is present
    res = await client.get("/api/v1/evaluations/datasets")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] >= 1
    builtin_ids = [d["dataset_id"] for d in data["datasets"]]
    assert "benchmark_customer_support" in builtin_ids

    # 2. Upload a custom dataset
    custom_dataset_payload = {
        "dataset_id": "test_qa_eval",
        "version": "1.0.0",
        "description": "Custom QA evaluation dataset",
        "examples": [
            {
                "example_id": "ex_01",
                "input_data": {"query": "What is Nexura AgentKit?"},
                "expected_output": "A standalone framework for autonomous AI agents.",
                "tags": ["unit", "qa"],
                "metadata": {"category": "intro"},
            },
            {
                "example_id": "ex_02",
                "input_data": {"query": "Does Nexura support offline evaluations?"},
                "expected_output": "Yes, evaluations run with deterministic local models.",
                "tags": ["unit", "features"],
            },
        ],
    }
    create_res = await client.post("/api/v1/evaluations/datasets", json=custom_dataset_payload)
    assert create_res.status_code == 200
    assert create_res.json()["dataset_id"] == "test_qa_eval"
    assert create_res.json()["example_count"] == 2

    # 3. Retrieve uploaded dataset
    get_res = await client.get("/api/v1/evaluations/datasets/test_qa_eval")
    assert get_res.status_code == 200
    assert len(get_res.json()["examples"]) == 2

    # 4. Execute deterministic evaluation run on built-in support benchmark
    run_res = await client.post(
        "/api/v1/evaluations/run",
        json={
            "dataset_id": "benchmark_customer_support",
            "graders": ["exact_match", "substring"],
        },
    )
    assert run_res.status_code == 200
    run_data = run_res.json()
    assert run_data["dataset_id"] == "benchmark_customer_support"
    assert run_data["pass_rate"] == 1.0
    assert run_data["mean_score"] == 1.0
    assert run_data["total_examples"] == 3
    assert run_data["passed_examples"] == 3
    run_id_a = run_data["run_id"]

    # 5. Execute second run
    run_res_2 = await client.post(
        "/api/v1/evaluations/run",
        json={
            "dataset_id": "benchmark_customer_support",
            "graders": ["exact_match"],
        },
    )
    assert run_res_2.status_code == 200
    run_id_b = run_res_2.json()["run_id"]

    # 6. List and get run details
    list_runs_res = await client.get("/api/v1/evaluations/runs")
    assert list_runs_res.status_code == 200
    assert list_runs_res.json()["total"] >= 2

    run_detail_res = await client.get(f"/api/v1/evaluations/runs/{run_id_a}")
    assert run_detail_res.status_code == 200
    assert len(run_detail_res.json()["results"]) == 3

    # 7. Compare runs
    compare_res = await client.post(
        "/api/v1/evaluations/compare",
        json={"run_id_a": run_id_a, "run_id_b": run_id_b},
    )
    assert compare_res.status_code == 200
    comp_data = compare_res.json()
    assert comp_data["baseline_run_id"] == run_id_a
    assert comp_data["candidate_run_id"] == run_id_b
    assert comp_data["delta_pass_rate"] == 0.0

    # 8. Delete custom dataset
    del_res = await client.delete("/api/v1/evaluations/datasets/test_qa_eval")
    assert del_res.status_code == 200
    assert del_res.json()["deleted"] is True


@pytest.mark.asyncio
async def test_evaluation_negative_cases(client: AsyncClient):
    # 1. Path traversal in dataset ID
    bad_id_res = await client.post(
        "/api/v1/evaluations/datasets",
        json={
            "dataset_id": "../../etc/passwd",
            "examples": [{"input_data": {"q": "test"}}],
        },
    )
    assert bad_id_res.status_code in (400, 422)

    # 2. Empty examples array
    empty_ex_res = await client.post(
        "/api/v1/evaluations/datasets",
        json={
            "dataset_id": "empty_set",
            "examples": [],
        },
    )
    assert empty_ex_res.status_code == 422

    # 3. Missing input_data in example
    invalid_ex_res = await client.post(
        "/api/v1/evaluations/datasets",
        json={
            "dataset_id": "invalid_set",
            "examples": [{"expected_output": "Missing input data"}],
        },
    )
    assert invalid_ex_res.status_code == 422
    assert "field_errors" in invalid_ex_res.json()["detail"]

    # 4. Attempting to delete built-in benchmark dataset fails closed
    del_builtin_res = await client.delete("/api/v1/evaluations/datasets/benchmark_customer_support")
    assert del_builtin_res.status_code == 400


@pytest.mark.asyncio
async def test_schedules_lifecycle_and_timezones(client: AsyncClient):
    # 1. Register schedule in Asia/Kolkata
    kolkata_res = await client.post(
        "/api/v1/triggers/schedules",
        json={
            "schedule_id": "sched_kolkata_daily",
            "workflow_id": "wf_daily_summary",
            "cron_expr": "0 9 * * 1-5",
            "timezone": "Asia/Kolkata",
            "missed_run_policy": "fire_once",
        },
    )
    assert kolkata_res.status_code == 200
    s_kolkata = kolkata_res.json()["schedule"]
    assert s_kolkata["timezone"] == "Asia/Kolkata"
    assert s_kolkata["is_active"] is True
    assert s_kolkata["next_run_at"] > 0

    # 2. Register schedule in America/New_York
    ny_res = await client.post(
        "/api/v1/triggers/schedules",
        json={
            "schedule_id": "sched_ny_hourly",
            "workflow_id": "wf_health_sync",
            "interval_seconds": 3600.0,
            "timezone": "America/New_York",
            "missed_run_policy": "skip",
        },
    )
    assert ny_res.status_code == 200

    # 3. List schedules
    list_res = await client.get("/api/v1/triggers/schedules")
    assert list_res.status_code == 200
    assert list_res.json()["total"] >= 2

    # 4. Pause and resume
    pause_res = await client.post("/api/v1/triggers/schedules/sched_kolkata_daily/pause")
    assert pause_res.status_code == 200
    assert pause_res.json()["schedule"]["is_active"] is False

    resume_res = await client.post("/api/v1/triggers/schedules/sched_kolkata_daily/resume")
    assert resume_res.status_code == 200
    assert resume_res.json()["schedule"]["is_active"] is True

    # 5. Delete schedule
    del_res = await client.delete("/api/v1/triggers/schedules/sched_ny_hourly")
    assert del_res.status_code == 200
    assert del_res.json()["deleted"] is True


@pytest.mark.asyncio
async def test_schedules_negative_cases(client: AsyncClient):
    # 1. Invalid timezone
    bad_tz_res = await client.post(
        "/api/v1/triggers/schedules",
        json={
            "schedule_id": "sched_bad_tz",
            "workflow_id": "wf_test",
            "cron_expr": "0 9 * * *",
            "timezone": "Mars/Phobos",
        },
    )
    assert bad_tz_res.status_code == 400
    assert bad_tz_res.json()["detail"]["code"] == "INVALID_TIMEZONE"

    # 2. Invalid cron expression
    bad_cron_res = await client.post(
        "/api/v1/triggers/schedules",
        json={
            "schedule_id": "sched_bad_cron",
            "workflow_id": "wf_test",
            "cron_expr": "not a valid cron pattern",
            "timezone": "UTC",
        },
    )
    assert bad_cron_res.status_code == 400
    assert bad_cron_res.json()["detail"]["code"] == "INVALID_CRON"


@pytest.mark.asyncio
async def test_signed_webhooks_and_deduplication(client: AsyncClient):
    # 1. Register webhook endpoint
    reg_res = await client.post(
        "/api/v1/triggers/webhooks",
        json={
            "endpoint_id": "orders_ingress",
            "workflow_id": "wf_order_triage",
            "replay_window_seconds": 300,
        },
    )
    assert reg_res.status_code == 200
    endpoint_data = reg_res.json()
    assert endpoint_data["endpoint_id"] == "orders_ingress"
    secret = endpoint_data["secret"]
    assert len(secret) >= 32

    # 2. Deliver valid signed test event
    test_evt_res = await client.post(
        "/api/v1/triggers/webhooks/orders_ingress/test",
        json={
            "event_id": "evt_order_1001",
            "payload": {"order_id": "ord_998877", "amount": 149.99},
        },
    )
    assert test_evt_res.status_code == 200
    assert test_evt_res.json()["success"] is True
    assert test_evt_res.json()["is_duplicate"] is False
    assert test_evt_res.json()["job_id"].startswith("job_wh_")

    # 3. Replay exact duplicate event -> enqueues once, returns is_duplicate: True
    replay_res = await client.post(
        "/api/v1/triggers/webhooks/orders_ingress/test",
        json={
            "event_id": "evt_order_1001",
            "payload": {"order_id": "ord_998877", "amount": 149.99},
        },
    )
    assert replay_res.status_code == 200
    assert replay_res.json()["is_duplicate"] is True

    # 4. Check trigger jobs list
    jobs_res = await client.get("/api/v1/triggers/jobs")
    assert jobs_res.status_code == 200
    # Exactly one job was enqueued for evt_order_1001
    matching_jobs = [
        j for j in jobs_res.json()["jobs"] if j["trigger_source_id"] == "orders_ingress"
    ]
    assert len(matching_jobs) == 1

    # 5. Negative: invalid signature
    bad_sig_res = await client.post(
        "/api/v1/triggers/webhooks/orders_ingress/test",
        json={
            "event_id": "evt_order_forged",
            "invalid_signature": True,
        },
    )
    assert bad_sig_res.status_code == 401
    assert bad_sig_res.json()["detail"]["code"] == "INVALID_SIGNATURE"

    # 6. Negative: expired timestamp
    expired_res = await client.post(
        "/api/v1/triggers/webhooks/orders_ingress/test",
        json={
            "event_id": "evt_order_expired",
            "expired_timestamp": True,
        },
    )
    assert expired_res.status_code == 400
    assert expired_res.json()["detail"]["code"] == "REPLAY_EXPIRED"


@pytest.mark.asyncio
async def test_dry_run_outbox_guarantees(client: AsyncClient):
    # 1. Dispatch email to observable dry-run outbox
    email_res = await client.post(
        "/api/v1/triggers/outbox/test-dispatch",
        json={
            "channel": "email",
            "recipient": "customer@example.com",
            "subject_or_template": "Order Confirmation #998877",
            "body": "Thank you for your order. Tracking link will follow.",
            "dedup_key": "dedup_email_998877",
        },
    )
    assert email_res.status_code == 200
    assert email_res.json()["dry_run"] is True
    assert email_res.json()["is_duplicate"] is False
    assert email_res.json()["receipt"]["status"] == "dry_run"

    # 2. Dispatch WhatsApp notification to outbox
    wa_res = await client.post(
        "/api/v1/triggers/outbox/test-dispatch",
        json={
            "channel": "whatsapp",
            "recipient": "+919876543210",
            "subject_or_template": "order_update_template",
            "body": "Your order #998877 is confirmed.",
            "dedup_key": "dedup_wa_998877",
        },
    )
    assert wa_res.status_code == 200
    assert wa_res.json()["receipt"]["channel"] == "whatsapp"

    # 3. Duplicate dispatch using same dedup_key
    dup_res = await client.post(
        "/api/v1/triggers/outbox/test-dispatch",
        json={
            "channel": "email",
            "recipient": "customer@example.com",
            "subject_or_template": "Order Confirmation #998877",
            "body": "Duplicate retry message.",
            "dedup_key": "dedup_email_998877",
        },
    )
    assert dup_res.status_code == 200
    assert dup_res.json()["is_duplicate"] is True
    assert dup_res.json()["receipt"]["status"] == "deduplicated"

    # 4. Verify outbox listing
    outbox_res = await client.get("/api/v1/triggers/outbox")
    assert outbox_res.status_code == 200
    assert outbox_res.json()["total"] >= 3
    assert "DRY_RUN_ONLY" in outbox_res.json()["guarantee"]


@pytest.mark.asyncio
async def test_scheduler_restart_reconciliation(tmp_db_path: str):
    # 1. Start initial app instance and schedule a job in the past
    app1 = create_api_app(db_path=tmp_db_path, profile="local-owner")
    async with AsyncClient(transport=ASGITransport(app=app1), base_url="http://testserver") as c1:
        await _seed_workflows(c1)
        res = await c1.post(
            "/api/v1/triggers/schedules",
            json={
                "schedule_id": "sched_restart_test",
                "workflow_id": "wf_restart",
                "interval_seconds": 60.0,
                "timezone": "UTC",
                "missed_run_policy": "fire_once",
            },
        )
        assert res.status_code == 200

    # Simulate that schedule fire was missed during service downtime
    # By modifying next_run_at to past timestamp
    sqlite_store = SqliteTriggerStore(tmp_db_path)
    sched = sqlite_store.get_schedule("sched_restart_test")
    assert sched is not None
    past_time = time.time() - 300  # 5 minutes in the past
    sqlite_store.update_schedule_next_fire("sched_restart_test", past_time)

    # 2. Restart app with the exact same database
    app2 = create_api_app(db_path=tmp_db_path, profile="local-owner")
    # Trigger reconcile on restarted app
    async with AsyncClient(transport=ASGITransport(app=app2), base_url="http://testserver") as c2:
        jobs_res = await c2.get("/api/v1/triggers/jobs")
        assert jobs_res.status_code == 200
        # Reconcile enqueued exactly one job for the missed run per "fire_once" policy
        sched_jobs = [
            j for j in jobs_res.json()["jobs"] if j["trigger_source_id"] == "sched_restart_test"
        ]
        assert len(sched_jobs) == 1
        assert sched_jobs[0]["trigger_type"] == "schedule"
