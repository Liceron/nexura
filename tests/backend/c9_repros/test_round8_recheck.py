"""Round 7 independent C9 re-check repros (Claude).

Assertions state required safety properties; a failing assertion is a
reproduced defect. SQL, SQLite I/O, Landlock, identity checks and worker
processes are real. Hooks only perform the hostile filesystem action at a
chosen moment and then call the original function. All victim data is
test-owned. Run with a basetemp in an owner-only directory chain (umask 077).
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

import nexura_runtime.retention as retention
from nexura_runtime.studio.store import StudioStore

OLD = time.time() - 100 * 86400


@pytest.fixture(autouse=True)
def _owner_only_umask():
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


def _insert(conn, rid="selected-run", status="completed"):
    conn.execute(
        "INSERT INTO nexura_studio_runs "
        "(run_id,spec_hash,spec_json,status,input_state_json,current_state_json,"
        "created_at,updated_at) VALUES (?,'review','{}',?,'{}','{}',?,?)",
        (rid, status, OLD, OLD),
    )


def _runs(db):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return [tuple(r) for r in conn.execute("SELECT run_id, status FROM nexura_studio_runs")]
    finally:
        conn.close()


def _delete_mode_db(directory: Path, *, status="completed", filler=False) -> Path:
    db = directory / "selected.sqlite3"
    store = StudioStore(db)
    with store._conn:
        _insert(store._conn, status=status)
    store._close_sync()
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
        if filler:
            conn.execute("CREATE TABLE review_filler (v BLOB)")
            conn.executemany("INSERT INTO review_filler VALUES (randomblob(3000))", [()] * 200)
            conn.commit()
    finally:
        conn.close()
    for suffix in ("-wal", "-shm", "-journal"):
        assert not Path(str(db) + suffix).exists()
    return db


def _live_wal_db(directory: Path):
    db = directory / "selected.sqlite3"
    store = StudioStore(db)
    with store._conn:
        _insert(store._conn)
    store._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return db, store


def _foreign(directory: Path):
    elsewhere = directory / "elsewhere"
    elsewhere.mkdir()
    foreign = elsewhere / "foreign.sqlite3"
    conn = sqlite3.connect(foreign)
    conn.execute("CREATE TABLE foreign_data (value TEXT)")
    conn.execute("INSERT INTO foreign_data VALUES ('precious foreign data')")
    conn.commit()
    conn.close()
    second = elsewhere / "foreign-second-name.sqlite3"
    os.link(foreign, second)
    return foreign, second, foreign.read_bytes()


def _worker_prefix(monkeypatch, code: str) -> list[bool]:
    """Prefix the real worker entry with a hostile actor (argv index 4 after -I)."""
    original = retention.subprocess.Popen
    launched = [False]

    def launch(arguments, *args, **kwargs):
        if any("_serve_database" in part for part in arguments):
            launched[0] = True
            arguments = list(arguments)
            assert arguments[1:4] == ["-I", "-B", "-c"]
            arguments[4] = (
                "import os, shutil, sqlite3\n"
                "import nexura_runtime.retention as retention\n"
                + code
                + "\nretention._serve_database()\n"
            )
        return original(arguments, *args, **kwargs)

    monkeypatch.setattr(retention.subprocess, "Popen", launch)
    return launched


def _swap_code(source: Path, target: Path, aside: Path | None, *, when: str) -> str:
    """when='connect' (apply) or 'deny' (dry run: just before Landlock)."""
    swap = (f"        os.rename({str(target)!r}, {str(aside)!r})\n" if aside else "") + (
        f"        os.rename({str(source)!r}, {str(target)!r})\n"
    )
    if when == "connect":
        return (
            "_connect = sqlite3.connect\n_done = False\n"
            "def _hostile_connect(*args, **kwargs):\n"
            "    global _done\n"
            "    if not _done:\n"
            "        _done = True\n" + swap + "    return _connect(*args, **kwargs)\n"
            "sqlite3.connect = _hostile_connect\n"
        )
    return (
        "_deny = retention._deny_filesystem_writes\n_done = False\n"
        "def _hostile_deny():\n"
        "    global _done\n"
        "    if not _done:\n"
        "        _done = True\n" + swap + "    return _deny()\n"
        "retention._deny_filesystem_writes = _hostile_deny\n"
    )


# ---------------------------------------------------------------------------
# 1. Retargeted lead R5-1 variant (only the argv index changes: 3 -> 4).
# ---------------------------------------------------------------------------


def test_lead_variant_retargeted_to_isolated_argv(tmp_path, monkeypatch):
    db, service = _live_wal_db(tmp_path)
    foreign = tmp_path / "foreign.sqlite3"
    conn = sqlite3.connect(foreign)
    conn.execute("CREATE TABLE foreign_data (value TEXT)")
    conn.execute("INSERT INTO foreign_data VALUES ('precious foreign data')")
    conn.commit()
    conn.close()
    expected = foreign.read_bytes()
    wal = Path(str(db) + "-wal")
    code = f"""
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
"""
    launched = _worker_prefix(monkeypatch, code)
    try:
        try:
            report = retention.apply_retention(
                retention.RetentionPolicy(30), db_path=db, dry_run=False
            )
        except retention.RetentionError as exc:
            report = exc.report
        assert launched[0] and not foreign.exists(), "sidecar substitution was not exercised"
        observed = wal.read_bytes() if wal.exists() else None
        assert observed == expected, report.summary()
        assert report.errors
    finally:
        service._close_sync()


# ---------------------------------------------------------------------------
# 2. Substitution after the final pre-open recheck, immediately before connect.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dry_run", [False, True], ids=["apply", "dry-run"])
@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_sidecar_swap_after_final_recheck_stops(tmp_path, monkeypatch, suffix, dry_run):
    db, service = _live_wal_db(tmp_path)
    foreign, second, expected = _foreign(tmp_path)
    slot = Path(str(db) + suffix)
    launched = _worker_prefix(
        monkeypatch,
        _swap_code(
            foreign, slot, tmp_path / ("original" + suffix), when="deny" if dry_run else "connect"
        ),
    )
    try:
        try:
            report = retention.apply_retention(
                retention.RetentionPolicy(30), db_path=db, dry_run=dry_run
            )
        except retention.RetentionError as exc:
            report = exc.report
        observed = second.read_bytes()
        (tmp_path / "evidence.json").write_text(
            json.dumps(
                {
                    "summary": report.summary(),
                    "bytes": [len(expected), len(observed)],
                    "magic_after": observed[:16].hex(),
                },
                indent=2,
            )
        )
        assert launched[0] and not foreign.exists(), "substitution not exercised"
        assert report.errors and report.runs_deleted == report.events_deleted == 0
        assert service._conn.execute("SELECT run_id FROM nexura_studio_runs").fetchall() == [
            ("selected-run",)
        ]
        if suffix == "-wal" or dry_run:
            # Not covered by the documented apply-mode SHM limit.
            assert observed == expected, report.summary()
    finally:
        service._close_sync()


# ---------------------------------------------------------------------------
# 3. A rollback journal appearing after the pre-open check, before connect.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state, dry_run",
    [("delete", True), ("delete", False), ("wal-live", True), ("wal-live", False)],
)
def test_journal_appearing_before_first_read_stops_without_change(
    tmp_path, monkeypatch, state, dry_run
):
    service = None
    if state == "delete":
        db = _delete_mode_db(tmp_path)
    else:
        db, service = _live_wal_db(tmp_path)
    foreign, second, expected = _foreign(tmp_path)
    journal = Path(str(db) + "-journal")
    launched = _worker_prefix(
        monkeypatch, _swap_code(foreign, journal, None, when="deny" if dry_run else "connect")
    )
    try:
        try:
            report = retention.apply_retention(
                retention.RetentionPolicy(30), db_path=db, dry_run=dry_run
            )
        except retention.RetentionError as exc:
            report = exc.report
        (tmp_path / "evidence.json").write_text(
            json.dumps(
                {
                    "summary": report.summary(),
                    "journal_exists": journal.exists(),
                    "second_bytes": len(second.read_bytes()),
                },
                indent=2,
            )
        )
        assert launched[0] and not foreign.exists(), "substitution not exercised"
        assert second.read_bytes() == expected
        assert journal.exists(), "the substituted -journal name was unlinked: " + report.summary()
        assert report.errors, "an unvalidated -journal must stop the pass"
        assert report.runs_deleted == report.events_deleted == 0
    finally:
        if service is not None:
            service._close_sync()


# ---------------------------------------------------------------------------
# 4. Root-owned sticky directory: another user can create a free -journal name.
#    The same uid stands in for that user (no second account is available);
#    the crafted journal is mode 0666, as another user would make it.
# ---------------------------------------------------------------------------


def _crafted_journal(source: Path, workdir: Path) -> bytes:
    """A real hot journal whose original pages mark the protected run completed."""
    copy = workdir / "attacker-copy.sqlite3"
    shutil.copyfile(source, copy)
    conn = sqlite3.connect(copy)
    conn.execute("UPDATE nexura_studio_runs SET status='completed'")
    conn.commit()
    conn.close()
    code = (
        "import os, sqlite3\n"
        f"c = sqlite3.connect({str(copy)!r}, isolation_level=None)\n"
        "c.execute('PRAGMA cache_size=2')\n"
        "c.execute('BEGIN')\n"
        "c.execute(\"UPDATE nexura_studio_runs SET status='x'\")\n"
        "c.execute('UPDATE review_filler SET v=randomblob(3000)')\n"
        "os._exit(9)\n"
    )
    subprocess.run([sys.executable, "-I", "-c", code], check=False)
    journal = Path(str(copy) + "-journal")
    assert journal.stat().st_size > 512
    return journal.read_bytes()


def _check_caller_then_exercise_worker_directory_guard(db, control, monkeypatch):
    # The new caller guard correctly refuses BEFORE Popen. Prove that first.
    # Then inject a caller-only permission-check fault and hold an unrelated RW
    # fd to select isolation. The fresh worker keeps the REAL final-directory
    # guard. This retains the reviewer's actual-launched assertion while testing
    # both refusals; no SQL/worker outcome is stubbed and no source bypass exists.
    with pytest.raises(retention.RetentionError) as refused:
        retention.apply_retention(retention.RetentionPolicy(30), db_path=db, dry_run=False)
    assert refused.value.report.errors
    assert refused.value.report.runs_deleted == refused.value.report.events_deleted == 0
    check = retention._check_directory_permissions
    apply = retention.apply_retention

    def caller_ancestor_check(path, info, *, final):
        return check(path, info, final=False if path == db.parent else final)

    def select_real_worker(*args, **kwargs):
        with control.open("w+b"):
            return apply(*args, **kwargs)

    monkeypatch.setattr(retention, "_check_directory_permissions", caller_ancestor_check)
    monkeypatch.setattr(retention, "apply_retention", select_real_worker)


def test_sticky_directory_journal_cannot_unprotect_running_run(tmp_path, monkeypatch):
    sticky = Path("/tmp")
    info = sticky.stat()
    if info.st_uid != 0 or not info.st_mode & 0o1000 or not info.st_mode & 0o002:
        pytest.skip("requires a root-owned world-writable sticky /tmp")
    prepared = _delete_mode_db(tmp_path, status="running", filler=True)
    crafted_file = tmp_path / "crafted-journal.bin"
    crafted_file.write_bytes(_crafted_journal(prepared, tmp_path))
    db = sticky / f"c9r7-review-{uuid.uuid4().hex}.sqlite3"
    shutil.move(prepared, db)
    journal = Path(str(db) + "-journal")
    _check_caller_then_exercise_worker_directory_guard(db, tmp_path / "force-worker", monkeypatch)
    code = f"""
