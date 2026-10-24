"""Round 6 independent C9 review repros (Claude).

Every assertion states a required safety property, so a failing assertion is a
reproduced defect. All SQL, SQLite I/O, retention identity checks and worker
processes are real. Hooks only perform the hostile filesystem operation (a
rename into a sidecar slot, or planting files in the caller's working
directory) and then call the original function. All victim data is test-owned.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import suppress
from pathlib import Path

import pytest

import nexura_runtime.retention as retention
from nexura_runtime.studio.store import StudioStore

OLD = time.time() - 100 * 86400


def _insert_old_run(conn: sqlite3.Connection, rid: str = "selected-run") -> None:
    conn.execute(
        "INSERT INTO nexura_studio_runs "
        "(run_id,spec_hash,spec_json,status,input_state_json,current_state_json,"
        "created_at,updated_at) VALUES (?,'review','{}','completed','{}','{}',?,?)",
        (rid, OLD, OLD),
    )


def _runs(db: Path) -> list[str]:
    conn = sqlite3.connect(db)
    try:
        return [r[0] for r in conn.execute("SELECT run_id FROM nexura_studio_runs")]
    finally:
        conn.close()


def _rollback_database(tmp_path: Path) -> Path:
    """A selected database in SQLite's default rollback-journal (DELETE) mode.

    The shipped service repository (storage/repository.py) and repro capture
    connect without enabling WAL, so this mode is a supported input.
    """
    db = tmp_path / "selected.sqlite3"
    service = StudioStore(db)
    with service._conn:
        _insert_old_run(service._conn)
    service._close_sync()
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
    finally:
        conn.close()
    for suffix in ("-wal", "-shm", "-journal"):
        assert not Path(str(db) + suffix).exists()
    return db


def _foreign(tmp_path: Path) -> tuple[Path, Path, bytes]:
    """A foreign SQLite file that also has a second, pre-existing name.

    The second name lets the test observe the foreign inode after its first
    name has been moved into the selected database's sidecar slot.
    """
    elsewhere = tmp_path / "elsewhere"
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


# ---------------------------------------------------------------------------
# F1: rollback-journal mode. SQLite opens/deletes "-journal" by name and
# retention's descriptor proof covers only WAL/SHM.
# ---------------------------------------------------------------------------


def test_rollback_journal_substituted_after_begin_is_untouched(tmp_path, monkeypatch):
    db = _rollback_database(tmp_path)
    foreign, second, expected = _foreign(tmp_path)
    journal = Path(str(db) + "-journal")
    original = retention._protection
    swapped = False

    def protection_after_begin_immediate(*args, **kwargs):
        # Called inside _run_retention's BEGIN IMMEDIATE transaction, before
        # any DELETE and before SQLite has opened a rollback journal.
        nonlocal swapped
        if not swapped:
            os.rename(foreign, journal)
            swapped = True
        return original(*args, **kwargs)

    monkeypatch.setattr(retention, "_protection", protection_after_begin_immediate)
    try:
        report = retention.apply_retention(retention.RetentionPolicy(30), db_path=db, dry_run=False)
    except retention.RetentionError as exc:
        report = exc.report
    observed = second.read_bytes()
    (tmp_path / "evidence.json").write_text(
        json.dumps(
            {
                "summary": report.summary(),
                "foreign_bytes_before": len(expected),
                "foreign_bytes_after": len(observed),
                "foreign_magic_after": repr(observed[:16]),
                "journal_name_exists_after": journal.exists(),
                "selected_runs_after": _runs(db),
            },
            indent=2,
        )
    )
    assert swapped, "substitution was not exercised"
    assert observed == expected, (
        "Retention wrote rollback-journal data into a foreign file moved into the "
        "-journal slot after BEGIN IMMEDIATE: " + report.summary()
    )
    assert report.errors, "An unvalidated -journal identity must stop with an error"
    assert _runs(db) == ["selected-run"], "Identity ambiguity must delete nothing"


def _closed_wal_database(tmp_path: Path) -> Path:
    """A WAL database with no open producer and no sidecars at selection."""
    db = tmp_path / "selected.sqlite3"
    service = StudioStore(db)
    with service._conn:
        _insert_old_run(service._conn)
    service._close_sync()
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()
    for suffix in ("-wal", "-shm", "-journal"):
        assert not Path(str(db) + suffix).exists()
    return db


@pytest.mark.parametrize("mode", ["rollback", "wal-no-producer"])
def test_rollback_journal_substituted_before_dry_run_read_is_not_removed(
    tmp_path, monkeypatch, mode
):
    db = _rollback_database(tmp_path) if mode == "rollback" else _closed_wal_database(tmp_path)
    foreign, _second, expected = _foreign(tmp_path)
    journal = Path(str(db) + "-journal")
    original = retention._validate_schemas
    swapped = False

    def schemas_after_open(conn):
        # The first SQL statement of the pass; identity checks already passed.
        nonlocal swapped
        if not swapped:
            os.rename(foreign, journal)
            swapped = True
        return original(conn)

    monkeypatch.setattr(retention, "_validate_schemas", schemas_after_open)
    if mode == "wal-no-producer":
        # Round 7 performs its first read inside the worker and cannot read a
        # closed WAL without creating sidecars. Inject before that opening/read
        # boundary instead of waiting for a caller schema callback never reached.
        popen = retention.subprocess.Popen

        def before_worker_read(arguments, *args, **kwargs):
            nonlocal swapped
            if not swapped and any("_serve_database" in part for part in arguments):
                os.rename(foreign, journal)
                swapped = True
            return popen(arguments, *args, **kwargs)

        monkeypatch.setattr(retention.subprocess, "Popen", before_worker_read)
    try:
        report = retention.apply_retention(retention.RetentionPolicy(30), db_path=db)
    except retention.RetentionError as exc:
        report = exc.report
    assert swapped and report.dry_run
    (tmp_path / "evidence.json").write_text(
        json.dumps({"summary": report.summary(), "journal_exists": journal.exists()}, indent=2)
    )
    assert journal.exists() and journal.read_bytes() == expected, (
        "A dry run removed (hot-journal deletion) a foreign file substituted into "
        "the -journal slot: " + report.summary()
    )
    assert report.errors, "An unvalidated -journal must stop with an error"


# ---------------------------------------------------------------------------
# F2: the documented SHM bootstrap limit, measured.
# ---------------------------------------------------------------------------


def _live_wal_database(tmp_path: Path) -> tuple[Path, StudioStore]:
    db = tmp_path / "selected.sqlite3"
    service = StudioStore(db)
    with service._conn:
        _insert_old_run(service._conn)
    service._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert Path(str(db) + "-shm").exists()
    return db, service


def _swap_after_shm_validation(monkeypatch, shm: Path, foreign: Path, aside: Path) -> list[bool]:
    original = retention.subprocess.Popen
    injected = [False]

    def launch(arguments, *args, **kwargs):
        if any("_serve_database" in part for part in arguments):
            injected[0] = True
            arguments = list(arguments)
            arguments[4] = f"""
