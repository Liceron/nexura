"""Promoted round-4 independent review controls; original assertions preserved."""

import ast
import json
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

import nexura_runtime.retention as retention
from nexura_runtime.cli.main import build_parser
from nexura_runtime.observability.models import EventRecord
from nexura_runtime.studio.store import StudioStore


def initialize(path, rid):
    store = StudioStore(path)
    store._close_sync()
    conn = sqlite3.connect(path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO nexura_studio_runs "
                "(run_id,spec_hash,spec_json,status,input_state_json,current_state_json,"
                "created_at,updated_at) "
                "VALUES (?,'review','{}','completed','{}','{}',?,?)",
                (rid, time.time() - 100 * 86400, time.time() - 100 * 86400),
            )
    finally:
        conn.close()


def remaining(db):
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT run_id FROM nexura_studio_runs").fetchall()
    finally:
        conn.close()


@pytest.mark.parametrize("replacement", ["directory", "database-entry"])
def test_real_directory_swap_does_not_reauthorize_foreign_database(
    tmp_path, monkeypatch, replacement
):
    selected = tmp_path / "selected"
    foreign = tmp_path / "foreign-workspace"
    selected.mkdir()
    foreign.mkdir()
    database = selected / "service.sqlite3"
    initialize(database, "selected-run")
    initialize(foreign / database.name, "foreign-run")
    original = sqlite3.connect
    isolate = retention._isolated_database
    swapped = False

    def swap():
        nonlocal swapped
        if not swapped:
            if replacement == "directory":
                selected.rename(tmp_path / "selected-pinned")
                foreign.rename(selected)
            else:
                database.rename(selected / "original.sqlite3")
                (foreign / database.name).rename(database)
            swapped = True

    def connect(path, *args, **kwargs):
        if str(path).startswith(database.as_uri()):
            swap()
        return original(path, *args, **kwargs)

    @contextmanager
    def worker(path, authorised):
        # Exercise the same substitution when existing handles select pre-open
        # isolation. The real child still verifies the original authorization.
        swap()
        with isolate(path, authorised) as connection:
            yield connection

    monkeypatch.setattr(retention.sqlite3, "connect", connect)
    monkeypatch.setattr(retention, "_isolated_database", worker)
    report = None
    try:
        report = retention.apply_retention(
            retention.RetentionPolicy(runs_days=30), db_path=database, dry_run=False
        )
    except retention.RetentionError as exc:
        report = exc.report
    assert swapped
    assert remaining(selected / database.name) == [("foreign-run",)], (
        "Isolated fallback adopted replacement directory as new authority: " + report.summary()
    )


def test_export_paths_have_no_mutation_calls_in_retention_source():
    tree = ast.parse(Path(retention.__file__).read_text())
    forbidden = {
        "unlink",
        "remove",
        "rename",
        "replace",
        "mkdir",
        "makedirs",
        "rmdir",
        "link",
        "symlink",
        "chmod",
        "fchmod",
        "truncate",
        "ftruncate",
        "utime",
    }
    found = [
        ast.unparse(node.func)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in forbidden
        and not (
            node.func.attr == "replace"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "value"
        )
    ]  # ISO timestamp string normalization
    assert not found, found


@pytest.mark.parametrize("dry", [True, False])
def test_apply_cannot_reach_export_mutations_and_lists_exact_bytes(tmp_path, monkeypatch, dry):
    db = tmp_path / "service.sqlite3"
    initialize(db, "r")
    if dry:
        prepared = sqlite3.connect(db)
        prepared.execute("PRAGMA journal_mode=DELETE")
        prepared.close()
    root = tmp_path / "exports"
    root.mkdir()
    content = (
        EventRecord(
            seq_num=1, run_id="r", timestamp=time.time() - 100 * 86400, event_type="LOCAL_REVIEW"
        ).model_dump_json()
        + "\n"
    )
    path = root / "unusual-name.bin"
    path.write_text(content)
    raw = path.read_bytes()
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("retention attempted filesystem mutation")

    with monkeypatch.context() as guard:
        for name in (
            "unlink",
            "remove",
            "rename",
            "replace",
            "mkdir",
            "rmdir",
            "link",
            "symlink",
            "chmod",
        ):
            guard.setattr(os, name, forbidden)
        report = retention.apply_retention(
            retention.RetentionPolicy(30), db_path=db, data_dir=root, dry_run=dry
        )
    assert not calls and path.read_bytes() == raw
    assert report.files_deleted == 0 and len(report.eligible_files) == 1
    item = report.eligible_files[0]
    assert (item.path, item.run_id, item.kind, item.size_bytes) == (
        str(path),
        "r",
        "traces",
        len(raw),
    )
    assert item.age_days > 30 and "manual removal only" in item.reason
    assert "Files listed, not deleted" in report.summary()
    assert remaining(db) == ([("r",)] if dry else [])