_execute = retention._RetentionConnection.execute
_planted = False
def _hostile_execute(self, statement, parameters=()):
    global _planted
    if not _planted and statement.strip().upper() == "BEGIN IMMEDIATE":
        _planted = True
        fd = os.open({str(journal)!r}, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        with open({str(crafted_file)!r}, "rb") as source:
            os.write(fd, source.read())
        os.close(fd)
        os.chmod({str(journal)!r}, 0o666)
    return _execute(self, statement, parameters)
retention._RetentionConnection.execute = _hostile_execute
"""
    launched = _worker_prefix(monkeypatch, code)
    try:
        assert _runs(db) == [("selected-run", "running")]
        try:
            report = retention.apply_retention(
                retention.RetentionPolicy(30), db_path=db, dry_run=False
            )
        except retention.RetentionError as exc:
            report = exc.report
        after = _runs(db)
        (tmp_path / "evidence.json").write_text(
            json.dumps(
                {
                    "summary": report.summary(),
                    "runs_after": after,
                    "journal_exists": journal.exists(),
                },
                indent=2,
            )
        )
        assert launched[0]
        assert after == [("selected-run", "running")], (
            "an unvalidated journal in a sticky directory changed or removed a protected "
            "run: " + report.summary()
        )
        assert report.errors
    finally:
        for suffix in ("", "-journal", "-wal", "-shm"):
            Path(str(db) + suffix).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 5. Directory-chain bypass attempts through symlinks.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("via", ["ancestor-symlink", "database-symlink", "dotdot-through-symlink"])
@pytest.mark.parametrize("dry_run", [True, False])
def test_symlinked_paths_are_refused_without_change(tmp_path, via, dry_run):
    real = tmp_path / "real"
    real.mkdir()
    db = _delete_mode_db(real)
    before = db.read_bytes()
    if via == "ancestor-symlink":
        (tmp_path / "link").symlink_to(real, target_is_directory=True)
        target = tmp_path / "link" / db.name
    elif via == "database-symlink":
        (tmp_path / "db-link.sqlite3").symlink_to(db)
        target = tmp_path / "db-link.sqlite3"
    else:
        (tmp_path / "link").symlink_to(real, target_is_directory=True)
        (real / "sub").mkdir()
        target = tmp_path / "link" / "sub" / ".." / db.name
    with pytest.raises(retention.RetentionError) as failure:
        retention.apply_retention(retention.RetentionPolicy(30), db_path=target, dry_run=dry_run)
    assert failure.value.report.errors
    assert failure.value.report.runs_deleted == 0
    assert db.read_bytes() == before and _runs(db) == [("selected-run", "completed")]


# ---------------------------------------------------------------------------
# 6. Dry-run neutrality: whole-directory snapshot (names, bytes, inode, mtime).
# ---------------------------------------------------------------------------


def _snapshot(directory: Path):
    return {
        p.name: (p.stat().st_ino, p.stat().st_size, p.stat().st_mtime_ns, p.read_bytes())
        for p in sorted(directory.iterdir())
        if p.is_file()
    }


@pytest.mark.parametrize("state", ["delete", "wal-live"])
def test_dry_run_leaves_database_directory_identical(tmp_path, state):
    service = None
    if state == "delete":
        db = _delete_mode_db(tmp_path)
    else:
        db, service = _live_wal_db(tmp_path)
    try:
        before = _snapshot(tmp_path)
        report = retention.apply_retention(retention.RetentionPolicy(30), db_path=db)
        assert report.dry_run and not report.errors and report.runs_deleted == 1
        assert _snapshot(tmp_path) == before
    finally:
        if service is not None:
            service._close_sync()


def test_default_dry_run_of_closed_wal_database(tmp_path):
    """Usability: the shipped StudioStore leaves a closed WAL database at rest."""
    db = tmp_path / "selected.sqlite3"
    store = StudioStore(db)
    with store._conn:
        _insert(store._conn)
    store._close_sync()
    before = _snapshot(tmp_path)
    try:
        report = retention.apply_retention(retention.RetentionPolicy(30), db_path=db)
    except retention.RetentionError as exc:
        report = exc.report
    assert _snapshot(tmp_path) == before
    (tmp_path / "evidence.json").write_text(json.dumps({"summary": report.summary()}))
    assert not report.errors, "default preview of an at-rest Studio database fails: " + (
        report.summary()
    )
