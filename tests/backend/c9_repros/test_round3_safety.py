"""Fail-closed filesystem support and persisted authority regressions."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import nexura_runtime.retention as retention
from nexura_runtime.observability.models import EventRecord
from nexura_runtime.studio.store import StudioStore


def setup_export(tmp_path: Path) -> tuple[Path, Path, Path]:
    db = tmp_path / "service.sqlite3"
    store = StudioStore(db)
    store._close_sync()
    conn = sqlite3.connect(db)
    try:
        with conn:
            conn.execute(
                "INSERT INTO nexura_studio_runs "
                "(run_id,spec_hash,spec_json,status,input_state_json,current_state_json,"
                "created_at,updated_at) VALUES ('r','h','{}','completed','{}','{}',?,?)",
                (time.time() - 100 * 86400,) * 2,
            )
    finally:
        conn.close()
    root = tmp_path / "exports"
    root.mkdir()
    content = EventRecord(
        seq_num=1, run_id="r", timestamp=time.time() - 100 * 86400, event_type="LOCAL_FAKE"
    ).model_dump_json()
    (root / "trace.jsonl").write_text(content)
    path = retention.write_retention_artifact(
        content,
        db_path=db,
        data_dir=root,
        relative_path="trace.jsonl",
        run_id="r",
        kind="traces",
    )
    # Establish the fixture's original closed/checkpointed starting state. The
    # retention worker now deliberately leaves WAL/SHM cleanup to the operator;
    # do this before any hostile actor or guard is installed, with fixture-owned
    # SQLite rather than changing/weakening any retention assertion.
    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        # A quiet preview fixture must not require recreating a WAL index.
        conn.execute("PRAGMA journal_mode=DELETE").fetchall()
    finally:
        conn.close()
    return db, root, path


@pytest.mark.parametrize("dry_run", [True, False])
def test_exports_never_mutate_namespace_without_exclusion(tmp_path, monkeypatch, dry_run):
    db, root, path = setup_export(tmp_path)
    original = path.read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("Filesystem mutation attempted without enforceable exclusion")

    with monkeypatch.context() as scope:
        for name in ("unlink", "rename", "rmdir", "replace", "link"):
            scope.setattr(os, name, forbidden)
        report = retention.apply_retention(
            retention.RetentionPolicy(30), db_path=db, data_dir=root, dry_run=dry_run
        )
    assert path.read_bytes() == original
    assert report.files_deleted == report.traces_deleted == 0
    assert report.runs_deleted == 1
    assert len(report.eligible_files) == 1 and not report.errors
    assert report.eligible_files[0].path == str(path)


def test_longer_export_retention_keeps_lifecycle_and_reports_later_refusal(tmp_path, monkeypatch):
    db, root, path = setup_export(tmp_path)
    raw = path.read_bytes()
    policy = retention.RetentionPolicy(runs_days=30, traces_days=120)
    first = retention.apply_retention(policy, db_path=db, data_dir=root, dry_run=False)
    assert first.runs_deleted == 1 and first.files_deleted == 0
    assert path.read_bytes() == raw and not first.errors and not first.eligible_files
    future = time.time() + 100 * 86400
    monkeypatch.setattr(retention, "time", SimpleNamespace(time=lambda: future))
    second = retention.apply_retention(policy, db_path=db, data_dir=root, dry_run=False)
    assert path.exists() and second.runs_deleted == second.files_deleted == 0
    assert path.read_bytes() == raw and not second.errors
    assert len(second.eligible_files) == 1
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT status FROM nexura_studio_runs").fetchall() == []
        assert conn.execute(f"SELECT status FROM {retention.LIFECYCLE_TABLE}").fetchall() == [
            ("completed",)
        ]
    finally:
        conn.close()


@pytest.mark.parametrize("dry_run", [True, False])
def test_round4_unregistered_schema_is_listed_without_filename_heuristics(tmp_path, dry_run):
    db, root, registered = setup_export(tmp_path)
    raw = registered.read_bytes()
    loose = root / "unrecognisable-name.without-trace-suffix"
    loose.write_bytes(raw)
    report = retention.apply_retention(
        retention.RetentionPolicy(30), db_path=db, data_dir=root, dry_run=dry_run
    )
    assert registered.read_bytes() == loose.read_bytes() == raw
    assert {entry.path for entry in report.eligible_files} == {str(registered), str(loose)}
    for entry in report.eligible_files:
        assert entry.run_id == "r" and entry.kind == "traces"
        assert entry.size_bytes == len(raw) and entry.age_days > 99
        assert entry.reason == "older than cutoff; terminal, unprotected run; manual removal only"
    assert report.files_deleted == report.traces_deleted == 0 and not report.errors


def test_round4_registration_never_creates_export(tmp_path):
    db, root, path = setup_export(tmp_path)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="inspect export safely"):
        retention.write_retention_artifact(
            before.decode(),
            db_path=db,
            data_dir=root,
            relative_path="missing",
            run_id="r",
            kind="traces",
        )
    assert not (root / "missing").exists() and path.read_bytes() == before


def test_round4_file_worker_refuses_any_write_or_namespace_operation(tmp_path):
    _, root, path = setup_export(tmp_path)
    before = path.read_bytes()
    script = """
