"""Regression tests promoted from the independent C9 review, with assertions preserved.

Only disposable databases/files below pytest's --basetemp are used. Interleaving
hooks coordinate real second SQLite connections; retention decisions are unmodified.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import os
import sqlite3
import threading
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from nexura_agents.contracts.execution import ExecutionId
from nexura_agents.durable import SQLiteCheckpointStore
from nexura_agents.execution_store import SQLiteExecutionStore
from nexura_agents.repro import ReproBundle

import nexura_runtime.retention as retention
from nexura_runtime.cli.main import main
from nexura_runtime.observability.models import EventRecord
from nexura_runtime.observability.sinks import JsonlEventSink, SqliteEventSink
from nexura_runtime.runtime.approvals.manager import ApprovalPolicyManager
from nexura_runtime.runtime.approvals.models import ApprovalDecisionRecord, ApprovalRequest
from nexura_runtime.service.repro_capture import FailedRunCapture
from nexura_runtime.studio.store import StudioStore

NOW = 2_000_000_000.0
DAY = 86400
OLD = NOW - 100 * DAY
CUTOFF = NOW - 30 * DAY
STATUSES = (
    "running",
    "paused",
    "pending_approval",
    "waiting_for_input",
    "interrupted",
    "effect_unknown",
)


@pytest.fixture(autouse=True)
def fixed_retention_clock(monkeypatch):
    # Replace only retention's reference, not the global time module used by stores.
    monkeypatch.setattr(retention, "time", SimpleNamespace(time=lambda: NOW))


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "service.sqlite3"
    store = StudioStore(path)
    store._close_sync()
    return path


def sql(db, statement, args=()):
    conn = sqlite3.connect(db)
    try:
        with conn:
            return conn.execute(statement, args).fetchall()
    finally:
        conn.close()


def run(db, rid="r", status="completed", timestamp=OLD, **columns):
    fields = dict(
        run_id=rid,
        workflow_id="wf",
        spec_hash="hash",
        spec_json='{"name":"review"}',
        status=status,
        input_state_json="{}",
        current_state_json="{}",
        created_at=timestamp,
        updated_at=timestamp,
    )
    fields.update(columns)
    sql(
        db,
        f"INSERT INTO nexura_studio_runs ({','.join(fields)}) VALUES "
        f"({','.join('?' for _ in fields)})",
        tuple(fields.values()),
    )
    event(db, rid, timestamp)


def event(db, rid="r", timestamp=OLD):
    sql(
        db,
        "INSERT INTO nexura_studio_run_events "
        "(run_id,event_seq,event_type,data_json,timestamp) VALUES (?,1,'REVIEW','{}',?)",
        (rid, timestamp),
    )


def exists(db, rid="r"):
    return bool(sql(db, "SELECT 1 FROM nexura_studio_runs WHERE run_id=?", (rid,)))


def approval(db, rid="r", status="pending", workspace="default"):
    manager = ApprovalPolicyManager(db)
    manager.create_request(
        ApprovalRequest(
            approval_id=f"approval-{rid}",
            workspace_id=workspace,
            run_id=rid,
            expires_at=NOW + DAY,
        )
    )
    if status != "pending":
        sql(db, "UPDATE approval_requests SET status=? WHERE run_id=?", (status, rid))


def bundle(db, rid="r", workspace="default", timestamp=OLD, data=None):
    capture = FailedRunCapture(db)
    capture._write(rid, workspace, data or {"run_id": rid})
    sql(db, "UPDATE nexura_service_repro_bundles SET created_at=? WHERE run_id=?", (timestamp, rid))


def trace(db, rid="r", timestamp=OLD):
    sink = SqliteEventSink(str(db), payload_export=True)
    sink.emit(EventRecord(seq_num=1, run_id=rid, event_type="REVIEW", timestamp=timestamp))
    sink.close()


def old_file(path, content="review", timestamp=OLD):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    os.utime(path, (timestamp, timestamp))
    return path


async def journal(db, rid="r", parent=None):
    store = SQLiteExecutionStore(db)
    try:
        await store.init_db()
        return await store.create(
            ExecutionId("default", rid),
            owner="review-worker",
            definition_digest="digest",
            kind="workflow",
            parent_run_id=parent,
        )
    finally:
        await store.close()


@pytest.mark.parametrize("status", STATUSES)
def test_control_nonterminal_rows_and_events_survive(db, status):
    run(db, status=status)
    report = retention.apply_retention(retention.RetentionPolicy(0), db_path=db, dry_run=False)
    assert exists(db) and sql(db, "SELECT 1 FROM nexura_studio_run_events")
    assert report.runs_deleted == report.events_deleted == 0


@pytest.mark.parametrize("status", ["pending", "claimed", "future_unresolved_status"])
def test_control_approval_referenced_terminal_survives(db, status):
    run(db)
    approval(db, status=status, workspace="other-workspace")
    report = retention.apply_retention(
        retention.RetentionPolicy(runs_days=30), db_path=db, dry_run=False
    )
    assert exists(db) and report.runs_deleted == 0


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_terminal_projection_with_pending_effect_is_protected(db, status):
    run(db, status=status, pending_effect_json='{"effect_id":"uncertain-write"}')
    report = retention.apply_retention(
        retention.RetentionPolicy(runs_days=30), db_path=db, dry_run=False
    )
    assert exists(db), f"Deleted {status} run with pending effect: {asdict(report)}"


def test_terminal_projection_with_pending_input_is_protected(db):
    run(db, pending_input_json='{"prompt":"required answer"}')
    retention.apply_retention(retention.RetentionPolicy(runs_days=30), db_path=db, dry_run=False)
    assert exists(db), "Deleted run still carrying pending input"


def test_terminal_projection_with_inline_pending_approval_is_protected(db):
    # Workflow PendingApproval is stored inline; it need not create an M3 approval row.
    run(db, approval_id="inline", approval_json='{"approval_id":"inline","run_id":"r"}')
    retention.apply_retention(retention.RetentionPolicy(runs_days=30), db_path=db, dry_run=False)
    assert exists(db), "Deleted inline approval's run when approval_requests table is absent"


def test_actual_sdk_approval_schema_works_in_dry_run(db):
    run(db)
    store = SQLiteCheckpointStore(db)
    try:
        # Use the store's synchronous implementation: asyncio.to_thread completion
        # notification hangs in this restricted command environment (diagnostic log).
        store._approval_sync("save", "a", json.dumps({"run_id": "r"}), NOW + DAY)
        report = retention.apply_retention(retention.RetentionPolicy(runs_days=30), db_path=db)
        assert exists(db) and report.runs_deleted == 0
    finally:
        store.close()


def test_actual_claimed_approval_protects_run_during_dispatch(db):
    run(db)
    manager = ApprovalPolicyManager(db)
    request = manager.create_request(
        ApprovalRequest(
            approval_id="dispatch",
            workspace_id="default",
            run_id="r",
            expires_at=NOW + DAY,
            action_name="critical_write",
            spec_digest="spec",
        )
    )
    manager.submit_decision(
        "default",
        "dispatch",
        ApprovalDecisionRecord(
            approval_id="dispatch",
            workspace_id="default",
            decided_by_principal_id="operator",
            decided_by_role="admin",
            decision="approved",
            expected_payload_digest=request.payload_digest,
        ),
    )
    assert (
        manager.claim_for_dispatch(
            "default", "dispatch", "active-worker", identity_digest=request.identity_digest
        )
        == "claimed"
    )
    assert sql(db, "SELECT status,claimed_by FROM approval_requests") == [
        ("approved", "active-worker")
    ]
    report = retention.apply_retention(
        retention.RetentionPolicy(runs_days=30), db_path=db, dry_run=False
    )
    assert exists(db), f"Deleted run during actual claimed approval dispatch: {asdict(report)}"


def test_authoritative_running_journal_protects_terminal_projection(db):
    run(db, journal_workspace="default")
    asyncio.run(journal(db))
    report = retention.apply_retention(
        retention.RetentionPolicy(runs_days=30), db_path=db, dry_run=False
    )
    assert exists(db), (
        f"Deleted projection while authoritative journal is running: {asdict(report)}"
    )


@pytest.mark.parametrize("direction", ["pending-child", "pending-parent"])
def test_pending_related_run_protects_terminal_record(db, direction):
    if direction == "pending-child":
        run(db, "parent", journal_workspace="default")
        asyncio.run(journal(db, "parent/child", parent="parent"))
        protected = "parent"
    else:
        run(db, "parent", status="pending_approval", journal_workspace="default")
        run(db, "parent/child", journal_workspace="default")
        asyncio.run(journal(db, "parent"))
        child = asyncio.run(journal(db, "parent/child", parent="parent"))
        # Persist a completed child through the journal's actual column/JSON schema.
        payload = asdict(child)
        payload["status"] = "completed"
        sql(
            db,
            "UPDATE nexura_exec_runs SET status='completed',run_json=? WHERE run_id=?",
            (json.dumps(payload), "parent/child"),
        )
        protected = "parent/child"
    retention.apply_retention(retention.RetentionPolicy(runs_days=30), db_path=db, dry_run=False)
    assert exists(db, protected), f"Deleted {protected} while its {direction} is unresolved"


@pytest.mark.parametrize("status", [*STATUSES, "approval-reference"])
def test_protected_run_artifacts_survive(db, status):
    run(db, status="failed" if status == "approval-reference" else status)
    if status == "approval-reference":
        approval(db)
    bundle(db)
    trace(db)
    report = retention.apply_retention(retention.RetentionPolicy(30), db_path=db, dry_run=False)
    assert sql(db, "SELECT 1 FROM nexura_service_repro_bundles"), (
        f"Deleted {status} run's bundle: {asdict(report)}"
    )
    assert sql(db, "SELECT 1 FROM observability_events"), f"Deleted {status} run's traces"


@pytest.mark.parametrize(
    "subdir,kind,filename",
    [
        ("repro", "bundles", "precious.txt"),
        ("bundles", "bundles", "precious.txt"),
        ("traces", "traces", "precious.jsonl"),
        ("observability", "traces", "precious.jsonl"),
    ],
)
def test_symlink_directory_must_not_delete_outside_data_dir(tmp_path, subdir, kind, filename):
    root = tmp_path / "data"
    root.mkdir()
    outside = tmp_path / "outside-policy"
    target = old_file(outside / filename)
    (root / subdir).symlink_to(outside, target_is_directory=True)
    report = retention.apply_retention(
        retention.RetentionPolicy(30, kinds=[kind]), data_dir=root, dry_run=False
    )
    assert target.exists(), f"Deleted outside data_dir through {subdir} symlink: {asdict(report)}"


@pytest.mark.parametrize("relative", ["bundle-config.json", "repro/credentials.sqlite3"])
def test_filename_is_not_proof_of_nexura_artifact_ownership(tmp_path, relative):
    target = old_file(tmp_path / relative, "unrelated application data")
    retention.apply_retention(
        retention.RetentionPolicy(bundles_days=30), data_dir=tmp_path, dry_run=False
    )
    assert target.exists(), f"Deleted unrelated file solely because of path: {relative}"


def test_bundle_scan_cannot_unlink_workflow_journal_database(tmp_path):
    data = tmp_path / "data"
    db = data / "bundles" / "service.sqlite3"
    db.parent.mkdir(parents=True)
    asyncio.run(journal(db))
    os.utime(db, (OLD, OLD))
    report = retention.apply_retention(
        retention.RetentionPolicy(bundles_days=30), db_path=db, data_dir=data, dry_run=False
    )
    assert db.exists(), f"Bundle scanner unlinked execution journal DB: {asdict(report)}"


@pytest.mark.parametrize("fresh_time", [NOW, NOW + DAY])
def test_invalid_negative_api_policy_cannot_delete_fresh_or_future_data(db, fresh_time):
    run(db, timestamp=fresh_time)
    with contextlib.suppress(ValueError):
        retention.apply_retention(
            retention.RetentionPolicy(runs_days=-2), db_path=db, dry_run=False
        )
    assert exists(db), "Negative API age created future cutoff and deleted fresh/future data"


@pytest.mark.parametrize("age", [float("nan"), float("inf"), float("-inf")])
def test_api_rejects_nonfinite_policy(age):
    with pytest.raises(ValueError):
        retention.RetentionPolicy(age)


@pytest.mark.parametrize("age", ["nan", "inf"])
def test_cli_rejects_nonfinite_policy(db, age):
    assert main(["retention", "--older-than-days", age, "--db", str(db), "--apply"]) != 0


def test_new_run_event_must_not_be_deleted_with_old_terminal_run(db):
    run(db)
    # This is a real store method; it appends the event without updating the run's age.
    store = StudioStore(db)
    store._record_run_event_sync("r", "RECENT_AUDIT", data={"review": True})
    store._close_sync()
    # NOW is in 2033; explicitly place this genuine event after the fixed 30-day cutoff.
    sql(
        db,
        "UPDATE nexura_studio_run_events SET timestamp=? WHERE event_type='RECENT_AUDIT'",
        (NOW,),
    )
    report = retention.apply_retention(
        retention.RetentionPolicy(runs_days=30), db_path=db, dry_run=False
    )
    assert sql(db, "SELECT 1 FROM nexura_studio_run_events WHERE timestamp>=?", (CUTOFF,)), (
        f"Deleted newer-than-cutoff event: {asdict(report)}"
    )


@pytest.mark.parametrize("kind", ["bundles", "traces"])
def test_disk_internal_timestamp_newer_than_cutoff_survives_old_mtime(tmp_path, kind):
    if kind == "traces":
        path = tmp_path / "recent.traces.jsonl"
        sink = JsonlEventSink(str(path))
        sink.emit(EventRecord(seq_num=1, run_id="r", event_type="RECENT", timestamp=NOW))
        sink.close()
        os.utime(path, (OLD, OLD))
    else:
        path = tmp_path / "recent.repro.json"
        ReproBundle(
            {
                "schema_version": 1,
                "run_kind": "workflow",
                "inputs": {},
                "spec": {},
                "model_responses": [],
                "tool_fixtures": [],
                "approvals": [],
                "budget_config": None,
                "operations": ["run"],
                "created_at": datetime.fromtimestamp(NOW, UTC).isoformat(),
                "expected_outcome": {
                    "status": "failed",
                    "error_type": "ReviewError",
                    "error_message": "offline fixture",
                    "failing_step": None,
                },
            }
        ).save(path)
        os.utime(path, (OLD, OLD))
    retention.apply_retention(
        retention.RetentionPolicy(30, kinds=[kind]), data_dir=tmp_path, dry_run=False
    )
    assert path.exists(), "Old file mtime hid new content timestamp; artifact was deleted"


@pytest.mark.parametrize("change", [*STATUSES, "fresh-terminal", "new-approval"])
def test_live_service_change_after_selection_is_rechecked(db, monkeypatch, change):
    run(db)
    if change == "new-approval":
        ApprovalPolicyManager(db)  # Create schema before the selection snapshot.
    before_count = retention._count_journal_rows_for_runs

    def interleave(conn, run_ids):
        result = before_count(conn, run_ids)
        if change == "new-approval":
            approval(db)
        elif change == "fresh-terminal":
            sql(db, "UPDATE nexura_studio_runs SET updated_at=? WHERE run_id='r'", (NOW,))
        else:
            store = StudioStore(db)
            try:
                assert store._update_run_sync("r", change, None, {}, 1)
            finally:
                store._close_sync()
        return result

    monkeypatch.setattr(retention, "_count_journal_rows_for_runs", interleave)
    report = retention.apply_retention(
        retention.RetentionPolicy(runs_days=30), db_path=db, dry_run=False
    )
    assert exists(db), f"Deleted after concurrent transition to {change}: {asdict(report)}"


def test_live_service_append_after_delete_does_not_orphan_events(db, monkeypatch):
    run(db)
    service = StudioStore(db)
    before_bundles = retention._process_db_bundles_retention

    def service_read_then_write(*args, **kwargs):
        # Models an in-flight request that read the run before retention committed.
        service._record_run_event_sync("r", "LATE_EVENT", data={"source": "live-request"})
        return before_bundles(*args, **kwargs)

    try:
        assert service._get_run_status_sync("r") == "completed"
        monkeypatch.setattr(retention, "_process_db_bundles_retention", service_read_then_write)
        retention.apply_retention(retention.RetentionPolicy(30), db_path=db, dry_run=False)
        orphans = sql(
            db,
            "SELECT e.run_id FROM nexura_studio_run_events e LEFT JOIN "
            "nexura_studio_runs r ON r.run_id=e.run_id WHERE r.run_id IS NULL",
        )
        assert not orphans, f"Live service appended orphan event after deletion: {orphans}"
    finally:
        service._close_sync()


@pytest.mark.parametrize("kind", ["bundles", "traces"])
def test_failed_unlink_is_not_reported_as_success(tmp_path, monkeypatch, capsys, kind):
    filename = "blocked.repro.json" if kind == "bundles" else "blocked.traces.jsonl"
    target = old_file(tmp_path / filename, '{"review":true}\n')
    original = Path.unlink

    def fail_target(path, *args, **kwargs):
        if path == target:
            raise PermissionError("review injected EACCES")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_target)
    code = main(
        [
            "retention",
            "--older-than-days",
            "30",
            "--kinds",
            kind,
            "--db",
            str(tmp_path / "absent.sqlite3"),
            "--data-dir",
            str(tmp_path),
            "--apply",
        ]
    )
    out = capsys.readouterr().out
    assert target.exists()
    assert code != 0, f"CLI returned 0 and reported successful deletion after EACCES:\n{out}"


def test_later_db_failure_returns_partial_result_or_rolls_back(db):
    run(db)
    bundle(db)
    sql(
        db,
        "CREATE TRIGGER review_deny_bundle BEFORE DELETE ON nexura_service_repro_bundles "
        "BEGIN SELECT RAISE(ABORT, 'review injected bundle failure'); END",
    )
    try:
        report = retention.apply_retention(retention.RetentionPolicy(30), db_path=db, dry_run=False)
    except sqlite3.DatabaseError as exc:
        assert exists(db) or hasattr(exc, "report"), (
            "Runs/events committed before bundle failure; exception carries no partial report"
        )
    else:
        assert getattr(report, "errors", None), "Partial failure missing from report"


def test_report_counts_utf8_bytes_not_unicode_characters(db):
    # Positive accounting requires terminal lifecycle authority; a capture's
    # run ID alone must never authorize deletion (Round 4 negative control).
    run(db, rid="unicode")
    payload = {"value": "日本語🔥"}
    bundle(db, rid="unicode", workspace="other", data=payload)
    text = sql(db, "SELECT bundle_json FROM nexura_service_repro_bundles")[0][0]
    expected = len(text.encode("utf-8")) + len(b"unicode") + len(b"other")
    # A checkpointed quiet database can be previewed without initializing SHM.
    sql(db, "PRAGMA journal_mode=DELETE")
    report = retention.apply_retention(retention.RetentionPolicy(bundles_days=30), db_path=db)
    assert report.bundles_bytes == expected, (
        f"Report {report.bundles_bytes} bytes, stored UTF-8 text {expected} bytes"
    )


def test_empty_trace_file_reports_zero_trace_records(tmp_path):
    old_file(tmp_path / "empty.traces.jsonl", "")
    report = retention.apply_retention(retention.RetentionPolicy(traces_days=30), data_dir=tmp_path)
    assert report.traces_deleted == 0, f"Empty file reported as {report.traces_deleted} traces"


def test_control_run_delete_error_rolls_back_events(db):
    run(db)
    sql(
        db,
        "CREATE TRIGGER review_deny_run BEFORE DELETE ON nexura_studio_runs "
        "BEGIN SELECT RAISE(ABORT, 'review injected run failure'); END",
    )
    with pytest.raises(sqlite3.DatabaseError):
        retention.apply_retention(
            retention.RetentionPolicy(runs_days=30), db_path=db, dry_run=False
        )
    assert exists(db) and sql(db, "SELECT 1 FROM nexura_studio_run_events")


def test_control_default_api_and_cli_dry_runs_preserve_bytes(db, tmp_path, capsys):
    run(db)
    bundle(db)
    trace(db)
    sql(db, "PRAGMA journal_mode=DELETE")
    data = tmp_path / "data"
    target = old_file(data / "old.repro.json")
    conn = sqlite3.connect(db)
    try:
        before_rows = list(conn.iterdump())
    finally:
        conn.close()
    before_file = hashlib.sha256(target.read_bytes()).hexdigest()
    report = retention.apply_retention(retention.RetentionPolicy(30), db_path=db, data_dir=data)
    assert report.dry_run and report.runs_deleted == 1
    assert (
        main(["retention", "--older-than-days", "30", "--db", str(db), "--data-dir", str(data)])
        == 0
    )
    assert "DRY RUN" in capsys.readouterr().out
    conn = sqlite3.connect(db)
    try:
        assert before_rows == list(conn.iterdump())
    finally:
        conn.close()
    assert before_file == hashlib.sha256(target.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    "timestamp",
    [
        CUTOFF,
        math.nextafter(CUTOFF, math.inf),
        NOW,
        NOW + DAY,
        str(NOW),
        "2033-05-18T03:33:20Z",
        "2033-05-18T05:33:20+02:00",
        "invalid-date",
    ],
)
def test_control_newer_equal_and_ambiguous_timestamps_are_not_deleted(db, timestamp):
    run(db, timestamp=timestamp)
    bundle(db, timestamp=timestamp)
    trace(db, timestamp=NOW)
    # Bypass EventRecord coercion to probe SQLite timestamp representations too.
    sql(db, "UPDATE observability_events SET timestamp=?", (timestamp,))
    retention.apply_retention(retention.RetentionPolicy(30), db_path=db, dry_run=False)
    assert exists(db)
    assert sql(db, "SELECT 1 FROM nexura_service_repro_bundles")
    assert sql(db, "SELECT 1 FROM observability_events")


def test_control_float_immediately_older_than_cutoff_is_eligible(db):
    run(db, timestamp=math.nextafter(CUTOFF, -math.inf))
    report = retention.apply_retention(
        retention.RetentionPolicy(runs_days=30), db_path=db, dry_run=False
    )
    assert not exists(db) and report.runs_deleted == report.events_deleted == 1


def test_control_workspace_and_kinds_policy(db):
    run(db, "old-other", journal_workspace="other", timestamp=OLD)
    run(db, "young-other", journal_workspace="other", timestamp=NOW)
    bundle(db, "old-other", workspace="other")
    trace(db, "old-other")

    # A journal marker requires a real matching authority. Keep this positive
    # workspace-policy control authoritative; its assertions are unchanged.
    async def settled_journals():
        store = SQLiteExecutionStore(db)
        try:
            await store.init_db()
            for rid, stamp in (("old-other", OLD), ("young-other", NOW)):
                record = await store.create(
                    ExecutionId("other", rid), owner="review", definition_digest="digest"
                )
                from dataclasses import asdict

                payload = asdict(record)
                payload.update(
                    status="completed", updated_at=stamp, lease_owner=None, lease_expires_at=None
                )
                sql(
                    db,
                    "UPDATE nexura_exec_runs SET status='completed',lease_owner=NULL,"
                    "lease_expires_at=NULL,run_json=? WHERE run_id=?",
                    (json.dumps(payload), rid),
                )
        finally:
            await store.close()

    asyncio.run(settled_journals())
    retention.apply_retention(retention.RetentionPolicy(runs_days=30), db_path=db, dry_run=False)
    # Policy is explicitly database-wide; there is no claimed workspace selector.
    assert not exists(db, "old-other") and exists(db, "young-other")
    assert sql(db, "SELECT 1 FROM nexura_service_repro_bundles")
    assert sql(db, "SELECT 1 FROM observability_events")


def test_control_traversal_bundle_id_is_not_interpreted_as_path(db, tmp_path):
    outside = old_file(tmp_path / "outside" / "precious.json")
    root = tmp_path / "data"
    root.mkdir()
    bundle(db, "../outside/precious.json")
    retention.apply_retention(
        retention.RetentionPolicy(bundles_days=30), db_path=db, data_dir=root, dry_run=False
    )
    assert outside.exists()


def test_control_file_symlink_does_not_unlink_target(tmp_path):
    outside = old_file(tmp_path / "outside" / "target.json")
    root = tmp_path / "data"
    root.mkdir()
    link = root / "old.repro.json"
    link.symlink_to(outside)
    retention.apply_retention(
        retention.RetentionPolicy(bundles_days=30), data_dir=root, dry_run=False
    )
    assert outside.exists()


def test_control_journal_rows_unchanged(db):
    run(db, journal_workspace="default")
    asyncio.run(journal(db))
    tables = [
        r[0]
        for r in sql(
            db,
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND (name LIKE 'nexura_exec_%' OR name LIKE 'nexura_workflow_%')",
        )
    ]
    before = {name: sql(db, f"SELECT * FROM {name}") for name in tables}
    retention.apply_retention(retention.RetentionPolicy(runs_days=30), db_path=db, dry_run=False)
    assert before == {name: sql(db, f"SELECT * FROM {name}") for name in tables}


def test_control_live_wal_reader_keeps_its_snapshot(db):
    run(db)
    reader = sqlite3.connect(db)
    try:
        reader.execute("BEGIN")
        assert reader.execute("SELECT run_id FROM nexura_studio_runs").fetchall() == [("r",)]
        retention.apply_retention(
            retention.RetentionPolicy(runs_days=30), db_path=db, dry_run=False
        )
        assert reader.execute("SELECT run_id FROM nexura_studio_runs").fetchall() == [("r",)]
        reader.rollback()
        assert not exists(db)
    finally:
        reader.close()


def test_control_short_writer_lock_is_respected(db):
    run(db)
    locked = threading.Event()
    release = threading.Event()

    def writer():
        conn = sqlite3.connect(db)
        try:
            conn.execute("BEGIN IMMEDIATE")
            locked.set()
            assert release.wait(5)
            conn.commit()
        finally:
            conn.close()

    thread = threading.Thread(target=writer)
    thread.start()
    assert locked.wait(5)
    timer = threading.Timer(0.1, release.set)
    timer.start()
    try:
        report = retention.apply_retention(
            retention.RetentionPolicy(runs_days=30), db_path=db, dry_run=False
        )
        assert report.runs_deleted == 1 and not exists(db)
    finally:
        release.set()
        thread.join(5)
        timer.join(5)


def test_live_service_read_is_consistent_across_run_and_events(db):
    run(db)
    store = StudioStore(db)
    interleaved = False

    def before_query(statement):
        nonlocal interleaved
        if "SELECT event_seq" in statement and not interleaved:
            interleaved = True
            retention.apply_retention(
                retention.RetentionPolicy(runs_days=30), db_path=db, dry_run=False
            )

    try:
        store._conn.set_trace_callback(before_query)
        snapshot = store._get_run_sync("r")
        assert interleaved and snapshot is not None
        assert snapshot["events"], "Live read returned a run with its events deleted mid-read"
    finally:
        store._close_sync()


def test_live_jsonl_sink_is_not_unlinked_while_open(tmp_path):
    path = old_file(tmp_path / "live.traces.jsonl", '{"old":true}\n')
    sink = JsonlEventSink(str(path))
    try:
        retention.apply_retention(
            retention.RetentionPolicy(traces_days=30), data_dir=tmp_path, dry_run=False
        )
        sink.emit(EventRecord(seq_num=1, run_id="live", event_type="NEW", timestamp=NOW))
    finally:
        sink.close()
    assert path.exists(), "Live sink continued writing to an unlinked file; new events vanished"


def test_concurrent_file_replacement_cannot_delete_newer_content(tmp_path, monkeypatch):
    path = old_file(tmp_path / "replace.repro.json")
    original = Path.unlink

    def replace_then_unlink(target, *args, **kwargs):
        if target == path:
            # A real atomic replacement by a concurrent exporter after retention's stat.
            replacement = old_file(tmp_path / "exporter.tmp", "brand new bundle", NOW)
            replacement.replace(path)
        return original(target, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", replace_then_unlink)
    retention.apply_retention(
        retention.RetentionPolicy(bundles_days=30), data_dir=tmp_path, dry_run=False
    )
    assert path.exists(), "Deleted replacement file newer than cutoff using stale stat"
