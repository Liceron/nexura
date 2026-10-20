"""Security regressions for the triggers REST routes, stores and signature scheme."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from _local_env import local_headers
from fastapi.testclient import TestClient

from nexura_runtime.integrations.channels.models import OutboxReceipt
from nexura_runtime.integrations.channels.outbox import SqliteChannelStore
from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.triggers.errors import InvalidScheduleError, TriggerConflictError
from nexura_runtime.service.triggers.manager import TriggerManager
from nexura_runtime.service.triggers.models import ScheduleConfig, WebhookEvent
from nexura_runtime.service.triggers.schedules import evaluate_missed_runs
from nexura_runtime.service.triggers.signatures import compute_webhook_signature
from nexura_runtime.service.triggers.store import MemoryTriggerStore, SqliteTriggerStore
from nexura_runtime.service.workspaces.repository import WorkspaceRepository

TRIG = "/api/v1/triggers"

_SPEC = {
    "name": "T",
    "entry_node": "a",
    "nodes": {"a": {"id": "a", "handler": "synthetic_order_lookup"}},
    "terminal_nodes": ["a"],
}
# Local workspace -> workflow id (set by _local).
_DEFAULT_WF: dict[str, str] = {}


def _mk_wf(c: TestClient, headers: dict[str, str], *ids: str) -> None:
    """Create workflows (owned by the caller workspace) so triggers can target them."""
    for wid in ids:
        r = c.post("/api/v1/workflows", headers=headers, json={"workflow_id": wid, "spec": _SPEC})
        assert r.status_code in (200, 201), r.text


def _local(tmp_path: Path, extra: tuple[tuple[str, str, str, str], ...] = ()):
    repo = WorkspaceRepository()
    repo.create_workspace("wsA", "A", owner_id="local-owner")
    repo.create_workspace("wsB", "B", owner_id="local-owner")
    keys: dict[str, dict[str, str]] = {}
    for name, ws, _pid, role in [
        ("a", "wsA", "alice", "admin"),
        ("b", "wsB", "bob", "admin"),
        *extra,
    ]:
        keys[name] = local_headers(ws, role)
    app = create_api_app(
        db_path=tmp_path / "s.db",
        workspace_repo=repo,
        profile="local-owner",
    )
    c = TestClient(app)
    _mk_wf(c, keys["a"], "wf", "w", "f")
    _mk_wf(c, keys["b"], "wf_b", "evil")
    for name in keys:
        _DEFAULT_WF[keys[name]["X-Workspace-ID"]] = "wf_b" if name == "b" else "wf"
    return c, keys, app


def _register(c: TestClient, h: dict[str, str], ep: str, **kw: Any):
    body = {"endpoint_id": ep, "workflow_id": _DEFAULT_WF.get(h.get("X-Workspace-ID", ""), "wf")}
    return c.post(f"{TRIG}/webhooks", headers=h, json={**body, **kw})


def _deliver(
    c: TestClient,
    ep: str,
    secret: str,
    event_id: str,
    body: Any,
    *,
    ts: float | None = None,
    sig: str | None = None,
    headers: dict[str, str] | None = None,
):
    ts = time.time() if ts is None else ts
    if sig is None:
        sig = compute_webhook_signature(secret, ts, body, event_id)
    return c.post(
        f"{TRIG}/webhooks/{ep}/deliver",
        json=body,
        headers={
            "X-Webhook-Signature": sig,
            "X-Webhook-Timestamp": str(ts),
            "X-Webhook-Event-ID": event_id,
            **(headers or {}),
        },
    )


# --- findings 0, 5: cross-workspace takeover ---------------------------------------------


def test_webhook_id_cannot_be_taken_over_by_another_workspace(tmp_path: Path) -> None:
    c, k, _ = _local(tmp_path)
    assert _register(c, k["a"], "shared", secret="victim-secret-0123").status_code == 200
    r = _register(c, k["b"], "shared", secret="attacker-secret-01", workflow_id="evil")
    assert r.status_code == 409, r.text
    mine = c.get(f"{TRIG}/webhooks", headers=k["a"]).json()["webhooks"]
    assert [w["endpoint_id"] for w in mine] == ["shared"]
    assert c.get(f"{TRIG}/webhooks", headers=k["b"]).json()["webhooks"] == []
    # Victim's endpoint still verifies with the victim's secret and its own workflow.
    assert (
        _deliver(c, "shared", "victim-secret-0123", "e1", {"x": 1}, headers=k["a"]).status_code
        == 200
    )
    # Same workspace may re-register (update) its own endpoint.
    assert _register(c, k["a"], "shared", secret="rotated-secret-01").status_code == 200


def test_schedule_id_cannot_be_taken_over(tmp_path: Path) -> None:
    c, k, _ = _local(tmp_path)
    body = {"schedule_id": "s1", "workflow_id": "wf", "interval_seconds": 120}
    assert c.post(f"{TRIG}/schedules", headers=k["a"], json=body).status_code == 200
    r = c.post(f"{TRIG}/schedules", headers=k["b"], json={**body, "workflow_id": "evil"})
    assert r.status_code == 409, r.text
    mine = c.get(f"{TRIG}/schedules", headers=k["a"]).json()["schedules"]
    assert [(s["schedule_id"], s["workflow_id"]) for s in mine] == [("s1", "wf")]


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
def test_store_refuses_cross_workspace_overwrite(store_kind: str, tmp_path: Path) -> None:
    store = (
        MemoryTriggerStore()
        if store_kind == "memory"
        else SqliteTriggerStore(str(tmp_path / "t.db"))
    )
    mgr = TriggerManager(store=store)
    mgr.register_webhook(
        endpoint_id="e", workspace_id="w1", workflow_id="a", release_id="r", secret="s1"
    )
    with pytest.raises(TriggerConflictError):
        mgr.register_webhook(
            endpoint_id="e", workspace_id="w2", workflow_id="b", release_id="r", secret="s2"
        )
    got = store.get_webhook_config("e")
    assert got is not None and (got.workspace_id, got.workflow_id, got.secret) == ("w1", "a", "s1")
    mgr.register_schedule(
        schedule_id="s", workspace_id="w1", workflow_id="a", release_id="r", interval_seconds=60
    )
    with pytest.raises(TriggerConflictError):
        mgr.register_schedule(
            schedule_id="s", workspace_id="w2", workflow_id="b", release_id="r", interval_seconds=60
        )


# --- finding 1: signature covers event_id ------------------------------------------------


def test_replay_with_different_event_id_is_rejected(tmp_path: Path) -> None:
    c, k, _ = _local(tmp_path)
    secret = _register(c, k["a"], "ep").json()["secret"]
    body = {"order": 1}
    ts = time.time()
    sig = compute_webhook_signature(secret, ts, body, "evt-1")
    r1 = _deliver(c, "ep", secret, "evt-1", body, ts=ts, sig=sig, headers=k["a"])
    assert r1.status_code == 200 and r1.json()["is_duplicate"] is False
    r2 = _deliver(c, "ep", secret, "attacker-evt-2", body, ts=ts, sig=sig, headers=k["a"])
    assert r2.status_code == 401, r2.text
    r3 = _deliver(c, "ep", secret, "evt-1", body, ts=ts, sig=sig, headers=k["a"])
    assert r3.status_code == 200 and r3.json()["is_duplicate"] is True
    assert len(c.get(f"{TRIG}/jobs", headers=k["a"]).json()["jobs"]) == 1


def test_api_registered_endpoint_rejects_legacy_v1_signature(tmp_path: Path) -> None:
    c, k, _ = _local(tmp_path)
    secret = _register(c, k["a"], "ep").json()["secret"]
    ts = time.time()
    body = {"a": 1}
    v1 = compute_webhook_signature(secret, ts, body)  # no event_id: legacy scheme
    assert _deliver(c, "ep", secret, "e1", body, ts=ts, sig=v1, headers=k["a"]).status_code == 401


def test_manager_level_legacy_signature_only_when_allowed() -> None:
    mgr = TriggerManager(store=MemoryTriggerStore())
    for ep, allow in (("old", True), ("new", False)):
        mgr.register_webhook(
            endpoint_id=ep,
            workspace_id="w",
            workflow_id="wf",
            release_id="r",
            secret="sec",
            allow_legacy_signature=allow,
        )
    now = time.time()
    for ep in ("old", "new"):
        ev = WebhookEvent(
            event_id=f"ev-{ep}",
            endpoint_id=ep,
            payload={"k": 1},
            timestamp=now,
            signature=compute_webhook_signature("sec", now, {"k": 1}),
        )
        if ep == "old":
            assert mgr.handle_webhook(ev)[0] is True
        else:
            from nexura_runtime.service.triggers.errors import InvalidWebhookSignatureError

            with pytest.raises(InvalidWebhookSignatureError):
                mgr.handle_webhook(ev)


# --- finding 2: malformed input is a 4xx, never a 500 ------------------------------------


@pytest.mark.parametrize(
    ("sig", "ts", "body"),
    [
        ("é".encode("latin-1"), "1700000000", b'{"a":1}'),
        (b"abcd", "nan", b'{"a":1}'),
        (b"abcd", "inf", b'{"a":1}'),
        (b"abcd", "1700000000", b"[1,2]"),
        (b"abcd", "1700000000", b"not json"),
    ],
)
def test_malformed_deliver_input_is_4xx(tmp_path: Path, sig: bytes, ts: str, body: bytes) -> None:
    c, k, app = _local(tmp_path)
    _register(c, k["a"], "ep")
    c = TestClient(app, raise_server_exceptions=True)
    r = c.post(
        f"{TRIG}/webhooks/ep/deliver",
        content=body,
        headers={
            **k["a"],
            "content-type": "application/json",
            "X-Webhook-Signature": sig,
            "X-Webhook-Timestamp": ts,
            "X-Webhook-Event-ID": "e1",
        },
    )
    assert 400 <= r.status_code < 500, r.text


def test_non_ascii_signature_with_valid_body_is_401(tmp_path: Path) -> None:
    c, k, _ = _local(tmp_path)
    _register(c, k["a"], "ep")
    r = c.post(
        f"{TRIG}/webhooks/ep/deliver",
        content=b'{"a":1}',
        headers={
            **k["a"],
            "content-type": "application/json",
            "X-Webhook-Signature": "é".encode("latin-1"),
            "X-Webhook-Timestamp": str(time.time()),
            "X-Webhook-Event-ID": "e1",
        },
    )
    assert r.status_code == 401


# --- findings 22, 24: receipts keyed per endpoint ----------------------------------------


def test_event_id_collision_across_tenants_does_not_suppress(tmp_path: Path) -> None:
    c, k, _ = _local(tmp_path)
    sa = _register(c, k["a"], "ep-a").json()["secret"]
    sb = _register(c, k["b"], "ep-b").json()["secret"]
    rb = _deliver(c, "ep-b", sb, "evt-1", {"x": "b"}, headers=k["b"])
    assert rb.status_code == 200 and rb.json()["is_duplicate"] is False
    ra = _deliver(c, "ep-a", sa, "evt-1", {"x": "a"}, headers=k["a"])
    assert ra.status_code == 200 and ra.json()["is_duplicate"] is False
    assert ra.json()["job_id"] != rb.json()["job_id"]
    assert len(c.get(f"{TRIG}/jobs", headers=k["a"]).json()["jobs"]) == 1


def test_test_endpoint_event_id_does_not_burn_other_endpoint(tmp_path: Path) -> None:
    c, k, _ = _local(tmp_path)
    sa = _register(c, k["a"], "ep-a").json()["secret"]
    _register(c, k["b"], "ep-b")
    assert (
        c.post(f"{TRIG}/webhooks/ep-b/test", headers=k["b"], json={"event_id": "evt-1"}).status_code
        == 200
    )
    r = _deliver(c, "ep-a", sa, "evt-1", {"h": 1}, headers=k["a"])
    assert r.status_code == 200 and r.json()["is_duplicate"] is False


def test_legacy_receipts_table_is_migrated_to_endpoint_scoped_key(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE webhook_receipts (event_id TEXT PRIMARY KEY, endpoint_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL, job_id TEXT NOT NULL, received_at REAL NOT NULL);
        INSERT INTO webhook_receipts VALUES ('e1','ep-a','w','j1',1.0);
        CREATE TABLE webhook_configs (endpoint_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL,
            workflow_id TEXT NOT NULL, release_id TEXT NOT NULL, secret TEXT NOT NULL,
            replay_window_seconds INTEGER NOT NULL, is_active INTEGER NOT NULL);
        INSERT INTO webhook_configs VALUES ('ep-a','w','wf','r','PLAINTEXT-LEGACY-SECRET',300,1);
        """
    )
    conn.commit()
    conn.close()
    store = SqliteTriggerStore(str(db))
    cfg = store.get_webhook_config("ep-a")
    assert cfg is not None and cfg.secret == "PLAINTEXT-LEGACY-SECRET"
    assert cfg.allow_legacy_signature is True  # pre-existing endpoints keep working
    with sqlite3.connect(db) as raw:
        assert (
            "PLAINTEXT-LEGACY-SECRET"
            not in raw.execute("SELECT secret FROM webhook_configs").fetchone()[0]
        )
        pk = [r[1] for r in raw.execute("PRAGMA table_info(webhook_receipts)") if r[5]]
    assert sorted(pk) == ["endpoint_id", "event_id"]


