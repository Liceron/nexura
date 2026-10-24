"""Real sidecar namespace change between validation and SQLite opening."""

import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

import nexura_runtime.retention as retention
from nexura_runtime.studio.store import StudioStore


def test_foreign_database_substituted_after_wal_validation_is_untouched(tmp_path, monkeypatch):
    db = tmp_path / "selected.sqlite3"
    service = StudioStore(db)
    with service._conn:
        service._conn.execute(
            "INSERT INTO nexura_studio_runs "
            "(run_id,spec_hash,spec_json,status,input_state_json,current_state_json,"
            "created_at,updated_at) "
            "VALUES ('selected-run','review','{}','completed','{}','{}',?,?)",
            (time.time() - 100 * 86400, time.time() - 100 * 86400),
        )
    foreign = tmp_path / "foreign.sqlite3"
    connection = sqlite3.connect(foreign)
    connection.execute("CREATE TABLE foreign_data (value TEXT)")
    connection.execute("INSERT INTO foreign_data VALUES ('precious foreign data')")
    connection.commit()
    connection.close()
    expected = foreign.read_bytes()
    foreign_stat = foreign.stat()
    # Keep the real WAL producer open, with all selected schema/pages checkpointed.
    service._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    wal = Path(str(db) + "-wal")
    assert wal.exists()
    original = retention.subprocess.Popen
    injected = False

    def launch(arguments, *args, **kwargs):
        nonlocal injected
        if any("_serve_database" in part for part in arguments):
            injected = True
            arguments = list(arguments)
            arguments[4] = f"""
import os
from pathlib import Path
import nexura_runtime.retention as retention
validate = retention._validate_sidecar
changed = False
def validate_then_swap(parent, name, suffix, expected, *, wal_validated):
    global changed
    validate(parent, name, suffix, expected, wal_validated=wal_validated)
    if suffix == "-wal" and not changed:
        os.rename({str(wal)!r}, {str(tmp_path / "original.wal")!r})
        os.rename({str(foreign)!r}, {str(wal)!r})
        changed = True
retention._validate_sidecar = validate_then_swap
retention._serve_database()
"""
        return original(arguments, *args, **kwargs)

    monkeypatch.setattr(retention.subprocess, "Popen", launch)
    report = None
    try:
        try:
            report = retention.apply_retention(
                retention.RetentionPolicy(30), db_path=db, dry_run=False
            )
        except retention.RetentionError as exc:
            report = exc.report
        assert injected and not foreign.exists(), "sidecar substitution was not exercised"
        observed = wal.read_bytes() if wal.exists() else None
        observed_stat = wal.stat() if wal.exists() else None
        (tmp_path / "evidence.json").write_text(
            json.dumps(
                {
                    "report": report.summary(),
                    "foreign_bytes_before": len(expected),
                    "foreign_bytes_after": len(observed) if observed is not None else None,
                    "foreign_original_device_inode": [foreign_stat.st_dev, foreign_stat.st_ino],
                    "wal_final_device_inode": [observed_stat.st_dev, observed_stat.st_ino]
                    if observed_stat
                    else None,
                    "foreign_before_magic": repr(expected[:16]),
                    "foreign_after_magic": repr(observed[:16]) if observed is not None else None,
                },
                indent=2,
            )
        )
        if observed is not None:
            (tmp_path / "damaged-foreign-evidence.bin").write_bytes(observed)
        assert observed == expected, (
            "Foreign database moved into WAL slot after validation was changed by retention: "
            + report.summary()
        )
        assert report.errors, "Detected sidecar identity change must be reported"
    finally:
        # Assertion evidence above is captured before closing our independent producer.
        service._close_sync()


def _live_database(tmp_path):
    db = tmp_path / "service.sqlite3"
    service = StudioStore(db)
    with service._conn:
        service._conn.execute(
            "INSERT INTO nexura_studio_runs "
            "(run_id,spec_hash,spec_json,status,input_state_json,current_state_json,"
            "created_at,updated_at) VALUES ('r','h','{}','completed','{}','{}',?,?)",
            (time.time() - 100 * 86400,) * 2,
        )
    service._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return db, service


def _worker_code(monkeypatch, code):
    original = retention.subprocess.Popen

    def launch(arguments, *args, **kwargs):
        if any("_serve_database" in part for part in arguments):
            arguments = list(arguments)
            arguments[4] = (
                "import json, os, sqlite3\nfrom pathlib import Path\n"
                "import nexura_runtime.retention as retention\n"
                + code
                + "\nretention._serve_database()\n"
            )
        return original(arguments, *args, **kwargs)

    monkeypatch.setattr(retention.subprocess, "Popen", launch)


