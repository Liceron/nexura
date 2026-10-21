# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Persistence layer for Nexura Studio workflow specifications, runs, and events.

Stores workflow definitions, execution run snapshots, and ordered lifecycle events
durably in SQLite, reusing the M2 SQLiteWorkflowStore for engine checkpoints.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import os
import socket
import sqlite3
import threading
import time
import uuid
from collections.abc import Collection, Sequence
from pathlib import Path
from typing import Any

from nexura_agents.errors import (
    WorkflowConflictError,
    WorkflowError,
    WorkflowValidationError,
)

from nexura_runtime.studio.builtin_modules import get_default_workflow_spec
from nexura_runtime.workflow import (
    PendingApproval,
    SQLiteWorkflowStore,
    WorkflowSpec,
)

# ---------------------------------------------------------------------------
# Run ownership (B46)
#
# A run in status 'running' records the process executing it as
# "<host id>:<pid>:<start marker>". Opening a store marks such a run interrupted only
# when that process is on this host and gone, so a second process on the same database
# (``nexura-runtime worker`` next to ``serve``) leaves live runs alone. The start marker
# is the boot id plus the process start time from /proc, so a reused pid (or a pid from
# before a reboot) does not count as the original process. Without /proc only pid
# liveness is checked (POSIX) or the owner is assumed alive (Windows). Runs owned by
# another host are never swept: SQLite is single-host.
# A full lease-based design replaces this sweep later (P35 A6 step S9).
# ---------------------------------------------------------------------------

_PROC = Path("/proc")


def _host_id() -> str:
    """This machine's name; stable across reboots so a rebooted host still sweeps its runs."""
    return (socket.gethostname() or "localhost").replace(":", "_")


