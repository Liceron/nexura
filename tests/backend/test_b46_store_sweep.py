"""B46: the open-time sweep only interrupts runs whose owning process is gone.

Opening a second StudioStore on the same database (as `nexura-runtime worker` does next to
a live `serve`, which docs/studio-and-service.md recommends) used to mark the live
process's running runs as interrupted.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from nexura_runtime.studio import store as store_module
from nexura_runtime.studio.store import StudioStore
from nexura_runtime.workflow import WorkflowSpec

HAS_PROC = Path("/proc/self/stat").exists()


def _spec() -> WorkflowSpec:
    return WorkflowSpec.from_dict(
        {
            "name": "b46",
            "entry_node": "a",
            "nodes": {"a": {"id": "a", "handler": "synthetic_order_lookup", "transitions": {}}},
            "terminal_nodes": ["a"],
        }
    )


def _start_run(db: Path, run_id: str) -> None:
    store = StudioStore(db)
    try:
        asyncio.run(store.record_run_start(run_id, "wf-1", _spec(), {}))
    finally:
        store._close_sync()


def _set_owner(db: Path, run_id: str, owner: str | None) -> None:
    conn = sqlite3.connect(str(db))
    with conn:
        conn.execute(
            "UPDATE nexura_studio_runs SET owner_process = ? WHERE run_id = ?", (owner, run_id)
        )
    conn.close()


def _row(db: Path, run_id: str) -> tuple[str, str | None, str | None]:
    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT status, error, owner_process FROM nexura_studio_runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    conn.close()
    return row[0], row[1], row[2]


def _event_types(db: Path, run_id: str) -> list[str]:
    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        "SELECT event_type FROM nexura_studio_run_events WHERE run_id = ? ORDER BY event_seq",
        (run_id,),
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def _reopen(db: Path) -> None:
    StudioStore(db)._close_sync()


@pytest.fixture
def sleeper() -> Iterator[subprocess.Popen[bytes]]:
    """A real, live process other than the test process."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        yield proc
    finally:
        proc.kill()
        proc.wait()


def _owner_for(pid: int) -> str:
    marker = store_module._process_start_marker(pid)
    assert marker is not None, "process should be alive"
    return f"{store_module._host_id()}:{pid}:{marker}"


def test_second_process_store_does_not_interrupt_live_runs(tmp_path: Path) -> None:
    db = tmp_path / "svc.sqlite3"
    serve_store = StudioStore(db)  # the live `serve` process
    asyncio.run(serve_store.record_run_start("run-live", "wf-1", _spec(), {}))
    assert asyncio.run(serve_store.get_run_status("run-live")) == "running"

    StudioStore(db)  # `nexura-runtime worker --db <same file>` starting up

    assert asyncio.run(serve_store.get_run_status("run-live")) == "running"
    serve_store._close_sync()


def test_run_start_records_this_process_as_owner(tmp_path: Path) -> None:
    db = tmp_path / "svc.sqlite3"
    _start_run(db, "run-1")
    _, _, owner = _row(db, "run-1")
    assert owner == store_module._process_owner()
    host, pid, _marker = owner.rsplit(":", 2)
    assert host == store_module._host_id()
    assert int(pid) == os.getpid()


def test_live_other_process_run_is_not_interrupted(
    tmp_path: Path, sleeper: subprocess.Popen[bytes]
) -> None:
    db = tmp_path / "svc.sqlite3"
    _start_run(db, "run-other")
    _set_owner(db, "run-other", _owner_for(sleeper.pid))

    _reopen(db)

    status, error, _ = _row(db, "run-other")
    assert status == "running"
    assert error is None
    assert "RUN_INTERRUPTED" not in _event_types(db, "run-other")