@pytest.mark.parametrize("where", ["caller", "worker"])
def test_wal_without_proc_descriptor_proof_fails_closed(tmp_path, monkeypatch, where):
    db, service = _live_database(tmp_path)
    paths = [db, Path(str(db) + "-wal"), Path(str(db) + "-shm")]
    before = {path: path.read_bytes() for path in paths}
    try:
        if where == "caller":
            original = os.listdir

            def missing_proc(path):
                if str(path) == "/proc/self/fd":
                    raise FileNotFoundError("simulated unavailable /proc")
                return original(path)

            monkeypatch.setattr(retention.os, "listdir", missing_proc)
        else:
            _worker_code(
                monkeypatch,
                """
original_listdir = os.listdir
def missing_proc(path):
    if str(path) == "/proc/self/fd":
        raise FileNotFoundError("simulated unavailable /proc")
    return original_listdir(path)
os.listdir = missing_proc
""",
            )
        with pytest.raises(retention.RetentionError) as failure:
            retention.apply_retention(retention.RetentionPolicy(30), db_path=db, dry_run=False)
        report = failure.value.report
        assert report.errors and "/proc/self/fd" in str(failure.value)
        assert "WAL-mode" in str(failure.value) and "refused" in str(failure.value)
        assert report.runs_deleted == report.events_deleted == report.bytes_freed == 0
        assert all(path.read_bytes() == raw for path, raw in before.items())
        assert service._conn.execute("SELECT run_id FROM nexura_studio_runs").fetchall() == [("r",)]
    finally:
        service._close_sync()


@pytest.mark.parametrize("phase", ["begin", "commit"])
def test_missing_shm_descriptor_abandons_worker_before_mutation_or_commit(
    tmp_path, monkeypatch, phase
):
    db, service = _live_database(tmp_path)
    marker = tmp_path / "unverified-write"
    code = f"phase = {phase!r}\nmarker = Path({str(marker)!r})\n"
    code += f"shm_stat = Path({str(db) + '-shm'!r}).stat()\n"
    code += """
shm_identity = (shm_stat.st_dev, shm_stat.st_ino)
unprovable = False
actual_execute = retention._RetentionConnection.execute
actual_commit = retention._RetentionConnection.commit
actual_probe = retention._writable_file_descriptors
actual_loads = json.loads
commit_requests = commit_calls = 0
def execute(self, statement, parameters=()):
    global unprovable
    if phase == "begin" and statement.lstrip().upper().startswith(("CREATE", "DELETE")):
        marker.write_text(statement)
    cursor = actual_execute(self, statement, parameters)
    if phase == "begin" and statement == "BEGIN IMMEDIATE":
        unprovable = True
    return cursor
def commit(self):
    global commit_calls
    commit_calls += 1
    if phase == "commit" and commit_calls == 2:
        marker.write_text("unverified COMMIT")
    return actual_commit(self)
def loads(value, *args, **kwargs):
    global commit_requests, unprovable
    request = actual_loads(value, *args, **kwargs)
    if isinstance(request, dict) and request.get("operation") == "commit":
        commit_requests += 1
        if phase == "commit" and commit_requests == 2:
            unprovable = True
    return request
def probe():
    descriptors = actual_probe()
    if unprovable:
        # Simulate /proc losing proof of one actual SQLite descriptor. All SQL,
        # BEGIN/COMMIT calls and their returned rows remain real.
        return {fd: identity for fd, identity in descriptors.items() if identity != shm_identity}
    return descriptors
retention._RetentionConnection.execute = execute
retention._RetentionConnection.commit = commit
retention._writable_file_descriptors = probe
json.loads = loads
"""
    _worker_code(monkeypatch, code)
    try:
        with pytest.raises(retention.RetentionError) as failure:
            retention.apply_retention(retention.RetentionPolicy(30), db_path=db, dry_run=False)
        assert "main/WAL/SHM descriptors" in str(failure.value)
        assert failure.value.report.errors
        assert failure.value.report.runs_deleted == failure.value.report.events_deleted == 0
        assert not marker.exists()
        assert service._conn.execute("SELECT run_id FROM nexura_studio_runs").fetchall() == [("r",)]
    finally:
        service._close_sync()