def test_inspector_read_only_even_while_hostile_parent_moves(tmp_path):
    root = tmp_path / "exports"
    root.mkdir()
    path = root / "closed.jsonl"
    content = (
        EventRecord(
            seq_num=1, run_id="r", timestamp=time.time() - 100 * 86400, event_type="LOCAL_REVIEW"
        ).model_dump_json()
        + "\n"
    )
    path.write_text(content)
    moved = tmp_path / "moved-outside-root"
    script = """
import os, sys, json
from pathlib import Path
request = json.loads(sys.argv[1])
root, moved = Path(request[0]), Path(sys.argv[2])
original = os.open
mutated = False
def hostile_open(name, flags, *args, **kwargs):
    global mutated
    if str(name) == "closed.jsonl" and not mutated:
        root.rename(moved)
        root.mkdir()
        mutated = True
    if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
        raise AssertionError("export opened for writing")
    return original(name, flags, *args, **kwargs)
os.open = hostile_open
from nexura_runtime.retention import _inspect_export_worker
try:
    _inspect_export_worker()
except (ValueError, OSError):
    assert mutated
else:
    raise AssertionError("moved directory should suppress eligibility")
"""
    proc = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            script,
            json.dumps([str(root), path.name, "traces", "r"]),
            str(moved),
        ],
        input=json.dumps([str(root), path.name, "traces", "r"]),
        capture_output=True,
        text=True,
        timeout=35,
    )
    assert proc.returncode == 0, proc.stderr
    assert (moved / path.name).read_bytes() == content.encode()
    assert list(root.iterdir()) == []


def test_cli_help_states_sql_only_and_manual_files():
    parser = build_parser()
    action = next(
        action
        for action in parser._actions
        if hasattr(action, "choices") and action.choices and "retention" in action.choices
    )
    help_text = action.choices["retention"].format_help()
    assert "database" in help_text.lower()
    assert (
        "Files listed, not deleted" in help_text or "files listed, not deleted" in help_text.lower()
    )


def test_installed_cli_lists_exports_and_applies_only_sql(tmp_path):
    db = tmp_path / "service.sqlite3"
    initialize(db, "r")
    prepared = sqlite3.connect(db)
    prepared.execute("PRAGMA journal_mode=DELETE")
    prepared.close()
    root = tmp_path / "exports"
    root.mkdir()
    path = root / "manual.trace"
    path.write_text(
        EventRecord(
            seq_num=1, run_id="r", timestamp=time.time() - 100 * 86400, event_type="LOCAL_REVIEW"
        ).model_dump_json()
        + "\n"
    )
    before = path.read_bytes()
    command = [
        str(Path(sys.executable).parent / "nexura-runtime"),
        "retention",
        "--older-than-days",
        "30",
        "--db",
        str(db),
        "--data-dir",
        str(root),
    ]
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    outputs = []
    for apply in (False, True):
        proc = subprocess.run(
            [*command, *(["--apply"] if apply else [])],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=35,
        )
        assert proc.returncode == 0, proc.stderr
        assert "Files listed, not deleted: 1 eligible exports" in proc.stdout
        assert str(path) in proc.stdout and "manual removal only" in proc.stdout
        assert path.read_bytes() == before
        assert remaining(db) == ([] if apply else [("r",)])
        outputs.append(proc.stdout)
    (tmp_path / "installed-cli.json").write_text(json.dumps(outputs, indent=2))


@pytest.mark.parametrize("replacement", ["directory", "database-entry"])
def test_worker_handoff_preserves_original_database_and_parent(tmp_path, monkeypatch, replacement):
    selected = tmp_path / "selected"
    foreign = tmp_path / "foreign"
    selected.mkdir()
    foreign.mkdir()
    database = selected / "service.sqlite3"
    foreign_db = foreign / database.name
    initialize(database, "selected-run")
    initialize(foreign_db, "foreign-run")
    original_bytes, foreign_bytes = database.read_bytes(), foreign_db.read_bytes()
    main, parent = database.stat(), selected.stat()
    expected = [main.st_dev, main.st_ino, parent.st_dev, parent.st_ino]
    popen = subprocess.Popen
    launched = False

    def replace_before_exec(args, *positional, **kwargs):
        process = popen(args, *positional, **kwargs)
        if any("_serve_database" in part for part in args):
            stream = process.stdin

            class AuthorisationPipe:
                def write(self, value):
                    nonlocal launched
                    if not launched:
                        # The original token travels as JSON over stdin now.
                        # Keep the assertion on actual transmitted authorization.
                        args = [json.dumps(json.loads(value)["authorised"])]
                        assert not launched
                        assert json.loads(args[-1]) == expected
                        if replacement == "directory":
                            selected.rename(tmp_path / "selected-pinned")
                            foreign.rename(selected)
                        else:
                            database.rename(selected / "original.sqlite3")
                            foreign_db.rename(database)
                        launched = True
                    return stream.write(value)

                def __getattr__(self, name):
                    return getattr(stream, name)

            process.stdin = AuthorisationPipe()
        return process

    monkeypatch.setattr(retention.subprocess, "Popen", replace_before_exec)
    unrelated = tmp_path / "unrelated"
    unrelated.write_bytes(b"unrelated")
    with unrelated.open("r+b"), pytest.raises(retention.RetentionError) as failure:
        retention.apply_retention(retention.RetentionPolicy(30), db_path=database, dry_run=False)
    report = failure.value.report
    original_db = (
        tmp_path / "selected-pinned" / database.name
        if replacement == "directory"
        else selected / "original.sqlite3"
    )
    assert launched and report.errors
    assert "caller-authorised identity" in str(failure.value)
    assert report.runs_deleted == report.events_deleted == report.bytes_freed == 0
    assert remaining(database) == [("foreign-run",)]
    assert remaining(original_db) == [("selected-run",)]
    assert database.read_bytes() == foreign_bytes
    assert original_db.read_bytes() == original_bytes


