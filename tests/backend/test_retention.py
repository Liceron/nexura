# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Tests for P38 C9 part A: retention and cleanup for stored run data."""

from __future__ import annotations

import asyncio
import gc
import json
import math
import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from nexura_agents.contracts.execution import ExecutionId
from nexura_agents.execution_store import SQLiteExecutionStore

import nexura_runtime.retention as retention
from nexura_runtime.cli.main import main
from nexura_runtime.observability.models import EventRecord
from nexura_runtime.retention import (
    RetentionError,
    RetentionPolicy,
    RetentionReport,
    apply_retention,
    write_retention_artifact,
)
from nexura_runtime.runtime.approvals.manager import ApprovalPolicyManager
from nexura_runtime.runtime.approvals.models import ApprovalDecisionRecord, ApprovalRequest
from nexura_runtime.studio.store import StudioStore

_test_connections: list[sqlite3.Connection] = []


@pytest.fixture(autouse=True)
def _release_unreferenced_connections() -> Iterator[None]:
    # sqlite3 context managers commit but do not close. Release unreachable
    # connections left by earlier test frames before descriptor attribution.
    gc.collect()
    try:
        yield
    finally:
        # Release helper-owned connections even when an assertion fails. Pytest
        # retains failure frames, so relying on garbage collection is insufficient.
        for conn in _test_connections:
            conn.close()
        _test_connections.clear()


def _closed_artifact(
    db: Path,
    root: Path,
    name: str,
    run_id: str,
    kind: str,
    timestamp: float,
    records: int = 1,
) -> Path:
    """Export schema-valid files independently, then register for read-only reporting.

    Arbitrary {"old": "file"} JSON and {"id": 1} lines are not Nexura data and
    cannot safely be positive eligibility fixtures. Loose files have negative tests.
    """
    if kind == "bundles":
        content = json.dumps(
            {
                "schema_version": 1,
                "run_kind": "workflow",
                "run_id": run_id,
                "created_at": datetime.fromtimestamp(timestamp, UTC).isoformat(),
                "inputs": {},
                "spec": {},
                "model_responses": [],
                "tool_fixtures": [],
                "approvals": [],
                "budget_config": None,
                "operations": ["run"],
                "expected_outcome": {
                    "status": "failed",
                    "error_type": "LocalFakeError",
                    "error_message": "local fixture",
                    "failing_step": None,
                },
            }
        )
    else:
        content = "".join(
            EventRecord(
                seq_num=i + 1, run_id=run_id, event_type="LOCAL_FAKE", timestamp=timestamp
            ).model_dump_json()
            + "\n"
            for i in range(records)
        )
    (root / name).write_text(content, encoding="utf-8")
    return write_retention_artifact(
        content,
        db_path=db,
        data_dir=root,
        relative_path=name,
        run_id=run_id,
        kind=kind,
    )


def _assert_eligible(report: RetentionReport, path: Path, raw: bytes, run_id: str) -> None:
    assert path.read_bytes() == raw
    entries = [item for item in report.eligible_files if item.path == str(path)]
    assert len(entries) == 1
    entry = entries[0]
    assert entry.run_id == run_id and entry.size_bytes == len(raw)
    assert entry.age_days > 30
    assert entry.reason == "older than cutoff; terminal, unprotected run; manual removal only"
    assert report.files_deleted == 0
    assert "Files listed, not deleted" in report.summary()


def _init_db(db_path: Path) -> sqlite3.Connection:
    """Create a Studio database and initialize tables using StudioStore."""
    store = StudioStore(db_path)
    store._close_sync()
    conn = sqlite3.connect(str(db_path))
    _test_connections.append(conn)
    return conn