import os
import nexura_runtime.retention as retention
validate = retention._validate_sidecar
changed = False
def validate_then_swap(parent, name, suffix, expected, *, wal_validated):
    global changed
    validate(parent, name, suffix, expected, wal_validated=wal_validated)
    if suffix == "-shm" and not changed:
        os.rename({str(shm)!r}, {str(aside)!r})
        os.rename({str(foreign)!r}, {str(shm)!r})
        changed = True
retention._validate_sidecar = validate_then_swap
retention._serve_database()
"""
        return original(arguments, *args, **kwargs)

    monkeypatch.setattr(retention.subprocess, "Popen", launch)
    return injected


@pytest.mark.parametrize("dry_run", [False, True], ids=["apply", "dry-run"])
def test_shm_substitution_after_validation_stops_without_deletion(tmp_path, monkeypatch, dry_run):
    db, service = _live_wal_database(tmp_path)
    foreign, _second, _expected = _foreign(tmp_path)
    shm = Path(str(db) + "-shm")
    injected = _swap_after_shm_validation(monkeypatch, shm, foreign, tmp_path / "original.shm")
    try:
        with pytest.raises(retention.RetentionError) as failure:
            retention.apply_retention(retention.RetentionPolicy(30), db_path=db, dry_run=dry_run)
        report = failure.value.report
        assert injected[0] and not foreign.exists()
        assert report.errors and report.runs_deleted == report.events_deleted == 0
        assert service._conn.execute("SELECT run_id FROM nexura_studio_runs").fetchall() == [
            ("selected-run",)
        ]
    finally:
        service._close_sync()


@pytest.mark.parametrize("dry_run", [False, True], ids=["apply", "dry-run"])
def test_documented_limit_shm_substitution_foreign_bytes_preserved(tmp_path, monkeypatch, dry_run):
    """Expected to fail: this measures the documented SHM bootstrap limitation."""
    db, service = _live_wal_database(tmp_path)
    foreign, second, expected = _foreign(tmp_path)
    shm = Path(str(db) + "-shm")
    _swap_after_shm_validation(monkeypatch, shm, foreign, tmp_path / "original.shm")
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
                    "foreign_bytes_before": len(expected),
                    "foreign_bytes_after": len(observed),
                    "foreign_magic_before": repr(expected[:16]),
                    "foreign_magic_after": repr(observed[:16]),
                    "foreign_header_intact": observed[:16] == expected[:16],
                },
                indent=2,
            )
        )
        assert observed == expected, (
            f"SHM bootstrap changed the foreign file ({len(expected)} -> {len(observed)} "
            f"bytes, header {observed[:16]!r})"
        )
    finally:
        service._close_sync()


# ---------------------------------------------------------------------------
# F3: worker children are steerable through the caller's working directory.
# ---------------------------------------------------------------------------


def _plant_package(cwd: Path, marker: Path) -> None:
    package = cwd / "nexura_runtime"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "retention.py").write_text(
        "from pathlib import Path\n"
        "import sys\n"
        f"MARKER = Path({str(marker)!r})\n"
        "def _serve_database():\n"
        "    MARKER.write_text('planted _serve_database ran')\n"
        "    sys.exit(3)\n"
        "def _inspect_export_worker():\n"
        "    MARKER.write_text('planted _inspect_export_worker ran')\n"
        "    sys.exit(3)\n"
    )


def test_database_worker_does_not_import_code_from_caller_cwd(tmp_path, monkeypatch):
    db, service = _live_wal_database(tmp_path)  # live WAL forces the worker
    cwd, marker = tmp_path / "operator-cwd", tmp_path / "steered.txt"
    _plant_package(cwd, marker)
    monkeypatch.chdir(cwd)
    try:
        with suppress(retention.RetentionError):
            retention.apply_retention(retention.RetentionPolicy(30), db_path=db)
        assert not marker.exists(), (
            "The SQLite worker imported a planted nexura_runtime package from the "
            "caller's working directory"
        )
    finally:
        service._close_sync()


def test_export_worker_does_not_import_code_from_caller_cwd(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    (data / "loose.json").write_text('{"run_id": "r"}')
    cwd, marker = tmp_path / "operator-cwd", tmp_path / "steered.txt"
    _plant_package(cwd, marker)
    monkeypatch.chdir(cwd)
    retention.apply_retention(retention.RetentionPolicy(30), data_dir=data)
    assert not marker.exists(), (
        "The export inspection worker imported a planted nexura_runtime package "
        "from the caller's working directory"
    )


# ---------------------------------------------------------------------------
# F4: a database path that does not exist (or is a non-SQLite URI) is not
# reported as an error.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "target",
    ["missing.sqlite3", "postgresql://review@127.0.0.1:32768/nexura_review"],
    ids=["typo-path", "postgres-uri"],
)
def test_unopenable_database_target_is_reported_not_silently_applied(tmp_path, target):
    db = tmp_path / target if "://" not in target else target
    try:
        report = retention.apply_retention(retention.RetentionPolicy(30), db_path=db, dry_run=False)
    except retention.RetentionError as exc:
        report = exc.report
    assert report.errors, (
        "Apply against an unopenable database reported success: " + report.summary()
    )