import os, sys
def audit(event, arguments):
    if event == "open":
        flags = arguments[2]
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            raise AssertionError("file opened for writing")
    if event in {"os.remove", "os.rename", "os.rmdir", "os.link", "os.symlink", "os.chmod"}:
        raise AssertionError("file namespace modified")
sys.addaudithook(audit)
from nexura_runtime.retention import _inspect_export_worker
_inspect_export_worker()
"""
    process = subprocess.run(
        [sys.executable, "-B", "-c", script],
        input=json.dumps([str(root), path.name, "traces", "r"]),
        capture_output=True,
        text=True,
        timeout=35,
    )
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout)["kind"] == "traces"
    assert path.read_bytes() == before


@pytest.mark.parametrize("change", ["running", "approval"])
def test_round4_live_authority_overrides_retained_terminal_snapshot(tmp_path, change):
    db, root, path = setup_export(tmp_path)
    before = path.read_bytes()
    retention.apply_retention(retention.RetentionPolicy(runs_days=30), db_path=db, dry_run=False)
    conn = sqlite3.connect(db)
    try:
        with conn:
            conn.execute(
                "INSERT INTO nexura_studio_runs "
                "(run_id,spec_hash,spec_json,status,input_state_json,current_state_json,"
                "created_at,updated_at,approval_json) VALUES ('r','h','{}',?,'{}','{}',?,?,?)",
                (
                    "running" if change == "running" else "completed",
                    time.time() - 100 * 86400,
                    time.time() - 100 * 86400,
                    "{}" if change == "approval" else None,
                ),
            )
    finally:
        conn.close()
    report = retention.apply_retention(
        retention.RetentionPolicy(30), db_path=db, data_dir=root, dry_run=False
    )
    assert path.read_bytes() == before and not report.eligible_files
    assert report.runs_deleted == report.files_deleted == 0


def test_round4_bundle_identity_alone_is_not_terminal_authority(tmp_path):
    db, root, path = setup_export(tmp_path)
    raw = path.read_bytes()
    conn = sqlite3.connect(db)
    try:
        with conn:
            conn.execute("DELETE FROM nexura_studio_runs")
            conn.execute(
                "CREATE TABLE nexura_service_repro_bundles "
                "(run_id TEXT PRIMARY KEY,workspace_id TEXT,created_at REAL,bundle_json TEXT)"
            )
            conn.execute("INSERT INTO nexura_service_repro_bundles VALUES ('r','default',1,'{}')")
        report = retention.apply_retention(
            retention.RetentionPolicy(30), db_path=db, data_dir=root, dry_run=False
        )
        assert conn.execute("SELECT run_id FROM nexura_service_repro_bundles").fetchall() == [
            ("r",)
        ]
    finally:
        conn.close()
    assert path.read_bytes() == raw and not report.eligible_files
    assert report.bundles_deleted == report.files_deleted == 0


def test_round4_loose_trace_unknown_child_protects_parent_before_sql_deletion(tmp_path):
    db, root, path = setup_export(tmp_path)
    raw = path.read_bytes()
    loose = root / "unknown-child"
    content = EventRecord(
        seq_num=1, run_id="unknown", parent_run_id="r", timestamp=1, event_type="LOCAL_FAKE"
    ).model_dump_json()
    loose.write_text(content)
    report = retention.apply_retention(
        retention.RetentionPolicy(30), db_path=db, data_dir=root, dry_run=False
    )
    assert path.read_bytes() == raw and loose.read_text() == content
    assert report.runs_deleted == report.files_deleted == 0 and not report.eligible_files


@pytest.mark.parametrize("metadata", [None, "{}", '[""]', "[1]"])
def test_unknown_registered_dependencies_stop_before_any_deletion(tmp_path, metadata):
    db, root, path = setup_export(tmp_path)
    conn = sqlite3.connect(db)
    try:
        with conn:
            conn.execute("UPDATE nexura_retention_artifacts SET parent_run_ids_json=?", (metadata,))
    finally:
        conn.close()
    with pytest.raises(retention.RetentionError) as failure:
        retention.apply_retention(
            retention.RetentionPolicy(30), db_path=db, data_dir=root, dry_run=False
        )
    assert path.exists()
    assert failure.value.report.runs_deleted == failure.value.report.files_deleted == 0
    assert failure.value.report.errors


@pytest.mark.parametrize("high_descriptor", [False, True])
def test_unrelated_writable_descriptor_uses_isolated_database(
    tmp_path, monkeypatch, high_descriptor
):
    db, root, path = setup_export(tmp_path)
    unrelated = tmp_path / "other-owner"
    unrelated.write_text("unrelated data")
    conn = sqlite3.connect(db)
    try:
        with conn:
            conn.execute(
                "CREATE TABLE nexura_service_repro_bundles "
                "(run_id TEXT PRIMARY KEY,workspace_id TEXT,created_at REAL,bundle_json TEXT)"
            )
            conn.execute(
                "INSERT INTO nexura_service_repro_bundles VALUES ('r','default',1,?)",
                (sqlite3.Binary(b'{"local":"payload"}'),),
            )
    finally:
        conn.close()
    if high_descriptor:
        import fcntl

        original = retention.subprocess.Popen

        def high_pipe(*args, **kwargs):
            process = original(*args, **kwargs)
            if process.stdout is not None and any("_serve_database" in part for part in args[0]):
                fd = fcntl.fcntl(process.stdout.fileno(), fcntl.F_DUPFD_CLOEXEC, 2048)
                process.stdout.close()
                process.stdout = os.fdopen(fd, "r")
            return process

        monkeypatch.setattr(retention.subprocess, "Popen", high_pipe)
    with unrelated.open("r+b"):
        report = retention.apply_retention(
            retention.RetentionPolicy(bundles_days=0), db_path=db, data_dir=root, dry_run=False
        )
    assert path.exists() and unrelated.read_text() == "unrelated data"
    assert report.bundles_deleted == 1 and report.runs_deleted == 0
    assert report.bundles_bytes == len(b"rdefault") + len(b'{"local":"payload"}')


def test_caller_write_transaction_and_event_both_commit(tmp_path):
    db, _, _ = setup_export(tmp_path)
    store = StudioStore(db)
    try:
        store._conn.execute("BEGIN IMMEDIATE")
        store._conn.execute("UPDATE nexura_studio_runs SET error='caller' WHERE run_id='r'")
        store._record_run_event_sync("r", "LOCAL_FAKE", data={"valid": True})
        conn = sqlite3.connect(db)
        try:
            assert conn.execute("SELECT error FROM nexura_studio_runs").fetchall() == [("caller",)]
            assert json.loads(
                conn.execute("SELECT data_json FROM nexura_studio_run_events").fetchone()[0]
            ) == {"valid": True}
        finally:
            conn.close()
        assert not store._conn.in_transaction
    finally:
        store._close_sync()


def test_hard_linked_database_is_not_an_authorized_delete_target(tmp_path):
    db, root, _ = setup_export(tmp_path)
    alias = tmp_path / "other-workspace.sqlite3"
    os.link(db, alias)
    with pytest.raises(retention.RetentionError, match="non-hard-linked") as failure:
        retention.apply_retention(
            retention.RetentionPolicy(0), db_path=db, data_dir=root, dry_run=False
        )
    assert failure.value.report.runs_deleted == 0
    conn = sqlite3.connect(alias)
    try:
        assert conn.execute("SELECT run_id FROM nexura_studio_runs").fetchall() == [("r",)]
    finally:
        conn.close()


def test_foreign_database_as_sidecar_never_authorizes_redirect(tmp_path, monkeypatch):
    db, root, _ = setup_export(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    outside, _, _ = setup_export(other)
    os.link(outside, Path(str(db) + "-wal"))
    original = sqlite3.connect
    redirected = False

    def connect(path, *args, **kwargs):
        nonlocal redirected
        if str(path).startswith(db.as_uri()):
            redirected = True
            return original(outside.as_uri() + "?mode=rw", *args, **kwargs)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(retention.sqlite3, "connect", connect)
    with pytest.raises(retention.RetentionError) as failure:
        retention.apply_retention(
            retention.RetentionPolicy(0), db_path=db, data_dir=root, dry_run=False
        )
    assert not redirected and failure.value.report.runs_deleted == 0
    conn = original(outside)
    try:
        assert conn.execute("SELECT run_id FROM nexura_studio_runs").fetchall() == [("r",)]
    finally:
        conn.close()


def test_unowned_database_named_as_wal_is_not_changed(tmp_path):
    db, root, _ = setup_export(tmp_path)
    main_bytes = db.read_bytes()
    sidecar = Path(str(db) + "-wal")
    sidecar.write_bytes(main_bytes)
    with pytest.raises(retention.RetentionError, match="sidecar format") as failure:
        retention.apply_retention(
            retention.RetentionPolicy(0), db_path=db, data_dir=root, dry_run=False
        )
    assert sidecar.read_bytes() == main_bytes
    assert db.read_bytes() == main_bytes
    assert failure.value.report.runs_deleted == failure.value.report.bytes_freed == 0


def test_isolated_transaction_failure_keeps_committed_partial_report(tmp_path, monkeypatch):
    db, root, path = setup_export(tmp_path)
    conn = sqlite3.connect(db)
    try:
        with conn:
            for rid in ("a", "b"):
                conn.execute(
                    "INSERT INTO nexura_studio_runs "
                    "(run_id,spec_hash,spec_json,status,input_state_json,current_state_json,"
                    "created_at,updated_at) VALUES (?,'h','{}','completed','{}','{}',1,1)",
                    (rid,),
                )
    finally:
        conn.close()
    original = retention._run_retention

    def fail_second(connection, rid, *args, **kwargs):
        if rid == "b":
            raise sqlite3.OperationalError("isolated second-run failure")
        return original(connection, rid, *args, **kwargs)

    monkeypatch.setattr(retention, "_run_retention", fail_second)
    unrelated = tmp_path / "live-descriptor"
    unrelated.write_text("unrelated")
    with unrelated.open("r+b"), pytest.raises(retention.RetentionError) as failure:
        retention.apply_retention(
            retention.RetentionPolicy(runs_days=0), db_path=db, data_dir=root, dry_run=False
        )
    assert path.exists() and unrelated.read_text() == "unrelated"
    assert failure.value.report.runs_deleted == 1 and failure.value.report.bytes_freed > 0
    assert "isolated second-run failure" in failure.value.report.errors[-1]
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT run_id FROM nexura_studio_runs ORDER BY run_id").fetchall() == [
            ("b",),
            ("r",),
        ]
    finally:
        conn.close()