def _insert_run(
    conn: sqlite3.Connection,
    run_id: str,
    status: str,
    *,
    updated_at: float,
    created_at: float | None = None,
    approval_id: str | None = None,
    pending_effect: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    c_at = created_at if created_at is not None else updated_at
    effect_json = json.dumps(pending_effect) if pending_effect else None
    with conn:
        conn.execute(
            """
            INSERT INTO nexura_studio_runs (
                run_id, workflow_id, spec_hash, spec_json, status,
                current_node_id, input_state_json, current_state_json,
                step_count, approval_id, approval_json, error,
                created_at, updated_at, pending_effect_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                "wf-1",
                "hash-1",
                json.dumps({"name": "test-wf", "nodes": {}}),
                status,
                "node-1",
                json.dumps({"input": "test"}),
                json.dumps({"current": "test"}),
                1,
                approval_id,
                None,
                error,
                c_at,
                updated_at,
                effect_json,
            ),
        )


def _insert_event(
    conn: sqlite3.Connection,
    run_id: str,
    event_seq: int,
    event_type: str,
    *,
    timestamp: float,
) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO nexura_studio_run_events (
                run_id, event_seq, event_type, node_id, data_json, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                event_seq,
                event_type,
                "node-1",
                json.dumps({"event": "payload"}),
                timestamp,
            ),
        )


def _init_approval_table(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS approval_requests (
                workspace_id TEXT NOT NULL,
                approval_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                node_id TEXT,
                attempt_id TEXT NOT NULL,
                action_kind TEXT NOT NULL,
                required_role TEXT NOT NULL,
                action_payload_json TEXT NOT NULL,
                payload_digest TEXT NOT NULL,
                spec_digest TEXT NOT NULL,
                policy_version TEXT NOT NULL,
                status TEXT NOT NULL,
                claimed_by TEXT,
                claimed_at REAL,
                expires_at REAL NOT NULL,
                decision_json TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (workspace_id, approval_id)
            );
            """
        )


def _init_repro_table(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS nexura_service_repro_bundles (
                run_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                created_at REAL NOT NULL,
                bundle_json TEXT NOT NULL
            );
            """
        )


def _init_observability_table(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS observability_events (
                seq_num INTEGER NOT NULL,
                event_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                parent_run_id TEXT,
                node_id TEXT,
                node_name TEXT,
                attempt_num INTEGER NOT NULL,
                cycle_index INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                timestamp REAL NOT NULL,
                duration_ms REAL,
                trace_id TEXT,
                span_id TEXT,
                parent_span_id TEXT,
                payload_json TEXT NOT NULL,
                usage_json TEXT,
                metadata_json TEXT NOT NULL,
                error_code TEXT,
                error_message TEXT
            );
            """
        )


def test_retention_policy_initialization() -> None:
    p1 = RetentionPolicy(older_than_days=30)
    assert p1.runs_days == 30.0
    assert p1.bundles_days == 30.0
    assert p1.traces_days == 30.0

    p2 = RetentionPolicy(older_than_days=14, kinds=["runs"])
    assert p2.runs_days == 14.0
    assert p2.bundles_days is None
    assert p2.traces_days is None

    p3 = RetentionPolicy(runs_days=30, bundles_days=15, traces_days=7)
    assert p3.runs_days == 30.0
    assert p3.bundles_days == 15.0
    assert p3.traces_days == 7.0

    p4 = RetentionPolicy({"runs": 10, "bundles": None, "traces": 5})
    assert p4.runs_days == 10.0
    assert p4.bundles_days is None
    assert p4.traces_days == 5.0


def test_dry_run_deletes_nothing(tmp_path: Path) -> None:
    db = tmp_path / "service.sqlite3"
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    conn = _init_db(db)
    _init_repro_table(conn)
    _init_observability_table(conn)

    now = time.time()
    old_time = now - (40 * 86400)

    # Insert old terminal run with events
    _insert_run(conn, "run-old-1", "completed", updated_at=old_time)
    _insert_event(conn, "run-old-1", 1, "RUN_STARTED", timestamp=old_time)
    _insert_event(conn, "run-old-1", 2, "RUN_COMPLETED", timestamp=old_time)

    # Insert old repro bundle in DB and file on disk
    with conn:
        conn.execute(
            """
            INSERT INTO nexura_service_repro_bundles (
                run_id, workspace_id, created_at, bundle_json
            ) VALUES (?, ?, ?, ?)
            """,
            ("run-old-1", "default", old_time, json.dumps({"bundle": "data"})),
        )
    bundle_file = _closed_artifact(
        db, data_dir, "test.repro.json", "run-old-1", "bundles", old_time
    )

    # Insert old trace in DB and file on disk
    with conn:
        conn.execute(
            """
            INSERT INTO observability_events (
                seq_num, event_id, run_id, attempt_num, cycle_index, event_type,
                timestamp, payload_json, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (1, "ev-1", "run-old-1", 1, 0, "node_exec", old_time, "{}", "{}"),
        )
    traces_dir = data_dir / "traces"
    traces_dir.mkdir()
    trace_file = _closed_artifact(
        db, data_dir, "traces/app.traces.jsonl", "run-old-1", "traces", old_time, 2
    )
    conn.close()

    policy = RetentionPolicy(older_than_days=30)
    before = {path: path.read_bytes() for path in (bundle_file, trace_file)}
    # A closed WAL needs sidecar initialization. Prepare a checkpointed DELETE
    # fixture independently; retention previews must never do that work.
    with closing(sqlite3.connect(db)) as prepared:
        prepared.execute("PRAGMA journal_mode=DELETE")
    report = apply_retention(policy, db_path=db, data_dir=data_dir, dry_run=True)

    assert report.dry_run is True
    assert report.runs_deleted == 1
    assert report.events_deleted == 2
    assert report.bundles_deleted == 1  # SQL rows only (lead's 0.4 contract)
    assert report.traces_deleted == 1
    for path, raw in before.items():
        _assert_eligible(report, path, raw, "run-old-1")
    assert report.bytes_freed > 0

    # Verify nothing was deleted
    check_conn = sqlite3.connect(str(db))
    runs = check_conn.execute("SELECT run_id FROM nexura_studio_runs").fetchall()
    assert len(runs) == 1
    events = check_conn.execute("SELECT id FROM nexura_studio_run_events").fetchall()
    assert len(events) == 2
    bundles = check_conn.execute("SELECT run_id FROM nexura_service_repro_bundles").fetchall()
    assert len(bundles) == 1
    traces = check_conn.execute("SELECT event_id FROM observability_events").fetchall()
    assert len(traces) == 1
    check_conn.close()

    assert bundle_file.is_file()
    assert trace_file.is_file()


def test_apply_deletes_only_old_terminal_runs_and_events(tmp_path: Path) -> None:
    db = tmp_path / "service.sqlite3"
    conn = _init_db(db)

    now = time.time()
    old_time = now - (40 * 86400)
    young_time = now - (10 * 86400)

    # Old terminal runs: completed, failed, cancelled
    _insert_run(conn, "run-old-completed", "completed", updated_at=old_time)
    _insert_event(conn, "run-old-completed", 1, "RUN_STARTED", timestamp=old_time)
    _insert_run(conn, "run-old-failed", "failed", updated_at=old_time)
    _insert_event(conn, "run-old-failed", 1, "RUN_FAILED", timestamp=old_time)
    _insert_run(conn, "run-old-cancelled", "cancelled", updated_at=old_time)
    _insert_event(conn, "run-old-cancelled", 1, "RUN_CANCELLED", timestamp=old_time)

    # Young terminal runs: completed, failed, cancelled
    _insert_run(conn, "run-young-completed", "completed", updated_at=young_time)
    _insert_event(conn, "run-young-completed", 1, "RUN_COMPLETED", timestamp=young_time)
    _insert_run(conn, "run-young-failed", "failed", updated_at=young_time)
    _insert_event(conn, "run-young-failed", 1, "RUN_FAILED", timestamp=young_time)
    _insert_run(conn, "run-young-cancelled", "cancelled", updated_at=young_time)
    _insert_event(conn, "run-young-cancelled", 1, "RUN_CANCELLED", timestamp=young_time)
    conn.close()

    policy = RetentionPolicy(runs_days=30)
    report = apply_retention(policy, db_path=db, dry_run=False)

    assert report.dry_run is False
    assert report.runs_deleted == 3
    assert report.events_deleted == 3

    check_conn = sqlite3.connect(str(db))
    remaining_runs = {
        row[0] for row in check_conn.execute("SELECT run_id FROM nexura_studio_runs").fetchall()
    }
    assert remaining_runs == {
        "run-young-completed",
        "run-young-failed",
        "run-young-cancelled",
    }

    remaining_events = {
        row[0]
        for row in check_conn.execute("SELECT run_id FROM nexura_studio_run_events").fetchall()
    }
    assert remaining_events == {
        "run-young-completed",
        "run-young-failed",
        "run-young-cancelled",
    }
    check_conn.close()


def test_non_terminal_runs_never_deleted_at_any_age(tmp_path: Path) -> None:
    db = tmp_path / "service.sqlite3"
    conn = _init_db(db)

    now = time.time()
    very_old = now - (365 * 86400)  # 1 year old

    non_terminal_cases = [
        ("run-running", "running", None),
        ("run-paused", "paused", None),
        ("run-pending-approval", "pending_approval", None),
        ("run-interrupted", "interrupted", None),
        ("run-waiting-input", "waiting_for_input", None),
        ("run-effect-unknown", "paused", {"effect": "pending"}),
    ]

    for r_id, status, effect in non_terminal_cases:
        _insert_run(conn, r_id, status, updated_at=very_old, pending_effect=effect)
        _insert_event(conn, r_id, 1, "EVENT", timestamp=very_old)
    conn.close()

    policy = RetentionPolicy(runs_days=30)
    report = apply_retention(policy, db_path=db, dry_run=False)

    assert report.runs_deleted == 0
    assert report.events_deleted == 0

    check_conn = sqlite3.connect(str(db))
    remaining = [
        row[0] for row in check_conn.execute("SELECT run_id FROM nexura_studio_runs").fetchall()
    ]
    assert len(remaining) == len(non_terminal_cases)
    check_conn.close()


def test_approval_referenced_runs_never_deleted_at_any_age(tmp_path: Path) -> None:
    db = tmp_path / "service.sqlite3"
    conn = _init_db(db)
    _init_approval_table(conn)

    now = time.time()
    old_time = now - (40 * 86400)

    # 1. Old completed run referenced by an unresolved pending approval
    _insert_run(conn, "run-app-pending", "completed", updated_at=old_time, approval_id="app-1")
    with conn:
        conn.execute(
            """
            INSERT INTO approval_requests (
                workspace_id, approval_id, run_id, attempt_id, action_kind,
                required_role, action_payload_json, payload_digest, spec_digest,
                policy_version, status, expires_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "default",
                "app-1",
                "run-app-pending",
                "att-1",
                "action",
                "admin",
                "{}",
                "d1",
                "d2",
                "1.0",
                "pending",
                now + 1000,
                old_time,
                old_time,
            ),
        )

    # 2. Old completed run referenced by a claimed (unresolved) approval
    _insert_run(conn, "run-app-claimed", "completed", updated_at=old_time, approval_id="app-2")
    with conn:
        conn.execute(
            """
            INSERT INTO approval_requests (
                workspace_id, approval_id, run_id, attempt_id, action_kind,
                required_role, action_payload_json, payload_digest, spec_digest,
                policy_version, status, expires_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "default",
                "app-2",
                "run-app-claimed",
                "att-2",
                "action",
                "admin",
                "{}",
                "d1",
                "d2",
                "1.0",
                "claimed",
                now + 1000,
                old_time,
                old_time,
            ),
        )

    # 3. Old completed run referenced by a resolved (approved) approval -> eligible for deletion
    _insert_run(conn, "run-app-resolved", "completed", updated_at=old_time, approval_id="app-3")
    with conn:
        conn.execute(
            """
            INSERT INTO approval_requests (
                workspace_id, approval_id, run_id, attempt_id, action_kind,
                required_role, action_payload_json, payload_digest, spec_digest,
                policy_version, status, expires_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "default",
                "app-3",
                "run-app-resolved",
                "att-3",
                "action",
                "admin",
                "{}",
                "d1",
                "d2",
                "1.0",
                "approved",
                now + 1000,
                old_time,
                old_time,
            ),
        )

    # 4. Normal old completed run without any approval -> eligible for deletion
    _insert_run(conn, "run-no-approval", "completed", updated_at=old_time)
    conn.close()

    policy = RetentionPolicy(runs_days=30)
    report = apply_retention(policy, db_path=db, dry_run=False)

    assert report.runs_deleted == 2  # run-app-resolved and run-no-approval

    check_conn = sqlite3.connect(str(db))
    remaining = {
        row[0] for row in check_conn.execute("SELECT run_id FROM nexura_studio_runs").fetchall()
    }
    assert "run-app-pending" in remaining
    assert "run-app-claimed" in remaining
    assert "run-app-resolved" not in remaining
    assert "run-no-approval" not in remaining
    check_conn.close()


def test_bundles_older_than_n_removed(tmp_path: Path) -> None:
    db = tmp_path / "service.sqlite3"
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    conn = _init_db(db)
    _init_repro_table(conn)

    now = time.time()
    old_time = now - (40 * 86400)
    young_time = now - (10 * 86400)

    # DB repro bundles
    _insert_run(conn, "bundle-old", "failed", updated_at=old_time)
    _insert_run(conn, "bundle-young", "failed", updated_at=young_time)
    with conn:
        conn.execute(
            "INSERT INTO nexura_service_repro_bundles VALUES (?, ?, ?, ?)",
            ("bundle-old", "default", old_time, json.dumps({"old": True})),
        )
        conn.execute(
            "INSERT INTO nexura_service_repro_bundles VALUES (?, ?, ?, ?)",
            ("bundle-young", "default", young_time, json.dumps({"young": True})),
        )
    conn.close()

    # Disk repro bundles
    old_file = _closed_artifact(
        db, data_dir, "old_run.repro.json", "bundle-old", "bundles", old_time
    )

    young_file = _closed_artifact(
        db, data_dir, "young_run.repro.json", "bundle-young", "bundles", young_time
    )

    before = {path: path.read_bytes() for path in (old_file, young_file)}
    policy = RetentionPolicy(bundles_days=30)
    report = apply_retention(policy, db_path=db, data_dir=data_dir, dry_run=False)

    assert report.bundles_deleted == 1

    check_conn = sqlite3.connect(str(db))
    remaining_db = [
        row[0]
        for row in check_conn.execute("SELECT run_id FROM nexura_service_repro_bundles").fetchall()
    ]
    assert remaining_db == ["bundle-young"]
    check_conn.close()

    _assert_eligible(report, old_file, before[old_file], "bundle-old")
    assert young_file.read_bytes() == before[young_file]
    assert str(young_file) not in {item.path for item in report.eligible_files}


def test_traces_older_than_n_removed(tmp_path: Path) -> None:
    db = tmp_path / "service.sqlite3"
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    conn = _init_db(db)
    _init_observability_table(conn)

    now = time.time()
    old_time = now - (40 * 86400)
    young_time = now - (10 * 86400)

    # DB traces
    _insert_run(conn, "r1", "completed", updated_at=old_time)
    _insert_run(conn, "r2", "completed", updated_at=young_time)
    with conn:
        conn.execute(
            """
            INSERT INTO observability_events (
                seq_num, event_id, run_id, attempt_num, cycle_index, event_type,
                timestamp, payload_json, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (1, "ev-old", "r1", 1, 0, "node_exec", old_time, "{}", "{}"),
        )
        conn.execute(
            """
            INSERT INTO observability_events (
                seq_num, event_id, run_id, attempt_num, cycle_index, event_type,
                timestamp, payload_json, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (2, "ev-young", "r2", 1, 0, "node_exec", young_time, "{}", "{}"),
        )
    conn.close()

    # Disk traces
    traces_dir = data_dir / "traces"
    traces_dir.mkdir()
    old_file = _closed_artifact(
        db, data_dir, "traces/old.traces.jsonl", "r1", "traces", old_time, 2
    )

    young_file = _closed_artifact(
        db, data_dir, "traces/young.traces.jsonl", "r2", "traces", young_time
    )

    before = {path: path.read_bytes() for path in (old_file, young_file)}
    policy = RetentionPolicy(traces_days=30)
    report = apply_retention(policy, db_path=db, data_dir=data_dir, dry_run=False)

    assert report.traces_deleted == 1  # SQL rows only

    check_conn = sqlite3.connect(str(db))
    remaining = [
        row[0] for row in check_conn.execute("SELECT event_id FROM observability_events").fetchall()
    ]
    assert remaining == ["ev-young"]
    check_conn.close()

    _assert_eligible(report, old_file, before[old_file], "r1")
    assert young_file.read_bytes() == before[young_file]
    assert str(young_file) not in {item.path for item in report.eligible_files}


def test_journal_rows_skipped_and_reported(tmp_path: Path) -> None:
    db = tmp_path / "service.sqlite3"
    conn = _init_db(db)

    now = time.time()
    old_time = now - (40 * 86400)

    _insert_run(conn, "run-with-journal", "completed", updated_at=old_time)

    # Insert journal rows in nexura_workflow_checkpoints and history
    with conn:
        conn.execute(
            """
            INSERT INTO nexura_workflow_checkpoints (
                workflow_run_id, spec_version, spec_hash, state_json, step_count,
                status, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("run-with-journal", 1, "h", "{}", 1, "completed", old_time),
        )
        conn.execute(
            """
            INSERT INTO nexura_workflow_checkpoint_history (
                workflow_run_id, checkpoint_json, created_at
            ) VALUES (?, ?, ?)
            """,
            ("run-with-journal", "{}", old_time),
        )
    conn.close()

    policy = RetentionPolicy(runs_days=30)
    report = apply_retention(policy, db_path=db, dry_run=False)

    assert report.runs_deleted == 1
    assert report.journal_rows_skipped == 2

    # Verify run record was deleted, but journal rows remain untouched
    check_conn = sqlite3.connect(str(db))
    runs = check_conn.execute("SELECT run_id FROM nexura_studio_runs").fetchall()
    assert len(runs) == 0

    cps = check_conn.execute("SELECT workflow_run_id FROM nexura_workflow_checkpoints").fetchall()
    assert len(cps) == 1

    hist = check_conn.execute(
        "SELECT workflow_run_id FROM nexura_workflow_checkpoint_history"
    ).fetchall()
    assert len(hist) == 1
    check_conn.close()


def test_report_summary_and_aliases() -> None:
    report = RetentionReport(
        runs_deleted=2,
        events_deleted=5,
        bundles_deleted=1,
        traces_deleted=4,
        runs_bytes=1000,
        events_bytes=500,
        bundles_bytes=200,
        traces_bytes=300,
        bytes_freed=2000,
        journal_rows_skipped=3,
        dry_run=True,
    )
    assert report.runs_count == 2
    assert report.events_count == 5
    assert report.bundles_count == 1
    assert report.traces_count == 4
    assert report.total_bytes == 2000
    assert report.left_for_journal_compaction == 3

    summary = report.summary()
    assert "DRY RUN" in summary
    assert "Left for journal compaction: 3 rows" in summary
    assert "(Dry run: no data was deleted. Use --apply to delete.)" in summary


def test_cli_retention_dry_run_and_apply(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "service.sqlite3"
    conn = _init_db(db)

    now = time.time()
    old_time = now - (40 * 86400)

    _insert_run(conn, "run-cli-1", "completed", updated_at=old_time)
    _insert_event(conn, "run-cli-1", 1, "RUN_COMPLETED", timestamp=old_time)
    conn.close()

    # Dry run CLI invocation
    with closing(sqlite3.connect(db)) as prepared:
        prepared.execute("PRAGMA journal_mode=DELETE")
    rc_dry = main(
        [
            "retention",
            "--older-than-days",
            "30",
            "--db",
            str(db),
        ]
    )
    assert rc_dry == 0
    out_dry = capsys.readouterr().out
    assert "DRY RUN" in out_dry
    assert "Runs: 1 run records" in out_dry

    # Check still exists
    check_conn = sqlite3.connect(str(db))
    runs_after_dry = check_conn.execute("SELECT run_id FROM nexura_studio_runs").fetchall()
    assert len(runs_after_dry) == 1
    check_conn.close()

    # Apply CLI invocation
    rc_apply = main(
        [
            "retention",
            "--older-than-days",
            "30",
            "--db",
            str(db),
            "--apply",
        ]
    )
    assert rc_apply == 0
    out_apply = capsys.readouterr().out
    assert "APPLIED" in out_apply
    assert "Runs: 1 run records" in out_apply

    # Check deleted
    check_conn = sqlite3.connect(str(db))
    runs_after_apply = check_conn.execute("SELECT run_id FROM nexura_studio_runs").fetchall()
    assert len(runs_after_apply) == 0
    check_conn.close()


def test_cli_retention_validation_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "service.sqlite3"

    # Negative days
    rc1 = main(["retention", "--older-than-days", "-5", "--db", str(db)])
    assert rc1 == 1
    err1 = capsys.readouterr().err
    assert "must be non-negative" in err1

    # Invalid kinds
    rc2 = main(
        [
            "retention",
            "--older-than-days",
            "10",
            "--kinds",
            "invalid_kind",
            "--db",
            str(db),
        ]
    )
    assert rc2 == 1
    err2 = capsys.readouterr().err
    assert "unsupported kind" in err2


@pytest.mark.parametrize("age", [-1, math.nan, math.inf, -math.inf, 1e100, True])
@pytest.mark.parametrize("form", ["scalar", "mapping", "max_age", "per_kind", "mutated"])
def test_invalid_policy_forms_leave_database_untouched(
    tmp_path: Path, age: float, form: str
) -> None:
    db = tmp_path / "service.sqlite3"
    conn = _init_db(db)
    _insert_run(conn, "fresh", "completed", updated_at=time.time())
    conn.close()
    with pytest.raises(ValueError):
        if form == "scalar":
            policy = RetentionPolicy(age)
        elif form == "mapping":
            policy = RetentionPolicy({"runs": age})
        elif form == "max_age":
            policy = RetentionPolicy(max_age_days={"runs": age})
        elif form == "per_kind":
            policy = RetentionPolicy(runs_days=age)
        else:
            policy = RetentionPolicy(30)
            policy.runs_days = age
        apply_retention(policy, db_path=db, dry_run=False)
    with closing(sqlite3.connect(db)) as check, check:
        assert check.execute("SELECT run_id FROM nexura_studio_runs").fetchall() == [("fresh",)]


@pytest.fixture
def owned_export(tmp_path: Path) -> tuple[Path, Path, Path]:
    db = tmp_path / "service.sqlite3"
    root = tmp_path / "exports"
    root.mkdir()
    conn = _init_db(db)
    old = time.time() - 100 * 86400
    _insert_run(conn, "owned", "completed", updated_at=old)
    conn.close()
    path = _closed_artifact(db, root, "closed.repro.json", "owned", "bundles", old)
    return db, root, path


def test_registered_unlink_failure_has_zero_success_counts(
    owned_export: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    db, root, path = owned_export
    raw = path.read_bytes()
    original = os.unlink

    def fail(name: str, *args: Any, **kwargs: Any) -> None:
        if name == "artifact":
            raise PermissionError("injected unlink failure")
        original(name, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", fail)
    report = apply_retention(
        RetentionPolicy(bundles_days=30), db_path=db, data_dir=root, dry_run=False
    )
    assert path.exists()
    assert report.bundles_deleted == report.bundles_bytes == report.files_deleted == 0
    assert report.candidates["bundles"] == 0 and report.candidates["files"] == 1
    assert not report.errors
    _assert_eligible(report, path, raw, "owned")


@pytest.mark.parametrize("moment", ["before-quarantine", "after-quarantine"])
def test_registered_file_replacement_never_removes_fresh_export(
    owned_export: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch, moment: str
) -> None:
    db, root, path = owned_export
    raw = path.read_bytes()
    rename, unlink = os.rename, os.unlink
    injected = False

    def replace() -> None:
        nonlocal injected
        replacement = root / "new-export"
        replacement.write_text("fresh export", encoding="utf-8")
        replacement.replace(path)
        injected = True

    def before(name: str, destination: str, *args: Any, **kwargs: Any) -> None:
        if name == path.name and not injected:
            replace()
        rename(name, destination, *args, **kwargs)

    def after(name: str, *args: Any, **kwargs: Any) -> None:
        if name == "artifact" and not injected:
            replace()
        unlink(name, *args, **kwargs)

    monkeypatch.setattr(
        os,
        "rename" if moment == "before-quarantine" else "unlink",
        before if moment == "before-quarantine" else after,
    )
    report = apply_retention(
        RetentionPolicy(bundles_days=30), db_path=db, data_dir=root, dry_run=False
    )
    assert not injected
    assert report.bundles_deleted == 0
    _assert_eligible(report, path, raw, "owned")


@pytest.mark.parametrize("change", ["modified", "symlink", "hardlink"])
def test_registered_identity_changes_are_retained(
    owned_export: tuple[Path, Path, Path], change: str, tmp_path: Path
) -> None:
    db, root, path = owned_export
    if change == "modified":
        path.chmod(0o600)
        path.write_text("changed since registration")
    elif change == "symlink":
        path.unlink()
        outside = tmp_path / "outside"
        outside.write_text("outside data")
        path.symlink_to(outside)
    else:
        os.link(path, tmp_path / "second-link")
    report = apply_retention(
        RetentionPolicy(bundles_days=30), db_path=db, data_dir=root, dry_run=False
    )
    assert path.exists() and report.bundles_deleted == 0
    assert report.errors


def test_registered_parent_directory_swap_is_retained(
    owned_export: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db, root, path = owned_export
    raw = path.read_bytes()
    moved = tmp_path / "moved-exports"
    original = os.rename
    injected = False

    def swap(name: str, destination: str, *args: Any, **kwargs: Any) -> None:
        nonlocal injected
        if name == path.name and not injected:
            original(root, moved)
            root.symlink_to(moved, target_is_directory=True)
            injected = True
        original(name, destination, *args, **kwargs)

    monkeypatch.setattr(os, "rename", swap)
    report = apply_retention(
        RetentionPolicy(bundles_days=30), db_path=db, data_dir=root, dry_run=False
    )
    assert not injected and not moved.exists()
    assert report.bundles_deleted == 0 and not report.errors
    _assert_eligible(report, path, raw, "owned")


def test_empty_registered_trace_has_zero_records_and_one_file(
    owned_export: tuple[Path, Path, Path],
) -> None:
    db, root, _ = owned_export
    (root / "empty.jsonl").write_text("")
    path = write_retention_artifact(
        "", db_path=db, data_dir=root, relative_path="empty.jsonl", run_id="owned", kind="traces"
    )
    report = apply_retention(
        RetentionPolicy(traces_days=30), db_path=db, data_dir=root, dry_run=False
    )
    _assert_eligible(report, path, b"", "owned")
    assert report.traces_deleted == report.traces_bytes == 0
    assert report.files_deleted == 0 and report.candidates["files"] == 1


@pytest.mark.parametrize("kind", ["bundles", "traces"])
def test_registered_new_content_survives_old_run(
    owned_export: tuple[Path, Path, Path], kind: str
) -> None:
    db, root, old = owned_export
    old_raw = old.read_bytes()
    path = _closed_artifact(db, root, f"new-{kind}", "owned", kind, time.time())
    raw = path.read_bytes()
    report = apply_retention(
        RetentionPolicy(30, kinds=[kind]), db_path=db, data_dir=root, dry_run=False
    )
    assert path.exists()
    assert report.candidates["files"] == (kind == "bundles")  # only the old bundle qualifies
    assert path.read_bytes() == raw
    assert str(path) not in {item.path for item in report.eligible_files}
    if kind == "bundles":
        _assert_eligible(report, old, old_raw, "owned")


@pytest.mark.parametrize("field", ["pending_effect_json", "pending_input_json", "approval_json"])
def test_registered_artifacts_share_run_protection(
    owned_export: tuple[Path, Path, Path], field: str
) -> None:
    db, root, path = owned_export
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute(f"UPDATE nexura_studio_runs SET {field}='{{}}'")
    report = apply_retention(RetentionPolicy(0), db_path=db, data_dir=root, dry_run=False)
    assert path.exists() and report.bundles_deleted == report.runs_deleted == 0


def test_later_transaction_failure_preserves_committed_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "service.sqlite3"
    conn = _init_db(db)
    for rid in ("a", "b"):
        _insert_run(conn, rid, "completed", updated_at=time.time() - 100 * 86400)
    conn.close()
    original = retention._run_retention

    def fail_second(
        conn: sqlite3.Connection, rid: str, *args: Any, **kwargs: Any
    ) -> RetentionReport:
        if rid == "b":
            raise sqlite3.OperationalError("injected second transaction failure")
        return original(conn, rid, *args, **kwargs)

    monkeypatch.setattr(retention, "_run_retention", fail_second)
    with pytest.raises(RetentionError) as failure:
        apply_retention(RetentionPolicy(runs_days=30), db_path=db, dry_run=False)
    assert failure.value.report.runs_deleted == failure.value.report.candidates["runs"] == 1
    assert failure.value.report.errors and failure.value.report.bytes_freed > 0
    with closing(sqlite3.connect(db)) as check, check:
        assert check.execute("SELECT run_id FROM nexura_studio_runs").fetchall() == [("b",)]


def test_unknown_schema_and_destructive_trigger_fail_closed(tmp_path: Path) -> None:
    db = tmp_path / "service.sqlite3"
    conn = _init_db(db)
    _insert_run(conn, "old", "completed", updated_at=time.time() - 100 * 86400)
    conn.executescript(
        "CREATE TABLE unrelated (value TEXT); INSERT INTO unrelated VALUES ('keep'); "
        "CREATE TRIGGER malicious AFTER DELETE ON nexura_studio_runs "
        "BEGIN DELETE FROM unrelated; END;"
    )
    conn.close()
    with pytest.raises(RetentionError):
        apply_retention(RetentionPolicy(0), db_path=db, dry_run=False)
    with closing(sqlite3.connect(db)) as check, check:
        assert check.execute("SELECT value FROM unrelated").fetchall() == [("keep",)]
        assert check.execute("SELECT run_id FROM nexura_studio_runs").fetchall() == [("old",)]
        check.execute("DROP TRIGGER malicious")
        check.execute("CREATE TABLE nexura_sdk_approvals (run_id TEXT, status TEXT)")
    with pytest.raises(RetentionError, match="schema"):
        apply_retention(RetentionPolicy(0), db_path=db, dry_run=False)


def test_service_append_after_retention_cannot_create_orphan(tmp_path: Path) -> None:
    db = tmp_path / "service.sqlite3"
    conn = _init_db(db)
    _insert_run(conn, "old", "completed", updated_at=time.time() - 100 * 86400)
    conn.close()
    store = StudioStore(db)
    try:
        assert store._get_run_status_sync("old") == "completed"
        report = apply_retention(RetentionPolicy(runs_days=30), db_path=db, dry_run=False)
        assert report.runs_deleted == 1
        store._record_run_event_sync("old", "AFTER_DELETION", data={"audit": "local fake"})
        with closing(sqlite3.connect(db)) as check, check:
            assert check.execute("SELECT run_id FROM nexura_studio_run_events").fetchall() == []
    finally:
        store._close_sync()


def test_foreign_key_failure_rolls_back_run_and_events(tmp_path: Path) -> None:
    db = tmp_path / "service.sqlite3"
    conn = _init_db(db)
    old = time.time() - 100 * 86400
    _insert_run(conn, "retained", "completed", updated_at=old)
    _insert_event(conn, "retained", 1, "LOCAL_FAKE", timestamp=old)
    conn.execute(
        "CREATE TABLE other_owner (run_id TEXT REFERENCES nexura_studio_runs(run_id) "
        "ON DELETE RESTRICT)"
    )
    conn.execute("INSERT INTO other_owner VALUES ('retained')")
    conn.commit()
    conn.close()
    with pytest.raises(RetentionError) as failure:
        apply_retention(RetentionPolicy(runs_days=30), db_path=db, dry_run=False)
    assert failure.value.report.runs_deleted == failure.value.report.events_deleted == 0
    assert failure.value.report.bytes_freed == 0
    with closing(sqlite3.connect(db)) as check, check:
        assert check.execute("SELECT run_id FROM nexura_studio_runs").fetchall() == [("retained",)]
        assert check.execute("SELECT run_id FROM nexura_studio_run_events").fetchall() == [
            ("retained",)
        ]
        assert check.execute("SELECT run_id FROM other_owner").fetchall() == [("retained",)]


def test_old_trace_for_recently_finished_run_survives(tmp_path: Path) -> None:
    db = tmp_path / "service.sqlite3"
    conn = _init_db(db)
    _init_observability_table(conn)
    _insert_run(conn, "recent-finish", "completed", updated_at=time.time())
    with conn:
        conn.execute(
            "INSERT INTO observability_events "
            "(seq_num,event_id,run_id,attempt_num,cycle_index,event_type,"
            "timestamp,payload_json,metadata_json) "
            "VALUES (1,'old','recent-finish',1,0,'LOCAL_FAKE',?,'{}','{}')",
            (time.time() - 100 * 86400,),
        )
    conn.close()
    report = apply_retention(RetentionPolicy(traces_days=30), db_path=db, dry_run=False)
    assert report.traces_deleted == 0
    with closing(sqlite3.connect(db)) as check, check:
        assert check.execute("SELECT event_id FROM observability_events").fetchall() == [("old",)]


def test_database_symlink_is_refused(tmp_path: Path) -> None:
    db = tmp_path / "service.sqlite3"
    conn = _init_db(db)
    _insert_run(conn, "keep", "completed", updated_at=time.time() - 100 * 86400)
    conn.close()
    alias = tmp_path / "alias.sqlite3"
    alias.symlink_to(db)
    with pytest.raises(RetentionError):
        apply_retention(RetentionPolicy(0), db_path=alias, dry_run=False)
    with closing(sqlite3.connect(db)) as check, check:
        assert check.execute("SELECT run_id FROM nexura_studio_runs").fetchall() == [("keep",)]


def test_actual_approved_request_awaiting_dispatch_is_protected(tmp_path: Path) -> None:
    db = tmp_path / "service.sqlite3"
    conn = _init_db(db)
    _insert_run(conn, "keep", "completed", updated_at=time.time() - 100 * 86400)
    conn.close()
    manager = ApprovalPolicyManager(db)
    request = manager.create_request(
        ApprovalRequest(
            approval_id="ready",
            workspace_id="default",
            run_id="keep",
            expires_at=time.time() + 86400,
        )
    )
    manager.submit_decision(
        "default",
        "ready",
        ApprovalDecisionRecord(
            approval_id="ready",
            workspace_id="default",
            decided_by_principal_id="local-operator",
            decided_by_role="admin",
            decision="approved",
            expected_payload_digest=request.payload_digest,
        ),
    )
    report = apply_retention(RetentionPolicy(0), db_path=db, dry_run=False)
    assert report.runs_deleted == 0
    with closing(sqlite3.connect(db)) as check, check:
        assert check.execute("SELECT run_id FROM nexura_studio_runs").fetchall() == [("keep",)]


def test_effect_alias_origin_protects_owning_run(tmp_path: Path) -> None:
    db = tmp_path / "service.sqlite3"
    conn = _init_db(db)
    _insert_run(conn, "keep", "completed", updated_at=time.time() - 100 * 86400)
    conn.close()

    async def create_alias() -> None:
        store = SQLiteExecutionStore(db)
        try:
            await store.init_db()
            await store.create(
                ExecutionId("default", "opaque-effect-alias"),
                owner="local-fake-worker",
                definition_digest="fixture",
                origin={"source": "sdk", "legacy": {"run_id": "keep"}},
            )
        finally:
            await store.close()

    asyncio.run(create_alias())
    report = apply_retention(RetentionPolicy(0), db_path=db, dry_run=False)
    assert report.runs_deleted == 0
    with closing(sqlite3.connect(db)) as check, check:
        assert check.execute("SELECT run_id FROM nexura_studio_runs").fetchall() == [("keep",)]


def test_missing_parent_append_logs_clearly(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = StudioStore(tmp_path / "service.sqlite3")
    try:
        store._record_run_event_sync("never-existed", "AUDIT", data={"secret": "not logged"})
        assert "parent run never-existed does not exist" in caplog.text
        assert "AUDIT" in caplog.text and "not logged" not in caplog.text
        assert store._conn.execute("SELECT COUNT(*) FROM nexura_studio_run_events").fetchone() == (
            0,
        )
        assert not store._conn.in_transaction
    finally:
        store._close_sync()


def test_run_read_preserves_caller_transaction(tmp_path: Path) -> None:
    store = StudioStore(tmp_path / "service.sqlite3")
    try:
        store._conn.execute("BEGIN")
        assert store._get_run_sync("missing") is None
        assert store._conn.in_transaction
        store._conn.rollback()
        assert store._get_run_sync("missing") is None
        assert not store._conn.in_transaction
    finally:
        store._close_sync()
