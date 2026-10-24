"""Additional C9 boundaries. Failed assertions express required safety."""

import asyncio
import contextlib
import json
import os
import sqlite3
import threading
import time
from dataclasses import asdict
from pathlib import Path

import pytest
from nexura_agents.contracts.execution import ExecutionId
from nexura_agents.execution_store import SQLiteExecutionStore

import nexura_runtime.retention as retention
from nexura_runtime.observability.models import EventRecord
from nexura_runtime.studio.store import StudioStore

OLD = time.time() - 100 * 86400
PROTECTED = [
    "running",
    "paused",
    "pending_approval",
    "waiting_for_input",
    "interrupted",
    "effect_unknown",
]


def query(db, statement, args=()):
    conn = sqlite3.connect(db)
    try:
        with conn:
            return conn.execute(statement, args).fetchall()
    finally:
        conn.close()


def initialize(path):
    store = StudioStore(path)
    store._close_sync()
    return path


def add_run(db, rid="r", status="completed", **extra):
    values = dict(
        run_id=rid,
        spec_hash="review",
        spec_json="{}",
        status=status,
        input_state_json="{}",
        current_state_json="{}",
        created_at=OLD,
        updated_at=OLD,
    )
    values.update(extra)
    query(
        db,
        f"INSERT INTO nexura_studio_runs ({','.join(values)}) "
        f"VALUES ({','.join('?' for _ in values)})",
        tuple(values.values()),
    )


@pytest.fixture
def db(tmp_path):
    return initialize(tmp_path / "service.sqlite3")


def has_run(db, rid="r"):
    return bool(query(db, "SELECT 1 FROM nexura_studio_runs WHERE run_id=?", (rid,)))


def exported_trace(db, root, rid="r", parent=None):
    root.mkdir(parents=True, exist_ok=True)
    content = (
        EventRecord(
            seq_num=1, run_id=rid, parent_run_id=parent, timestamp=OLD, event_type="LOCAL_REVIEW"
        ).model_dump_json()
        + "\n"
    )
    (root / "closed.jsonl").write_text(content)
    return retention.write_retention_artifact(
        content, db_path=db, data_dir=root, relative_path="closed.jsonl", run_id=rid, kind="traces"
    )


def assert_eligible(report, path, raw):
    assert path.read_bytes() == raw
    entries = [item for item in report.eligible_files if item.path == str(path)]
    assert len(entries) == 1
    assert entries[0].run_id == "r" and entries[0].size_bytes == len(raw)
    assert entries[0].age_days > 30
    assert entries[0].reason == "older than cutoff; terminal, unprotected run; manual removal only"
    assert report.files_deleted == 0


def test_database_ancestor_swap_cannot_redirect_retention(tmp_path, monkeypatch):
    parent = tmp_path / "selected"
    parent.mkdir()
    db = initialize(parent / "service.sqlite3")
    add_run(db, "selected-run")
    outside = tmp_path / "other-workspace"
    outside.mkdir()
    victim = initialize(outside / "service.sqlite3")
    add_run(victim, "outside-run")
    original = sqlite3.connect
    isolate = retention._isolated_database
    swapped = False

    def swap():
        nonlocal swapped
        if not swapped:
            parent.rename(tmp_path / "pinned-selected")
            parent.symlink_to(outside, target_is_directory=True)
            swapped = True

    def connect(path, *args, **kwargs):
        if str(path).startswith(db.as_uri()):
            swap()
        return original(path, *args, **kwargs)

    @contextlib.contextmanager
    def worker(path, authorised):
        # Ambient handles may select isolation before the caller opens SQLite.
        # Schedule the same real actor at that handoff; do not bypass checks.
        swap()
        with isolate(path, authorised) as connection:
            yield connection

    monkeypatch.setattr(retention.sqlite3, "connect", connect)
    monkeypatch.setattr(retention, "_isolated_database", worker)
    try:
        report = retention.apply_retention(
            retention.RetentionPolicy(runs_days=30), db_path=db, dry_run=False
        )
    except retention.RetentionError:
        report = None
    assert swapped
    assert has_run(victim, "outside-run"), (
        f"Ancestor swap redirected SQLite connection to another workspace: {asdict(report)}"
    )