# --- finding 29: test endpoint is not a signing oracle -----------------------------------


def test_test_endpoint_ignores_role_headers_and_returns_no_signature(tmp_path: Path) -> None:
    c, k, _ = _local(tmp_path, extra=(("e", "wsA", "ed", "editor"), ("v", "wsA", "vic", "viewer")))
    _register(c, k["a"], "e1", secret="S3CRET-S3CRET-S3CRET")
    for who in ("v", "e"):
        r = c.post(f"{TRIG}/webhooks/e1/test", headers=k[who], json={"payload": {"x": 1}})
        assert r.status_code == 200, (who, r.text)
        assert "signature" not in r.text.lower().replace("invalid_signature", "")
        assert "S3CRET" not in r.text
    r = c.post(f"{TRIG}/webhooks/e1/test", headers=k["a"], json={"payload": {"x": 1}})
    assert r.status_code == 200
    assert "signature" not in r.text.lower().replace("invalid_signature", "")
    assert "S3CRET" not in r.text


# --- finding 30: secrets encrypted at rest -----------------------------------------------


def test_webhook_secret_not_in_database_files(tmp_path: Path) -> None:
    c, k, app = _local(tmp_path)
    secret = "WEBHOOKSECRET-ABCDEF-123456"
    assert _register(c, k["a"], "e1", secret=secret).status_code == 200
    app.state.trigger_store._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # noqa: SLF001
    leaked = [
        p.name for p in tmp_path.iterdir() if p.is_file() and secret.encode() in p.read_bytes()
    ]
    assert leaked == []
    # ...and it still verifies after a reopen of the store (key persisted beside the DB).
    assert _deliver(c, "e1", secret, "ev1", {"a": 1}, headers=k["a"]).status_code == 200
    reopened = SqliteTriggerStore(str(tmp_path / "s.db"))
    cfg = reopened.get_webhook_config("e1")
    assert cfg is not None and cfg.secret == secret
    assert (tmp_path / "s.db.trigger.key").stat().st_mode & 0o077 == 0