@functools.lru_cache(maxsize=1)
def _boot_id() -> str:
    try:
        return (_PROC / "sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def _process_start_marker(pid: int) -> str | None:
    """Start marker of a live process, ``""`` if unknowable here, None if the process is gone."""
    stat_path = _PROC / str(pid) / "stat"
    if _PROC.is_dir() and (_PROC / "self").exists():
        try:
            raw = stat_path.read_text()
        except OSError:
            return None
        # The command name (field 2) may contain spaces or parentheses; fields after the
        # last ')' start at field 3 (state). Field 22 (starttime) is index 19 there.
        fields = raw[raw.rfind(")") + 2 :].split()
        if len(fields) < 20 or fields[0] in ("Z", "X", "x"):
            return None  # zombie or dead: the process no longer runs
        return f"{_boot_id()}-{fields[19]}"
    if os.name == "nt":
        # os.kill(pid, 0) terminates the process on Windows; assume it is alive.
        return ""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return ""  # alive, owned by another user
    except OSError:
        return None
    return ""


@functools.lru_cache(maxsize=1)
def _own_marker() -> tuple[int, str]:
    pid = os.getpid()
    return pid, _process_start_marker(pid) or ""


def _process_owner() -> str:
    """Owner string for runs started by this process."""
    pid, marker = _own_marker()
    if pid != os.getpid():  # forked child: recompute for the new process
        _own_marker.cache_clear()
        pid, marker = _own_marker()
    return f"{_host_id()}:{pid}:{marker}"


def _owner_is_gone(owner: str | None) -> bool:
    """True when the run's owning process is known to be dead (or the row predates owners).

    Rows without an owner (databases from before B46) keep the old behaviour and are
    swept. Owners on another host are never treated as gone.
    """
    if not owner:
        return True
    parts = owner.rsplit(":", 2)
    if len(parts) != 3:
        return True  # unreadable owner: same as a legacy row
    host, pid_text, marker = parts
    if host != _host_id():
        return False
    try:
        pid = int(pid_text)
    except ValueError:
        return True
    if pid <= 0:
        return True
    current = _process_start_marker(pid)
    if current is None:
        return True
    # A different start marker means the pid now belongs to a different process.
    return bool(marker and current and marker != current)


class StudioStore:
    """Manages SQLite storage for Studio definitions, runs, and event streams."""

    def __init__(self, db_path: str | Path, *, checkpoint_store: Any = None) -> None:
        """Open the Studio database at ``db_path``.

        ``checkpoint_store``: where workflow checkpoints live (for example
        ``nexura_postgres.PostgresWorkflowStore``); the store then owns it and closes it
        with ``close()``. Default: a ``SQLiteWorkflowStore`` on the same database file.
        """
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._closed = False

        self._conn: sqlite3.Connection | None = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            timeout=10.0,
        )

        with self._lock, self._conn:
            self._conn.execute("PRAGMA journal_mode = WAL;")
            self._conn.execute("PRAGMA busy_timeout = 5000;")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS nexura_studio_workflows (
                    workflow_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    spec_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS nexura_studio_runs (
                    run_id TEXT PRIMARY KEY,
                    workflow_id TEXT,
                    spec_hash TEXT NOT NULL,
                    spec_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_node_id TEXT,
                    input_state_json TEXT NOT NULL,
                    current_state_json TEXT NOT NULL,
                    step_count INTEGER NOT NULL DEFAULT 0,
                    approval_id TEXT,
                    approval_json TEXT,
                    error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )
            # cancel_reason: why a run was cancelled when access ended (B40), e.g.
            # "access_revoked"; started_by_json: who started it (principal, API key).
            for column in (
                "pending_input_json",
                "forked_from_json",
                "cancel_reason",
                "started_by_json",
                # owner_process: "<host>:<pid>:<start marker>" of the process running it (B46).
                "owner_process",
            ):
                with contextlib.suppress(sqlite3.OperationalError):
                    self._conn.execute(f"ALTER TABLE nexura_studio_runs ADD COLUMN {column} TEXT;")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS nexura_studio_run_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    event_seq INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    node_id TEXT,
                    data_json TEXT NOT NULL,
                    timestamp REAL NOT NULL
                );
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_studio_events_run_id
                ON nexura_studio_run_events(run_id, event_seq);
                """
            )

            # Startup recovery: a run left in 'running' by a process that is gone was
            # interrupted by a restart. Runs of live processes (another `serve` or
            # `worker` on this database) and of other hosts are left alone (B46).
            now = time.time()
            cursor = self._conn.execute(
                "SELECT run_id, owner_process FROM nexura_studio_runs WHERE status = 'running'"
            )
            interrupted_ids: list[str] = []
            for r_id, owner in cursor.fetchall():
                if not _owner_is_gone(owner):
                    continue
                # Compare-and-set: skip a run whose owner changed since it was read.
                swept = self._conn.execute(
                    """
                    UPDATE nexura_studio_runs
                    SET status = 'interrupted',
                        error = 'Run was interrupted by server restart or shutdown.',
                        updated_at = ?
                    WHERE run_id = ? AND status = 'running' AND owner_process IS ?
                    """,
                    (now, r_id, owner),
                )
                if swept.rowcount > 0:
                    interrupted_ids.append(r_id)
            if interrupted_ids:
                for r_id in interrupted_ids:
                    # Get max seq
                    seq_cursor = self._conn.execute(
                        "SELECT COALESCE(MAX(event_seq), 0) + 1 "
                        "FROM nexura_studio_run_events WHERE run_id = ?",
                        (r_id,),
                    )
                    next_seq = seq_cursor.fetchone()[0]
                    self._conn.execute(
                        """
                        INSERT INTO nexura_studio_run_events (
                            run_id, event_seq, event_type, node_id, data_json, timestamp
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            r_id,
                            next_seq,
                            "RUN_INTERRUPTED",
                            None,
                            json.dumps(
                                {"reason": "Server shutdown / restart while run was executing."},
                                ensure_ascii=False,
                            ),
                            now,
                        ),
                    )

            # Seed default template workflow if workflows table is completely empty
            wf_count_cur = self._conn.execute("SELECT COUNT(*) FROM nexura_studio_workflows")
            if wf_count_cur.fetchone()[0] == 0:
                default_spec = get_default_workflow_spec()
                default_spec_json = default_spec.to_json(indent=2)
                self._conn.execute(
                    """
                    INSERT INTO nexura_studio_workflows (
                        workflow_id, name, revision, spec_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "wf-order-support",
                        default_spec.name,
                        1,
                        default_spec_json,
                        now,
                        now,
                    ),
                )

        # Re-use M2 SQLiteWorkflowStore against the same database file, unless the
        # workflow state lives elsewhere (a Postgres workflow store, A6 S8).
        self.checkpoint_store: Any = (
            checkpoint_store if checkpoint_store is not None else SQLiteWorkflowStore(self.db_path)
        )

    def _ensure_open(self) -> sqlite3.Connection:
        if self._closed or self._conn is None:
            raise WorkflowError("StudioStore connection is closed.")
        return self._conn

    # -------------------------------------------------------------------------
    # Workflows Synchronous Storage
    # -------------------------------------------------------------------------

    def _list_workflows_sync(self) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._ensure_open()
            cursor = conn.execute(
                """
                SELECT workflow_id, name, revision, spec_json, created_at, updated_at
                FROM nexura_studio_workflows
                ORDER BY updated_at DESC
                """
            )
            rows = cursor.fetchall()
            results: list[dict[str, Any]] = []
            for wf_id, name, rev, spec_json, c_at, u_at in rows:
                try:
                    spec_dict = json.loads(spec_json)
                    node_count = len(spec_dict.get("nodes", {}))
                    entry_node = spec_dict.get("entry_node", "")
                except Exception:
                    node_count = 0
                    entry_node = ""
                results.append(
                    {
                        "workflow_id": wf_id,
                        "name": name,
                        "revision": rev,
                        "entry_node": entry_node,
                        "node_count": node_count,
                        "created_at": c_at,
                        "updated_at": u_at,
                    }
                )
            return results

    def _get_workflow_sync(self, workflow_id: str) -> dict[str, Any] | None:
        with self._lock:
            conn = self._ensure_open()
            cursor = conn.execute(
                """
                SELECT workflow_id, name, revision, spec_json, created_at, updated_at
                FROM nexura_studio_workflows
                WHERE workflow_id = ?
                """,
                (workflow_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            wf_id, name, rev, spec_json, c_at, u_at = row
            spec_dict = json.loads(spec_json)
            spec = WorkflowSpec.from_dict(spec_dict)
            return {
                "workflow_id": wf_id,
                "name": name,
                "revision": rev,
                "spec": spec.to_dict(),
                "spec_hash": spec.canonical_hash(),
                "created_at": c_at,
                "updated_at": u_at,
            }

    def _create_workflow_sync(
        self, spec: WorkflowSpec, workflow_id: str | None = None
    ) -> dict[str, Any]:
        with self._lock:
            conn = self._ensure_open()
            wf_id = workflow_id or f"wf-{uuid.uuid4().hex[:12]}"
            # Verify no collision
            cur = conn.execute(
                "SELECT 1 FROM nexura_studio_workflows WHERE workflow_id = ?",
                (wf_id,),
            )
            if cur.fetchone() is not None:
                raise WorkflowValidationError(f"Workflow ID '{wf_id}' already exists.")

            now = time.time()
            spec_json = spec.to_json(indent=2)
            with conn:
                conn.execute(
                    """
                    INSERT INTO nexura_studio_workflows (
                        workflow_id, name, revision, spec_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (wf_id, spec.name, 1, spec_json, now, now),
                )
            return {
                "workflow_id": wf_id,
                "name": spec.name,
                "revision": 1,
                "spec": spec.to_dict(),
                "spec_hash": spec.canonical_hash(),
                "created_at": now,
                "updated_at": now,
            }

    def _update_workflow_sync(
        self, workflow_id: str, spec: WorkflowSpec, expected_revision: int
    ) -> dict[str, Any]:
        with self._lock:
            conn = self._ensure_open()
            cur = conn.execute(
                "SELECT revision, created_at FROM nexura_studio_workflows WHERE workflow_id = ?",
                (workflow_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise WorkflowValidationError(f"Workflow '{workflow_id}' not found.")
            current_rev, created_at = row
            if current_rev != expected_revision:
                raise WorkflowConflictError(
                    workflow_id=workflow_id,
                    expected_revision=expected_revision,
                    actual_revision=current_rev,
                )

            new_rev = current_rev + 1
            now = time.time()
            spec_json = spec.to_json(indent=2)
            with conn:
                conn.execute(
                    """
                    UPDATE nexura_studio_workflows
                    SET name = ?, revision = ?, spec_json = ?, updated_at = ?
                    WHERE workflow_id = ? AND revision = ?
                    """,
                    (spec.name, new_rev, spec_json, now, workflow_id, expected_revision),
                )
            return {
                "workflow_id": workflow_id,
                "name": spec.name,
                "revision": new_rev,
                "spec": spec.to_dict(),
                "spec_hash": spec.canonical_hash(),
                "created_at": created_at,
                "updated_at": now,
            }

    def _delete_workflow_sync(self, workflow_id: str) -> bool:
        with self._lock:
            conn = self._ensure_open()
            with conn:
                cur = conn.execute(
                    "DELETE FROM nexura_studio_workflows WHERE workflow_id = ?",
                    (workflow_id,),
                )
                return cur.rowcount > 0

    # -------------------------------------------------------------------------
    # Runs & Events Synchronous Storage
    # -------------------------------------------------------------------------

    def _record_run_start_sync(
        self,
        run_id: str,
        workflow_id: str | None,
        spec: WorkflowSpec,
        initial_state: dict[str, Any],
        forked_from: dict[str, Any] | None = None,
        started_by: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            conn = self._ensure_open()
            now = time.time()
            forked_json = json.dumps(forked_from, ensure_ascii=False) if forked_from else None
            started_json = json.dumps(started_by, ensure_ascii=False) if started_by else None
            spec_json = spec.to_json(indent=2)
            spec_hash = spec.canonical_hash()
            state_json = json.dumps(initial_state, ensure_ascii=False)

            with conn:
                conn.execute(
                    """
                    INSERT INTO nexura_studio_runs (
                        run_id, workflow_id, spec_hash, spec_json, status,
                        current_node_id, input_state_json, current_state_json,
                        step_count, created_at, updated_at, forked_from_json, started_by_json,
                        owner_process
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        workflow_id,
                        spec_hash,
                        spec_json,
                        "running",
                        spec.entry_node,
                        state_json,
                        state_json,
                        0,
                        now,
                        now,
                        forked_json,
                        started_json,
                        _process_owner(),
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO nexura_studio_run_events (
                        run_id, event_seq, event_type, node_id, data_json, timestamp
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        1,
                        "RUN_STARTED",
                        spec.entry_node,
                        json.dumps(
                            {"workflow_id": workflow_id, "initial_state": initial_state},
                            ensure_ascii=False,
                        ),
                        now,
                    ),
                )

    def _record_run_event_sync(
        self,
        run_id: str,
        event_type: str,
        node_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            conn = self._ensure_open()
            now = time.time()
            data_json = json.dumps(data or {}, ensure_ascii=False)
            with conn:
                seq_cur = conn.execute(
                    "SELECT COALESCE(MAX(event_seq), 0) + 1 "
                    "FROM nexura_studio_run_events WHERE run_id = ?",
                    (run_id,),
                )
                next_seq = seq_cur.fetchone()[0]
                conn.execute(
                    """
                    INSERT INTO nexura_studio_run_events (
                        run_id, event_seq, event_type, node_id, data_json, timestamp
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (run_id, next_seq, event_type, node_id, data_json, now),
                )

    def _update_run_sync(
        self,
        run_id: str,
        status: str,
        current_node_id: str | None,
        current_state: dict[str, Any],
        step_count: int,
        pending_approval: PendingApproval | None = None,
        error: str | None = None,
        pending_input: dict[str, Any] | None = None,
        cancel_reason: str | None = None,
        unless_status: Collection[str] = (),
    ) -> bool:
        with self._lock:
            conn = self._ensure_open()
            now = time.time()
            state_json = json.dumps(current_state, ensure_ascii=False)
            app_id = pending_approval.approval_id if pending_approval else None
            app_json = (
                json.dumps(pending_approval.to_dict(), ensure_ascii=False)
                if pending_approval
                else None
            )
            input_json = (
                json.dumps(pending_input, ensure_ascii=False) if pending_input is not None else None
            )

            # Compare-and-set: the row is left alone while its status is in ``unless_status``.
            guarded = sorted(set(unless_status))
            guard_sql = f" AND status NOT IN ({', '.join('?' for _ in guarded)})" if guarded else ""
            with conn:
                cur = conn.execute(
                    f"""
                    UPDATE nexura_studio_runs
                    SET status = ?, current_node_id = ?, current_state_json = ?,
                        step_count = ?, approval_id = ?, approval_json = ?,
                        error = ?, updated_at = ?, pending_input_json = ?,
                        cancel_reason = COALESCE(?, cancel_reason),
                        owner_process = COALESCE(?, owner_process)
                    WHERE run_id = ?{guard_sql}
                    """,
                    (
                        status,
                        current_node_id,
                        state_json,
                        step_count,
                        app_id,
                        app_json,
                        error,
                        now,
                        input_json,
                        cancel_reason,
                        # The process setting a run (back) to running now executes it.
                        _process_owner() if status == "running" else None,
                        run_id,
                        *guarded,
                    ),
                )
            return cur.rowcount > 0

    def _get_run_status_sync(self, run_id: str) -> str | None:
        with self._lock:
            conn = self._ensure_open()
            row = conn.execute(
                "SELECT status FROM nexura_studio_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            return str(row[0]) if row else None

    def _list_run_ids_started_by_key_sync(self, api_key_id: str) -> list[str]:
        with self._lock:
            conn = self._ensure_open()
            rows = conn.execute(
                "SELECT run_id FROM nexura_studio_runs "
                "WHERE json_extract(started_by_json, '$.api_key_id') = ?",
                (api_key_id,),
            ).fetchall()
            return [str(r[0]) for r in rows]

    @staticmethod
    def _run_filter(
        workflow_id: str | None, run_ids: Sequence[str] | None, status: str | None
    ) -> tuple[str, list[Any]]:
        """WHERE clause for run listings; the workspace id set is applied in SQL."""
        clauses: list[str] = []
        params: list[Any] = []
        if workflow_id:
            clauses.append("workflow_id = ?")
            params.append(workflow_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if run_ids is not None:
            clauses.append("run_id IN (SELECT value FROM json_each(?))")
            params.append(json.dumps(list(run_ids)))
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    def _count_runs_sync(
        self,
        workflow_id: str | None = None,
        run_ids: Sequence[str] | None = None,
        status: str | None = None,
    ) -> int:
        where, params = self._run_filter(workflow_id, run_ids, status)
        with self._lock:
            conn = self._ensure_open()
            row = conn.execute("SELECT COUNT(*) FROM nexura_studio_runs" + where, params).fetchone()
            return int(row[0]) if row else 0

    def _list_runs_sync(
        self,
        workflow_id: str | None = None,
        limit: int | None = 50,
        run_ids: Sequence[str] | None = None,
        status: str | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        where, params = self._run_filter(workflow_id, run_ids, status)
        sql = (
            "SELECT run_id, workflow_id, spec_hash, status, current_node_id, "
            "step_count, error, created_at, updated_at, current_state_json, "
            "approval_json, pending_input_json, forked_from_json, cancel_reason "
            "FROM nexura_studio_runs" + where + " ORDER BY created_at DESC, run_id DESC "
            "LIMIT ? OFFSET ?"
        )
        params = [*params, -1 if limit is None else limit, max(0, offset)]
        with self._lock:
            conn = self._ensure_open()
            cursor = conn.execute(sql, params)
            rows = cursor.fetchall()
            results: list[dict[str, Any]] = []
            for (
                r_id,
                wf_id,
                s_hash,
                status,
                node_id,
                steps,
                err,
                c_at,
                u_at,
                curr_json,
                app_json,
                input_json,
                forked_json,
                cancel_reason,
            ) in rows:
                results.append(
                    {
                        "run_id": r_id,
                        "workflow_id": wf_id,
                        "spec_hash": s_hash,
                        "status": status,
                        "current_node_id": node_id,
                        "step_count": steps,
                        "error": err,
                        "created_at": c_at,
                        "updated_at": u_at,
                        "current_state": json.loads(curr_json) if curr_json else {},
                        "pending_approval": json.loads(app_json) if app_json else None,
                        "pending_input": json.loads(input_json) if input_json else None,
                        "forked_from": json.loads(forked_json) if forked_json else None,
                        "cancel_reason": cancel_reason,
                    }
                )
            return results

    def _get_run_sync(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            conn = self._ensure_open()
            cursor = conn.execute(
                """
                SELECT run_id, workflow_id, spec_hash, spec_json, status,
                       current_node_id, input_state_json, current_state_json,
                       step_count, approval_json, error, created_at, updated_at,
                       pending_input_json, forked_from_json, cancel_reason
                FROM nexura_studio_runs
                WHERE run_id = ?
                """,
                (run_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None

            (
                r_id,
                wf_id,
                s_hash,
                s_json,
                status,
                node_id,
                in_state_json,
                curr_state_json,
                steps,
                app_json,
                err,
                c_at,
                u_at,
                input_json,
                forked_json,
                cancel_reason,
            ) = row

            in_state = json.loads(in_state_json)
            curr_state = json.loads(curr_state_json)
            pending_app = json.loads(app_json) if app_json else None
            spec_dict = json.loads(s_json)

            # Fetch ordered events
            ev_cursor = conn.execute(
                """
                SELECT event_seq, event_type, node_id, data_json, timestamp
                FROM nexura_studio_run_events
                WHERE run_id = ?
                ORDER BY event_seq ASC
                """,
                (run_id,),
            )
            events: list[dict[str, Any]] = []
            for seq, ev_type, ev_node, d_json, ts in ev_cursor.fetchall():
                events.append(
                    {
                        "seq": seq,
                        "type": ev_type,
                        "node_id": ev_node,
                        "data": json.loads(d_json),
                        "timestamp": ts,
                    }
                )

            return {
                "run_id": r_id,
                "workflow_id": wf_id,
                "spec_hash": s_hash,
                "spec": spec_dict,
                "status": status,
                "current_node_id": node_id,
                "step_count": steps,
                "input_state": in_state,
                "current_state": curr_state,
                "pending_approval": pending_app,
                "pending_input": json.loads(input_json) if input_json else None,
                "forked_from": json.loads(forked_json) if forked_json else None,
                "error": err,
                "cancel_reason": cancel_reason,
                "events": events,
                "created_at": c_at,
                "updated_at": u_at,
            }

    def _close_connection(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                if self._conn is not None:
                    self._conn.close()
                    self._conn = None

    def _close_sync(self) -> None:
        self._close_connection()
        self.checkpoint_store.close_sync()

    # -------------------------------------------------------------------------
    # Asynchronous Interface
    # -------------------------------------------------------------------------

    async def list_workflows(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_workflows_sync)

    async def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_workflow_sync, workflow_id)

    async def create_workflow(
        self, spec: WorkflowSpec, workflow_id: str | None = None
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self._create_workflow_sync, spec, workflow_id)

    async def update_workflow(
        self, workflow_id: str, spec: WorkflowSpec, expected_revision: int
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._update_workflow_sync, workflow_id, spec, expected_revision
        )

    async def delete_workflow(self, workflow_id: str) -> bool:
        return await asyncio.to_thread(self._delete_workflow_sync, workflow_id)

    async def record_run_start(
        self,
        run_id: str,
        workflow_id: str | None,
        spec: WorkflowSpec,
        initial_state: dict[str, Any],
        forked_from: dict[str, Any] | None = None,
        started_by: dict[str, Any] | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._record_run_start_sync,
            run_id,
            workflow_id,
            spec,
            initial_state,
            forked_from,
            started_by,
        )

    async def record_run_event(
        self,
        run_id: str,
        event_type: str,
        node_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        await asyncio.to_thread(self._record_run_event_sync, run_id, event_type, node_id, data)

    async def update_run(
        self,
        run_id: str,
        status: str,
        current_node_id: str | None,
        current_state: dict[str, Any],
        step_count: int,
        pending_approval: PendingApproval | None = None,
        error: str | None = None,
        pending_input: dict[str, Any] | None = None,
        cancel_reason: str | None = None,
        unless_status: Collection[str] = (),
    ) -> bool:
        """Replace the run row's live fields; ``cancel_reason`` is kept once set.

        With ``unless_status`` the write is a compare-and-set: it is skipped when the stored
        status is one of those (B40: a cancelled run never becomes paused or completed again).
        Returns whether the row was written.
        """
        return await asyncio.to_thread(
            self._update_run_sync,
            run_id,
            status,
            current_node_id,
            current_state,
            step_count,
            pending_approval,
            error,
            pending_input,
            cancel_reason,
            unless_status,
        )

    async def get_run_status(self, run_id: str) -> str | None:
        """The run's status without loading its events, or None if unknown."""
        return await asyncio.to_thread(self._get_run_status_sync, run_id)

    async def list_run_ids_started_by_key(self, api_key_id: str) -> list[str]:
        """Ids of runs whose recorded starter is the API key ``api_key_id``."""
        return await asyncio.to_thread(self._list_run_ids_started_by_key_sync, api_key_id)

    async def list_runs(
        self,
        workflow_id: str | None = None,
        limit: int | None = 50,
        run_ids: Sequence[str] | None = None,
        status: str | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self._list_runs_sync, workflow_id, limit, run_ids, status, offset
        )

    async def count_runs(
        self,
        workflow_id: str | None = None,
        run_ids: Sequence[str] | None = None,
        status: str | None = None,
    ) -> int:
        return await asyncio.to_thread(self._count_runs_sync, workflow_id, run_ids, status)

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_run_sync, run_id)

    async def close(self) -> None:
        if isinstance(self.checkpoint_store, SQLiteWorkflowStore):
            await asyncio.to_thread(self._close_sync)
            return
        # Another checkpoint store (Postgres) closes on the event loop its pool runs on.
        await asyncio.to_thread(self._close_connection)
        await self.checkpoint_store.close()