def test_registered_parent_moved_out_after_last_validation_is_retained(db, tmp_path, monkeypatch):
    add_run(db)
    root = tmp_path / "exports"
    path = exported_trace(db, root)
    raw = path.read_bytes()
    moved = tmp_path / "outside-policy"
    original = os.unlink
    swapped = False

    def unlink(name, *args, **kwargs):
        nonlocal swapped
        if name == "artifact" and not swapped:
            root.rename(moved)
            root.mkdir()
            swapped = True
        return original(name, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", unlink)
    report = retention.apply_retention(
        retention.RetentionPolicy(traces_days=30), db_path=db, data_dir=root, dry_run=False
    )
    assert not swapped and not moved.exists()
    assert_eligible(report, path, raw)


def test_fresh_quarantined_replacement_not_deleted(db, tmp_path, monkeypatch):
    add_run(db)
    root = tmp_path / "exports"
    path = exported_trace(db, root)
    raw = path.read_bytes()
    original = os.unlink
    replaced = False
    saved = tmp_path / "old-owned.jsonl"

    def unlink(name, *args, **kwargs):
        nonlocal replaced
        if name == "artifact" and not replaced:
            # A same-UID exporter can reach the private directory; 0700 does not
            # protect against another process belonging to the service operator.
            private = kwargs["dir_fd"]
            os.rename("artifact", saved, src_dir_fd=private)
            fd = os.open("artifact", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=private)
            try:
                os.write(fd, b"brand-new unrelated content")
            finally:
                os.close(fd)
            replaced = True
        return original(name, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", unlink)
    report = retention.apply_retention(
        retention.RetentionPolicy(traces_days=30), db_path=db, data_dir=root, dry_run=False
    )
    assert not replaced and not saved.exists()
    assert_eligible(report, path, raw)


def test_missing_journal_authority_does_not_authorize_deletion(db):
    add_run(db, journal_workspace="default")
    assert not query(db, "SELECT 1 FROM sqlite_master WHERE name='nexura_exec_runs'")
    report = retention.apply_retention(
        retention.RetentionPolicy(runs_days=30), db_path=db, dry_run=False
    )
    assert has_run(db), (
        f"Deleted journaled run without consulting its absent authority: {asdict(report)}"
    )


def test_registered_trace_parent_dependency_protects_child_artifacts(db, tmp_path):
    add_run(db, "parent", status="pending_approval")
    add_run(db, "opaque-child")
    root = tmp_path / "exports"
    path = exported_trace(db, root, rid="opaque-child", parent="parent")
    report = retention.apply_retention(
        retention.RetentionPolicy(30), db_path=db, data_dir=root, dry_run=False
    )
    assert has_run(db, "opaque-child") and path.exists(), (
        f"Ignored parent_run_id in registered trace: {asdict(report)}"
    )


@pytest.mark.parametrize("status", PROTECTED)
def test_checkpoint_protected_states_veto_terminal_projection(db, status):
    add_run(db)
    query(
        db,
        "INSERT INTO nexura_workflow_checkpoints "
        "(workflow_run_id,spec_version,spec_hash,state_json,step_count,status,updated_at) "
        "VALUES ('r',1,'h','{}',1,?,?)",
        (status, OLD),
    )
    before = query(db, "SELECT * FROM nexura_workflow_checkpoints")
    retention.apply_retention(retention.RetentionPolicy(runs_days=0), db_path=db, dry_run=False)
    assert has_run(db)
    assert query(db, "SELECT * FROM nexura_workflow_checkpoints") == before


@pytest.mark.parametrize("status", PROTECTED)
def test_journal_protected_states_veto_terminal_projection(db, status):
    add_run(db)

    async def create():
        store = SQLiteExecutionStore(db)
        try:
            await store.init_db()
            return await store.create(
                ExecutionId("default", "r"), owner="review", definition_digest="review"
            )
        finally:
            await store.close()

    data = asdict(asyncio.run(create()))
    data.update(status=status, updated_at=OLD, lease_owner=None, lease_expires_at=None)
    query(
        db,
        "UPDATE nexura_exec_runs SET status=?,lease_owner=NULL,lease_expires_at=NULL,run_json=?",
        (status, json.dumps(data)),
    )
    before = query(db, "SELECT * FROM nexura_exec_runs")
    retention.apply_retention(retention.RetentionPolicy(runs_days=0), db_path=db, dry_run=False)
    assert has_run(db)
    assert query(db, "SELECT * FROM nexura_exec_runs") == before


def test_store_normal_concurrent_appends_have_contiguous_sequences(db):
    # A legacy unowned running fixture would correctly gain a recovery event on
    # open; use a settled parent to isolate sequence allocation and appends.
    add_run(db)
    stores = [StudioStore(db) for _ in range(4)]
    barrier = threading.Barrier(4)
    errors = []

    def worker(store, worker_id):
        try:
            barrier.wait(timeout=5)
            for i in range(30):
                store._record_run_event_sync("r", "NORMAL", data={"worker": worker_id, "index": i})
        except BaseException as exc:
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(store, i)) for i, store in enumerate(stores)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(15)
        assert not errors and not any(t.is_alive() for t in threads), errors
        snapshot = stores[0]._get_run_sync("r")
        assert [e["seq"] for e in snapshot["events"]] == list(range(1, 121))
        assert not stores[0]._conn.in_transaction
    finally:
        for store in stores:
            store._close_sync()


def test_store_bad_json_read_releases_its_owned_snapshot(db):
    add_run(db, current_state_json="invalid json")
    store = StudioStore(db)
    try:
        with pytest.raises(json.JSONDecodeError):
            store._get_run_sync("r")
        assert not store._conn.in_transaction
        store._conn.execute("BEGIN IMMEDIATE")
        store._conn.rollback()
    finally:
        store._close_sync()


def test_store_existing_caller_write_transaction_not_rolled_back(db):
    add_run(db)
    store = StudioStore(db)
    try:
        store._conn.execute("BEGIN IMMEDIATE")
        store._conn.execute("UPDATE nexura_studio_runs SET error='caller update' WHERE run_id='r'")
        with contextlib.suppress(sqlite3.OperationalError):
            store._record_run_event_sync("r", "NORMAL")
        assert query(db, "SELECT error FROM nexura_studio_runs")[0][0] == "caller update" or (
            store._conn.in_transaction
            and store._conn.execute("SELECT error FROM nexura_studio_runs").fetchone()[0]
            == "caller update"
        ), "Nested BEGIN IMMEDIATE rolled back unrelated caller write"
    finally:
        store._conn.rollback()
        store._close_sync()


def test_registered_export_remains_cleanable_after_shorter_run_retention(db, tmp_path, monkeypatch):
    add_run(db)
    root = tmp_path / "exports"
    path = exported_trace(db, root)
    raw = path.read_bytes()
    # The export is 100 days old: expire the run at 30 days but keep exports
    # until 120 days. A subsequent pass should be able to finish its cleanup.
    first = retention.apply_retention(
        retention.RetentionPolicy(runs_days=30, traces_days=120),
        db_path=db,
        data_dir=root,
        dry_run=False,
    )
    assert path.exists() and first.traces_deleted == 0
    assert first.runs_deleted == 1 and not has_run(db)
    assert path.read_bytes() == raw and not first.eligible_files
    from types import SimpleNamespace

    future = time.time() + 100 * 86400
    monkeypatch.setattr(retention, "time", SimpleNamespace(time=lambda: future))
    second = retention.apply_retention(
        retention.RetentionPolicy(runs_days=30, traces_days=120),
        db_path=db,
        data_dir=root,
        dry_run=False,
    )
    assert_eligible(second, path, raw)
    assert not second.errors


@pytest.mark.parametrize(
    "stamp", [float("inf"), float("-inf"), "nan", "invalid", "2030-01-01T00:00:00", -1]
)
def test_ambiguous_terminal_timestamp_fails_closed(db, stamp):
    add_run(db, updated_at=stamp)
    report = retention.apply_retention(
        retention.RetentionPolicy(runs_days=0), db_path=db, dry_run=False
    )
    assert has_run(db) and report.runs_deleted == 0


def test_registered_mixed_age_trace_preserved_and_dry_run_does_not_quarantine(db, tmp_path):
    add_run(db)
    root = tmp_path / "exports"
    root.mkdir()
    content = (
        "\n".join(
            EventRecord(
                seq_num=i + 1, run_id="r", timestamp=stamp, event_type="LOCAL_REVIEW"
            ).model_dump_json()
            for i, stamp in enumerate([OLD, time.time() + 86400])
        )
        + "\n"
    )
    (root / "mixed.jsonl").write_text(content)
    path = retention.write_retention_artifact(
        content, db_path=db, data_dir=root, relative_path="mixed.jsonl", run_id="r", kind="traces"
    )
    before = path.read_bytes()
    for dry in (True, False):
        report = retention.apply_retention(
            retention.RetentionPolicy(traces_days=30), db_path=db, data_dir=root, dry_run=dry
        )
        assert path.read_bytes() == before
        assert report.files_deleted == report.traces_deleted == 0
        assert list(root.iterdir()) == [path]


def test_registered_file_failure_reports_only_committed_database_success(db, tmp_path, monkeypatch):
    add_run(db)
    root = tmp_path / "exports"
    path = exported_trace(db, root)
    raw = path.read_bytes()
    original = os.unlink

    def deny(name, *args, **kwargs):
        if name == "artifact":
            raise PermissionError("review failed unlink")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", deny)
    report = retention.apply_retention(
        retention.RetentionPolicy(30), db_path=db, data_dir=root, dry_run=False
    )
    assert path.exists() and not has_run(db)
    assert report.runs_deleted == 1 and report.traces_deleted == report.files_deleted == 0
    assert report.bytes_freed == report.runs_bytes and not report.errors
    assert_eligible(report, path, raw)


def test_prechange_store_kept_existing_caller_write_transaction(db):
    import importlib.util
    import sys

    # Baseline check against the pre-change store, run where that copy is available.
    baseline = os.environ.get("NEXURA_C9_PRECHANGE_STORE")
    if not baseline or not Path(baseline).is_file():
        pytest.skip("set NEXURA_C9_PRECHANGE_STORE to the pre-change studio/store.py")
    backup = Path(baseline)
    spec = importlib.util.spec_from_file_location("c9_recheck_store_before", backup)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    add_run(db)
    store = module.StudioStore(db)
    try:
        store._conn.execute("BEGIN IMMEDIATE")
        store._conn.execute("UPDATE nexura_studio_runs SET error='caller update' WHERE run_id='r'")
        store._record_run_event_sync("r", "NORMAL")
        assert query(db, "SELECT error FROM nexura_studio_runs") == [("caller update",)]
        assert query(db, "SELECT event_type FROM nexura_studio_run_events") == [("NORMAL",)]
    finally:
        store._close_sync()