def test_secret_never_returned_after_creation(tmp_path: Path) -> None:
    c, k, _ = _local(tmp_path)
    secret = _register(c, k["a"], "e1").json()["secret"]
    assert secret not in c.get(f"{TRIG}/webhooks", headers=k["a"]).text


# --- finding 17: schedule intervals and catch-up loops -----------------------------------


@pytest.mark.parametrize("interval", [1e-9, 0.5, 59.0, -5, 1e12])
def test_api_rejects_out_of_range_intervals(tmp_path: Path, interval: float) -> None:
    c, k, _ = _local(tmp_path)
    r = c.post(
        f"{TRIG}/schedules",
        headers=k["a"],
        json={"schedule_id": "s", "workflow_id": "w", "interval_seconds": interval},
    )
    assert r.status_code == 400, r.text


def test_min_interval_is_configurable(tmp_path: Path) -> None:
    _, k, app = _local(tmp_path)
    app.state.min_schedule_interval_seconds = 1.0
    c = TestClient(app)
    r = c.post(
        f"{TRIG}/schedules",
        headers=k["a"],
        json={"schedule_id": "s", "workflow_id": "w", "interval_seconds": 2},
    )
    assert r.status_code == 200, r.text


def test_manager_rejects_tiny_interval_and_tick_terminates() -> None:
    mgr = TriggerManager(store=MemoryTriggerStore())
    with pytest.raises(InvalidScheduleError):
        mgr.register_schedule(
            schedule_id="s",
            workspace_id="w",
            workflow_id="f",
            release_id="r",
            interval_seconds=1e-9,
        )
    # A pre-existing bad row in the store must not hang the scheduler either.
    mgr.store.save_schedule(
        ScheduleConfig(
            schedule_id="bad",
            workspace_id="w",
            workflow_id="f",
            release_id="r",
            interval_seconds=1e-9,
            next_run_at=time.time() - 10,
        )
    )
    done = threading.Event()

    def run() -> None:
        with contextlib.suppress(InvalidScheduleError):
            mgr.tick_schedules(time.time() + 5)
        done.set()

    threading.Thread(target=run, daemon=True).start()
    assert done.wait(5)


