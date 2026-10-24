"""Private closed-WAL previews under concurrent writers and namespace changes."""

import errno
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from nexura_runtime import retention
from nexura_runtime.studio.store import StudioStore


def _closed_wal(tmp_path):
    directory = tmp_path / "database"
    directory.mkdir()
    db = directory / "service.sqlite3"
    service = StudioStore(db)
    with service._conn:
        service._conn.execute(
            "INSERT INTO nexura_studio_runs "
            "(run_id,spec_hash,spec_json,status,input_state_json,current_state_json,"
            "created_at,updated_at) VALUES ('r','h','{}','completed','{}','{}',0,0)"
        )
    service._close_sync()
    assert sorted(p.name for p in directory.iterdir()) == [db.name]
    return db


def _snapshot(directory):
    return {
        p.name: (p.stat().st_ino, p.stat().st_mtime_ns, p.read_bytes()) for p in directory.iterdir()
    }


def _worker_code(monkeypatch, code):
    popen = retention.subprocess.Popen

    def launch(argv, *args, **kwargs):
        if any("_serve_database" in part for part in argv):
            argv = list(argv)
            argv[4] = "import nexura_runtime.retention as retention\n" + code + "\n" + argv[4]
        return popen(argv, *args, **kwargs)

    monkeypatch.setattr(retention.subprocess, "Popen", launch)


def test_default_snapshot_reports_real_candidates_without_touching_source(tmp_path):
    db = _closed_wal(tmp_path)
    before = _snapshot(db.parent)
    report = retention.apply_retention(retention.RetentionPolicy(30), db_path=db)
    assert not report.errors and report.dry_run and report.runs_deleted == 1
    assert _snapshot(db.parent) == before


def test_closed_wal_with_an_open_writer_is_refused_clearly(tmp_path):
    db = _closed_wal(tmp_path)
    before = _snapshot(db.parent)
    # Real SQLite RW fd; no first query or sidecar creation has occurred yet.
    writer = sqlite3.connect(db)
    try:
        with pytest.raises(retention.RetentionError) as failure:
            retention.apply_retention(retention.RetentionPolicy(30), db_path=db)
        assert "Read lease unavailable" in str(failure.value)
        assert "service is running" in str(failure.value)
        assert failure.value.report.errors and failure.value.report.runs_deleted == 0
        assert _snapshot(db.parent) == before
    finally:
        writer.close()


def test_snapshot_without_lease_support_is_refused_without_writing(tmp_path, monkeypatch):
    db = _closed_wal(tmp_path)
    before = _snapshot(db.parent)
    _worker_code(
        monkeypatch,
        """
import errno, fcntl
actual_fcntl = fcntl.fcntl
def unsupported(fd, command, *args):
    if command == fcntl.F_SETLEASE:
        raise OSError(errno.EOPNOTSUPP, 'test filesystem has no lease support')
    return actual_fcntl(fd, command, *args)
fcntl.fcntl = unsupported
""",
    )
    with pytest.raises(retention.RetentionError) as failure:
        retention.apply_retention(retention.RetentionPolicy(30), db_path=db)
    assert "private read-only preview" in str(failure.value)
    assert "Nothing was changed" in str(failure.value)
    assert "service is running" in str(failure.value)
    assert failure.value.report.errors and failure.value.report.runs_deleted == 0
    assert _snapshot(db.parent) == before


@pytest.mark.parametrize("action", ["write-open", "sidecar"])
def test_snapshot_refuses_a_real_lease_break_or_appeared_wal(tmp_path, monkeypatch, action):
    db = _closed_wal(tmp_path)
    raw = db.read_bytes()
    ready, done = tmp_path / "ready", tmp_path / "done"
    actor = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            """
import errno, json, os, signal, sys, time
from pathlib import Path
db, ready, done, action = sys.argv[1:]
triggered = False
def trigger(*args):
    global triggered
    triggered = True
signal.signal(signal.SIGUSR1, trigger)
Path(ready).write_text('ready')
deadline = time.monotonic() + 15
while not triggered:
    if time.monotonic() > deadline:
        raise RuntimeError('retention did not exercise the actor')
    time.sleep(.005)
if action == 'write-open':
    try:
        fd = os.open(db, os.O_RDWR | os.O_NONBLOCK)
    except OSError as exc:
        result = {'errno': exc.errno}
    else:
        os.close(fd)
        result = {'errno': 0}
else:
    Path(db + '-wal').write_bytes(b'independent actor WAL')
    result = {'wal_created': True}
Path(done).write_text(json.dumps(result))
""",
            str(db),
            str(ready),
            str(done),
            action,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and actor.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "independent actor did not start"
        info = db.stat()
        _worker_code(
            monkeypatch,
            f"""
import os, signal, time
from pathlib import Path
actual_read = os.read
fired = False
def interleaved_read(fd, size):
    global fired
    info = os.fstat(fd)
    if not fired and (info.st_dev, info.st_ino) == {(info.st_dev, info.st_ino)!r}:
        fired = True
        os.kill({actor.pid}, signal.SIGUSR1)
        deadline = time.monotonic() + 5
        while not Path({str(done)!r}).exists() and time.monotonic() < deadline:
            time.sleep(.005)
        if not Path({str(done)!r}).exists():
            raise RuntimeError('independent actor did not finish')
    return actual_read(fd, size)
os.read = interleaved_read
""",
        )
        with pytest.raises(retention.RetentionError) as failure:
            retention.apply_retention(retention.RetentionPolicy(30), db_path=db)
        assert failure.value.report.errors and failure.value.report.runs_deleted == 0
        assert "snapshot" in str(failure.value) or "lease break" in str(failure.value)
        assert db.read_bytes() == raw
        result = json.loads(done.read_text())
        if action == "write-open":
            assert result["errno"] == errno.EAGAIN  # Kernel lease actually blocked the writer.
        else:
            assert result["wal_created"] is True
            assert Path(str(db) + "-wal").read_bytes() == b"independent actor WAL"
        actor.wait(timeout=5)
        assert actor.returncode == 0
    finally:
        if actor.poll() is None:
            actor.kill()
        actor.communicate(timeout=5)


def test_source_change_after_capture_invalidates_private_preview(tmp_path, monkeypatch):
    db = _closed_wal(tmp_path)
    original = retention._validate_schemas
    changed = False

    def live_change(conn):
        nonlocal changed
        if not changed:
            changed = True
            with sqlite3.connect(db) as writer:
                writer.execute("UPDATE nexura_studio_runs SET status='running'")
        return original(conn)

    monkeypatch.setattr(retention, "_validate_schemas", live_change)
    with pytest.raises(retention.RetentionError) as failure:
        retention.apply_retention(retention.RetentionPolicy(30), db_path=db)
    assert changed and failure.value.report.errors
    assert failure.value.report.runs_deleted == failure.value.report.events_deleted == 0
    with sqlite3.connect(db) as observer:
        assert observer.execute("SELECT run_id,status FROM nexura_studio_runs").fetchall() == [
            ("r", "running")
        ]