@pytest.mark.parametrize("opened_case", ["redirected", "extra-descriptor"])
def test_opened_connection_mismatch_or_ambiguity_never_retries(tmp_path, opened_case):
    database, foreign = tmp_path / "selected.sqlite3", tmp_path / "foreign.sqlite3"
    initialize(database, "selected-run")
    initialize(foreign, "foreign-run")
    selected_bytes, foreign_bytes = database.read_bytes(), foreign.read_bytes()
    # A fresh interpreter keeps this control independent of unrelated suite FDs.
    # Both SQLite opens and descriptor inspection remain real; only scheduling
    # and the destination of the hostile connect are injected.
    script = """
import json, sqlite3, sys
from pathlib import Path
import nexura_runtime.retention as retention
selected, foreign, mode = json.load(sys.stdin)
connect = sqlite3.connect
connected = retried = False
extra = None
def hostile_open(path, *args, **kwargs):
    global connected, extra
    connected = True
    if mode == "redirected":
        return connect(Path(foreign).as_uri() + "?mode=rw", *args, **kwargs)
    extra = open(foreign, "r+b")
    return connect(path, *args, **kwargs)
def forbidden_retry(*args, **kwargs):
    global retried
    retried = True
    raise AssertionError("identity failure must never launch a replacement worker")
retention.sqlite3.connect = hostile_open
retention._isolated_database = forbidden_retry
try:
    try:
        retention.apply_retention(retention.RetentionPolicy(30), db_path=selected, dry_run=False)
    except retention.RetentionError as failure:
        report = failure.report
        assert report.errors and report.runs_deleted == report.bytes_freed == 0
        assert "ambiguous or changed" in str(failure)
    else:
        raise AssertionError("unverified opened connection was accepted")
    assert connected and not retried
finally:
    if extra is not None:
        extra.close()
"""
    process = subprocess.run(
        [sys.executable, "-B", "-c", script],
        input=json.dumps([str(database), str(foreign), opened_case]),
        capture_output=True,
        text=True,
        timeout=35,
    )
    assert process.returncode == 0, process.stderr
    assert remaining(database) == [("selected-run",)]
    assert remaining(foreign) == [("foreign-run",)]
    assert database.read_bytes() == selected_bytes and foreign.read_bytes() == foreign_bytes


def test_worker_cannot_authorise_a_path_without_caller_identity(tmp_path):
    database = tmp_path / "service.sqlite3"
    initialize(database, "selected-run")
    before = database.read_bytes()
    with (
        pytest.raises(ValueError, match="requires the caller-authorised database identity"),
        retention._database(database, isolated=True),
    ):
        pytest.fail("worker accepted a pathname without its original identity")
    assert remaining(database) == [("selected-run",)] and database.read_bytes() == before


def test_identity_failure_before_commit_rolls_back_the_current_run(tmp_path):
    database = tmp_path / "service.sqlite3"
    initialize(database, "selected-run")
    conn = sqlite3.connect(database, factory=retention._RetentionConnection)
    calls = 0

    def verify():
        nonlocal calls
        calls += 1
        if calls == 3:
            raise ValueError("identity changed before commit")

    conn.verify_identity = verify
    try:
        with (
            pytest.raises(ValueError, match="identity changed before commit"),
            retention._transaction(conn, dry_run=False),
        ):
            conn.execute("DELETE FROM nexura_studio_runs")
            assert conn.execute("SELECT run_id FROM nexura_studio_runs").fetchall() == []
        assert calls == 3 and not conn.in_transaction
        assert conn.execute("SELECT run_id FROM nexura_studio_runs").fetchall() == [
            ("selected-run",)
        ]
    finally:
        conn.close()