@pytest.mark.parametrize("policy", ["skip", "fire_once"])
def test_catchup_after_long_downtime_is_arithmetic(policy: str) -> None:
    now = 2_000_000_000.0
    sched = ScheduleConfig(
        schedule_id="s",
        workspace_id="w",
        workflow_id="f",
        release_id="r",
        interval_seconds=0.01,
        missed_run_policy=policy,  # type: ignore[arg-type]
        next_run_at=now - 365 * 24 * 3600,
    )
    started = time.monotonic()
    times, nxt = evaluate_missed_runs(sched, now)
    assert time.monotonic() - started < 1.0
    assert nxt >= now and nxt - now <= 0.01 + 1e-6
    assert len(times) == (1 if policy == "fire_once" else 0)


def test_cron_catchup_jumps_to_next_fire() -> None:
    now = 2_000_000_000.0
    sched = ScheduleConfig(
        schedule_id="s",
        workspace_id="w",
        workflow_id="f",
        release_id="r",
        cron_expr="* * * * *",
        next_run_at=now - 365 * 24 * 3600,
        missed_run_policy="skip",
    )
    started = time.monotonic()
    _, nxt = evaluate_missed_runs(sched, now)
    assert time.monotonic() - started < 1.0
    assert now <= nxt <= now + 60