def test_dead_owner_run_is_interrupted_on_open(
    tmp_path: Path, sleeper: subprocess.Popen[bytes]
) -> None:
    db = tmp_path / "svc.sqlite3"
    _start_run(db, "run-dead")
    _set_owner(db, "run-dead", _owner_for(sleeper.pid))
    sleeper.kill()
    sleeper.wait()  # reaped: the pid no longer exists

    _reopen(db)

    status, error, _ = _row(db, "run-dead")
    assert status == "interrupted"
    assert error is not None and "interrupted by server restart" in error
    assert _event_types(db, "run-dead")[-1] == "RUN_INTERRUPTED"


def test_legacy_row_without_owner_keeps_old_behaviour(tmp_path: Path) -> None:
    db = tmp_path / "svc.sqlite3"
    _start_run(db, "run-legacy")
    _set_owner(db, "run-legacy", None)

    _reopen(db)

    status, error, _ = _row(db, "run-legacy")
    assert status == "interrupted"
    assert error is not None and "interrupted by server restart" in error
    assert "RUN_INTERRUPTED" in _event_types(db, "run-legacy")


def test_old_schema_database_is_migrated_and_swept(tmp_path: Path) -> None:
    """A database created before owner_process existed gains the column on open."""
    db = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE nexura_studio_runs (
            run_id TEXT PRIMARY KEY, workflow_id TEXT, spec_hash TEXT NOT NULL,
            spec_json TEXT NOT NULL, status TEXT NOT NULL, current_node_id TEXT,
            input_state_json TEXT NOT NULL, current_state_json TEXT NOT NULL,
            step_count INTEGER NOT NULL DEFAULT 0, approval_id TEXT, approval_json TEXT,
            error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO nexura_studio_runs VALUES "
        "('run-old', 'wf-1', 'h', '{}', 'running', 'a', '{}', '{}', 1, NULL, NULL, NULL, 1, 2)"
    )
    conn.commit()
    conn.close()

    _reopen(db)

    status, _, owner = _row(db, "run-old")
    assert status == "interrupted"
    assert owner is None
    _reopen(db)  # reopening a migrated database is fine


@pytest.mark.skipif(not HAS_PROC, reason="start markers need /proc (Linux)")
def test_reused_pid_with_different_start_marker_counts_as_dead(tmp_path: Path) -> None:
    db = tmp_path / "svc.sqlite3"
    _start_run(db, "run-reused")
    # Our own pid is alive, but the recorded start marker belongs to an earlier process.
    _set_owner(db, "run-reused", f"{store_module._host_id()}:{os.getpid()}:old-boot-123")

    _reopen(db)

    status, _, _ = _row(db, "run-reused")
    assert status == "interrupted"
    assert "RUN_INTERRUPTED" in _event_types(db, "run-reused")


def test_run_owned_by_another_host_is_left_alone(tmp_path: Path) -> None:
    db = tmp_path / "svc.sqlite3"
    _start_run(db, "run-remote")
    _set_owner(db, "run-remote", "some-other-host-b46:1:marker")

    _reopen(db)

    status, error, _ = _row(db, "run-remote")
    assert status == "running"
    assert error is None


def test_resumed_run_takes_the_resuming_process_as_owner(tmp_path: Path) -> None:
    db = tmp_path / "svc.sqlite3"
    _start_run(db, "run-resume")
    _set_owner(db, "run-resume", "some-other-host-b46:1:marker")
    store = StudioStore(db)
    try:
        asyncio.run(store.update_run("run-resume", "pending_approval", "a", {}, 1))
        assert _row(db, "run-resume")[2] == "some-other-host-b46:1:marker"
        asyncio.run(store.update_run("run-resume", "running", "a", {}, 1))
        assert _row(db, "run-resume")[2] == store_module._process_owner()
    finally:
        store._close_sync()


def test_owner_is_gone_parsing() -> None:
    gone = store_module._owner_is_gone
    assert gone(None) and gone("") and gone("garbage")
    assert gone(f"{store_module._host_id()}:notapid:x")
    assert gone(f"{store_module._host_id()}:0:x")
    assert not gone(store_module._process_owner())
    assert not gone("elsewhere:1:x")