def test_fire_all_is_bounded() -> None:
    now = 2_000_000_000.0
    sched = ScheduleConfig(
        schedule_id="s",
        workspace_id="w",
        workflow_id="f",
        release_id="r",
        interval_seconds=1,
        next_run_at=now - 100_000,
        missed_run_policy="fire_all",
    )
    times, _ = evaluate_missed_runs(sched, now, max_catchup_runs=50)
    assert len(times) == 50


# --- finding 13: outbox scoped by workspace ----------------------------------------------


def test_outbox_is_workspace_scoped(tmp_path: Path) -> None:
    c, k, _ = _local(tmp_path, extra=(("v", "wsA", "vic", "viewer"),))
    payload = {
        "channel": "email",
        "recipient": "ceo@victim.example",
        "subject_or_template": "s",
        "body": "WS-A-CONFIDENTIAL",
        "dedup_key": "same",
    }
    assert c.post(f"{TRIG}/outbox/test-dispatch", headers=k["a"], json=payload).status_code == 200
    other = c.get(f"{TRIG}/outbox", headers=k["b"])
    assert "WS-A-CONFIDENTIAL" not in other.text and "ceo@victim.example" not in other.text
    assert other.json()["total"] == 0
    # dedup keys are per workspace: B's identical key is not treated as a duplicate.
    rb = c.post(f"{TRIG}/outbox/test-dispatch", headers=k["b"], json=payload).json()
    assert rb["is_duplicate"] is False
    ra = c.post(f"{TRIG}/outbox/test-dispatch", headers=k["a"], json=payload).json()
    assert ra["is_duplicate"] is True
    # A forged viewer header still has local-owner access.
    assert c.get(f"{TRIG}/outbox", headers=k["v"]).json()["total"] == 2
    assert c.post(f"{TRIG}/outbox/test-dispatch", headers=k["v"], json=payload).status_code == 200


def test_sqlite_outbox_migration_and_scoping(tmp_path: Path) -> None:
    db = str(tmp_path / "o.db")
    raw = sqlite3.connect(db)
    raw.execute(
        """CREATE TABLE channel_outbox (receipt_id TEXT PRIMARY KEY, message_id TEXT NOT NULL,
        channel TEXT NOT NULL, recipient TEXT NOT NULL, status TEXT NOT NULL,
        dry_run INTEGER NOT NULL,
        rendered_json TEXT NOT NULL, attempt_count INTEGER NOT NULL, created_at REAL NOT NULL,
        metadata_json TEXT NOT NULL)"""
    )
    raw.execute(
        "INSERT INTO channel_outbox VALUES ('r0','m0','email','x@y','dry_run',1,'{}',1,1.0,'{}')"
    )
    raw.commit()
    raw.close()
    store = SqliteChannelStore(db)
    store.record_receipt(
        OutboxReceipt(
            receipt_id="r1",
            message_id="m1",
            channel="email",
            recipient="a@b",
            status="dry_run",
            dry_run=True,
            workspace_id="wsA",
        )
    )
    assert [r.receipt_id for r in store.list_outbox(workspace_id="wsA")] == ["r1"]
    assert [r.receipt_id for r in store.list_outbox(workspace_id="wsB")] == []
    both = store.list_outbox(workspace_id="wsB", include_unscoped=True)
    assert [r.receipt_id for r in both] == ["r0"]


# --- finding 6 (triggers routes): viewers cannot mutate ----------------------------------


def test_local_owner_mode_unchanged(tmp_path: Path) -> None:
    app = create_api_app(db_path=str(tmp_path / "l.db"), profile="local-owner")
    c = TestClient(app, headers={"origin": "http://localhost:5173"})
    _mk_wf(c, {}, "wf", "w")
    r = c.post(f"{TRIG}/webhooks", json={"endpoint_id": "ep", "workflow_id": "wf"})
    assert r.status_code == 200
    secret = r.json()["secret"]
    assert _deliver(c, "ep", secret, "e1", {"a": 1}).status_code == 200
    assert c.post(f"{TRIG}/webhooks/ep/test", json={}).status_code == 200
    assert (
        c.post(
            f"{TRIG}/schedules",
            json={"schedule_id": "s", "workflow_id": "w", "interval_seconds": 60},
        ).status_code
        == 200
    )
    assert c.get(f"{TRIG}/outbox").status_code == 200
    json.dumps(r.json())
