# SPDX-License-Identifier: Apache-2.0
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
import logging
import os
import socket
import sqlite3
import threading
import time
import uuid
from collections.abc import Collection, Iterator, Sequence
from pathlib import Path
from typing import Any

from nexura_agents.errors import (
    WorkflowConflictError,
    WorkflowError,
    WorkflowValidationError,
)

from nexura_runtime.blobs.binding import SQLiteBlobBinding
from nexura_runtime.blobs.payloads import RUN_EVENT_NAMESPACE, DeferredPayload, decode_json
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
#
# A6 S9: a run the service journals (``journal_workspace`` set when it was recorded) is
# never swept here. Whether it is interrupted is decided from its execution-journal lease
# by the service (``nexura_runtime.service.run_liveness``), on any backend. The sweep below
# stays for legacy rows: runs recorded before S9 or by a caller that does not journal.
# ---------------------------------------------------------------------------

_PROC = Path("/proc")
logger = logging.getLogger(__name__)


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


def this_process_owner() -> str:
    """The ``owner_process`` value runs started or resumed by this process record."""
    return _process_owner()


def owner_process_is_gone(owner: str | None) -> bool:
    """True when a run row's ``owner_process`` is known to be gone (the B46 rule).

    The A6 S9 service falls back to it for a journaled run whose journal cannot tell (no
    journal row yet, or the journal moved on while the run row still says ``running``).
    """
    return _owner_is_gone(owner)


@contextlib.contextmanager
def _run_read_snapshot(conn: sqlite3.Connection) -> Iterator[None]:
    """Keep run and event reads on one SQLite snapshot during retention."""
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN")
    try:
        yield
    finally:
        if owns_transaction:
            conn.rollback()


#: Error text and event reason of the open-time sweep of legacy runs (shared by the backends).
SWEEP_ERROR = "Run was interrupted by server restart or shutdown."
SWEEP_REASON = "Server shutdown / restart while run was executing."
#: The workflow seeded into an empty workflow table.
DEFAULT_WORKFLOW_ID = "wf-order-support"

# Column lists and row converters shared by the SQLite store and the Postgres store
# (``nexura_postgres.PostgresStudioStore``), so both return the same dictionaries.
WORKFLOW_COLUMNS = "workflow_id, name, revision, spec_json, created_at, updated_at"
RUN_SUMMARY_COLUMNS = (
    "run_id, workflow_id, spec_hash, status, current_node_id, "
    "step_count, error, created_at, updated_at, current_state_json, "
    "approval_json, pending_input_json, forked_from_json, cancel_reason, "
    "pending_effect_json"
)
RUN_DETAIL_COLUMNS = (
    "run_id, workflow_id, spec_hash, spec_json, status, "
    "current_node_id, input_state_json, current_state_json, "
    "step_count, approval_json, error, created_at, updated_at, "
    "pending_input_json, forked_from_json, cancel_reason, "
    "pending_effect_json, journal_workspace"
)
EVENT_COLUMNS = "event_seq, event_type, node_id, data_json, timestamp"


def workflow_summary(row: Sequence[Any]) -> dict[str, Any]:
    """A workflow list entry from a ``WORKFLOW_COLUMNS`` row."""
    wf_id, name, rev, spec_json, c_at, u_at = row
    try:
        spec_dict = json.loads(spec_json)
        node_count = len(spec_dict.get("nodes", {}))
        entry_node = spec_dict.get("entry_node", "")
    except Exception:
        node_count = 0
        entry_node = ""
    return {
        "workflow_id": wf_id,
        "name": name,
        "revision": rev,
        "entry_node": entry_node,
        "node_count": node_count,
        "created_at": c_at,
        "updated_at": u_at,
    }


def workflow_detail(row: Sequence[Any]) -> dict[str, Any]:
    """A workflow with its parsed spec from a ``WORKFLOW_COLUMNS`` row."""
    wf_id, name, rev, spec_json, c_at, u_at = row
    spec = WorkflowSpec.from_dict(json.loads(spec_json))
    return {
        "workflow_id": wf_id,
        "name": name,
        "revision": rev,
        "spec": spec.to_dict(),
        "spec_hash": spec.canonical_hash(),
        "created_at": c_at,
        "updated_at": u_at,
    }


def run_summary(row: Sequence[Any]) -> dict[str, Any]:
    """A run list entry from a ``RUN_SUMMARY_COLUMNS`` row."""
    (
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
        effect_json,
    ) = row
    pending_effect = json.loads(effect_json) if effect_json else None
    return {
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
        "pending_effect": pending_effect,
        "pause_reason": "effect_unknown" if pending_effect else None,
    }


def run_event(row: Sequence[Any], raw_data: bool) -> dict[str, Any]:
    """An event from an ``EVENT_COLUMNS`` row; with ``raw_data`` "data" keeps the stored text."""
    seq, ev_type, ev_node, d_json, ts = row
    return {
        "seq": seq,
        "type": ev_type,
        "node_id": ev_node,
        "data": d_json if raw_data else json.loads(d_json),
        "timestamp": ts,
    }


def run_detail(row: Sequence[Any], events: list[dict[str, Any]], raw_data: bool) -> dict[str, Any]:
    """A run with its events from a ``RUN_DETAIL_COLUMNS`` row."""
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
        effect_json,
        run_journal_workspace,
    ) = row
    pending_effect = json.loads(effect_json) if effect_json else None
    run: dict[str, Any] = {
        "run_id": r_id,
        "workflow_id": wf_id,
        "spec_hash": s_hash,
        "spec": json.loads(s_json),
        "status": status,
        "current_node_id": node_id,
        "step_count": steps,
        "input_state": json.loads(in_state_json),
        "current_state": json.loads(curr_state_json),
        "pending_approval": json.loads(app_json) if app_json else None,
        "pending_input": json.loads(input_json) if input_json else None,
        "forked_from": json.loads(forked_json) if forked_json else None,
        "error": err,
        "cancel_reason": cancel_reason,
        "pending_effect": pending_effect,
        "pause_reason": "effect_unknown" if pending_effect else None,
        "events": events,
        "created_at": c_at,
        "updated_at": u_at,
    }
    if raw_data:
        run["_journal_workspace"] = run_journal_workspace
    return run


class StudioStore:
    """Manages SQLite storage for Studio definitions, runs, and event streams."""

    def __init__(
        self, db_path: str | Path, *, checkpoint_store: Any = None, blob_payloads: Any = None
    ) -> None:
        """Open the Studio database at ``db_path``.

        ``checkpoint_store``: where workflow checkpoints live (for example
        ``nexura_postgres.PostgresWorkflowStore``); the store then owns it and closes it
        with ``close()``. Default: a ``SQLiteWorkflowStore`` on the same database file.

        ``blob_payloads``: a ``nexura_runtime.blobs.BlobPayloads``. Run-event payloads above
        its threshold are then stored as blobs under the run's workspace, and ``data_json``
        keeps a reference with a short preview (MVP WP2b). The service sets it; without it
        every payload stays inline, as before. Inline rows always read as before.
        """
        self.db_path = Path(db_path).resolve()
        self._blob_payloads: Any = None
        self.blob_payloads = blob_payloads
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
                # journal_workspace: the workspace the run's execution journal is keyed by
                # (A6 S9); NULL for legacy runs, which the open-time sweep still handles.
                "journal_workspace",
                # pending_effect_json: the effect a run paused on (pause_reason effect_unknown).
                "pending_effect_json",
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

            # Startup recovery of legacy runs: a run left in 'running' by a process that is
            # gone was interrupted by a restart. Runs of live processes (another `serve` or
            # `worker` on this database) and of other hosts are left alone (B46). Journaled
            # runs are decided by their run lease instead (A6 S9, see the module comment).
            now = time.time()
            cursor = self._conn.execute(
                "SELECT run_id, owner_process FROM nexura_studio_runs "
                "WHERE status = 'running' AND journal_workspace IS NULL"
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
                        error = ?,
                        updated_at = ?
                    WHERE run_id = ? AND status = 'running' AND owner_process IS ?
                      AND journal_workspace IS NULL
                    """,
                    (SWEEP_ERROR, now, r_id, owner),
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
                            json.dumps({"reason": SWEEP_REASON}, ensure_ascii=False),
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
                        DEFAULT_WORKFLOW_ID,
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

    #: One database file per host: run liveness uses the B46 owner rule only.
    shared_across_instances = False

    def this_owner(self) -> str:
        """The ``owner_process`` value runs started or resumed by this process record."""
        return _process_owner()

    def owner_is_gone(self, owner: str | None) -> bool:
        """The B46 rule: a run of this host whose process is gone (or a row without an owner)."""
        return _owner_is_gone(owner)

    @property
    def blob_payloads(self) -> Any:
        return self._blob_payloads

    @blob_payloads.setter
    def blob_payloads(self, codec: Any) -> None:
        """Set the codec; this database is registered as one its references live in, so it
        records the store identity before its first blob reference (``blobs.binding``)."""
        self._blob_payloads = codec
        if codec is not None and hasattr(codec, "add_binding"):
            codec.add_binding(SQLiteBlobBinding(self.db_path))

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
                f"SELECT {WORKFLOW_COLUMNS} FROM nexura_studio_workflows ORDER BY updated_at DESC"
            )
            return [workflow_summary(row) for row in cursor.fetchall()]

    def _get_workflow_sync(self, workflow_id: str) -> dict[str, Any] | None:
        with self._lock:
            conn = self._ensure_open()
            cursor = conn.execute(
                f"SELECT {WORKFLOW_COLUMNS} FROM nexura_studio_workflows WHERE workflow_id = ?",
                (workflow_id,),
            )
            row = cursor.fetchone()
            return None if row is None else workflow_detail(row)

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
        journal_workspace: str | None = None,
    ) -> None:
        # A large initial state goes to the blob store before the database lock is taken.
        started_json = self._event_data_text(
            journal_workspace,
            {"workflow_id": workflow_id, "initial_state": initial_state},
        )
        with self._lock:
            conn = self._ensure_open()
            now = time.time()
            forked_json = json.dumps(forked_from, ensure_ascii=False) if forked_from else None
            started_by_json = json.dumps(started_by, ensure_ascii=False) if started_by else None
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
                        owner_process, journal_workspace
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        started_by_json,
                        _process_owner(),
                        journal_workspace,
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
                        started_json,
                        now,
                    ),
                )

    def _run_workspace(self, run_id: str) -> str | None:
        """The workspace a run's blobs are filed under (its journal workspace), if recorded."""
        with self._lock:
            row = (
                self._ensure_open()
                .execute(
                    "SELECT journal_workspace FROM nexura_studio_runs WHERE run_id = ?", (run_id,)
                )
                .fetchone()
            )
        return row[0] if row else None

    def _event_data_text(self, workspace_id: str | None, data: Any) -> str:
        """``data_json`` for a run event: inline JSON, or a blob reference when it is large.

        Blobs of runs that have no journal workspace (runs recorded outside the service) are
        filed under ``default``, the local owner's workspace.
        """
        text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        if self.blob_payloads is None:
            return text
        return str(self.blob_payloads.encode(workspace_id or "default", RUN_EVENT_NAMESPACE, text))

    def _record_run_event_sync(
        self,
        run_id: str,
        event_type: str,
        node_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        data_json = json.dumps(data or {}, ensure_ascii=False)
        codec = self.blob_payloads
        if codec is not None and codec.needs_offload(data_json):
            # The blob is written outside the database lock; an event that is then not
            # recorded (no parent run) leaves an unreferenced blob for blob retention.
            data_json = self._event_data_text(self._run_workspace(run_id), data_json)
        with self._lock:
            conn = self._ensure_open()
            now = time.time()
            with conn:
                # Serialize the parent check and append with retention's DELETEs.
                # Preserve legacy batching: commit the caller's existing write
                # transaction with this append, as the original method did.
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                if (
                    conn.execute(
                        "SELECT 1 FROM nexura_studio_runs WHERE run_id = ?", (run_id,)
                    ).fetchone()
                    is None
                ):
                    logger.warning(
                        "Run event %s was not recorded: parent run %s does not exist",
                        event_type,
                        run_id,
                    )
                    return
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
        pending_effect: dict[str, Any] | None = None,
    ) -> bool:
        with self._lock:
            conn = self._ensure_open()
            now = time.time()
            state_json = json.dumps(current_state, ensure_ascii=False)
            effect_json = (
                json.dumps(pending_effect, ensure_ascii=False)
                if pending_effect is not None
                else None
            )
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
                        pending_effect_json = ?,
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
                        effect_json,
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
            f"SELECT {RUN_SUMMARY_COLUMNS} FROM nexura_studio_runs"
            + where
            + " ORDER BY created_at DESC, run_id DESC LIMIT ? OFFSET ?"
        )
        params = [*params, -1 if limit is None else limit, max(0, offset)]
        with self._lock:
            conn = self._ensure_open()
            return [run_summary(row) for row in conn.execute(sql, params).fetchall()]

    def _get_run_sync(
        self, run_id: str, payloads: bool = True, payloads_after_seq: int = 0
    ) -> dict[str, Any] | None:
        codec = self.blob_payloads
        run = self._read_run_sync(run_id, raw_data=codec is not None)
        if run is None or codec is None:
            return run
        # Blob references are followed after the database lock is released, and only for the
        # events the caller wants; the others stay DeferredPayload placeholders.
        workspace = run.pop("_journal_workspace") or "default"
        for event in run["events"]:
            stored = event["data"]
            if codec.reference(stored, workspace) is None:
                event["data"] = json.loads(stored)  # inline (or not this workspace's marker)
            elif payloads and int(event.get("seq") or 0) > payloads_after_seq:
                event["data"] = decode_json(codec, stored, workspace)
            else:
                event["data"] = DeferredPayload(stored, workspace)
        return run

    def _load_payloads_sync(self, run: dict[str, Any], after_seq: int) -> dict[str, Any]:
        codec = self.blob_payloads
        if codec is None:
            return run
        for event in run.get("events") or []:
            data = event.get("data")
            if isinstance(data, DeferredPayload) and int(event.get("seq") or 0) > after_seq:
                event["data"] = decode_json(codec, data.stored, data.workspace_id)
        return run

    def _read_run_sync(self, run_id: str, *, raw_data: bool = False) -> dict[str, Any] | None:
        with self._lock, _run_read_snapshot(self._ensure_open()):
            conn = self._ensure_open()
            row = conn.execute(
                f"SELECT {RUN_DETAIL_COLUMNS} FROM nexura_studio_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            ev_rows = conn.execute(
                f"SELECT {EVENT_COLUMNS} FROM nexura_studio_run_events "
                "WHERE run_id = ? ORDER BY event_seq ASC",
                (run_id,),
            ).fetchall()
            # With raw_data, "data" holds the stored text until _get_run_sync decodes it.
            return run_detail(row, [run_event(ev, raw_data) for ev in ev_rows], raw_data)

    def _list_journaled_runs_sync(
        self,
        statuses: Sequence[str],
        workspace_id: str | None,
        run_ids: Sequence[str] | None,
    ) -> list[dict[str, Any]]:
        clauses = [
            "journal_workspace IS NOT NULL",
            f"status IN ({', '.join('?' for _ in statuses)})",
        ]
        params: list[Any] = list(statuses)
        if workspace_id is not None:
            clauses.append("journal_workspace = ?")
            params.append(workspace_id)
        if run_ids is not None:
            clauses.append("run_id IN (SELECT value FROM json_each(?))")
            params.append(json.dumps(list(run_ids)))
        with self._lock:
            conn = self._ensure_open()
            rows = conn.execute(
                "SELECT run_id, status, journal_workspace, owner_process "
                "FROM nexura_studio_runs WHERE " + " AND ".join(clauses),
                params,
            ).fetchall()
        return [
            {"run_id": r[0], "status": r[1], "journal_workspace": r[2], "owner_process": r[3]}
            for r in rows
        ]

    def _mark_interrupted_sync(self, run_id: str, error: str, reason: str) -> bool:
        with self._lock:
            conn = self._ensure_open()
            now = time.time()
            with conn:
                cur = conn.execute(
                    """
                    UPDATE nexura_studio_runs
                    SET status = 'interrupted', error = ?, updated_at = ?
                    WHERE run_id = ? AND status = 'running'
                    """,
                    (error, now, run_id),
                )
                if cur.rowcount == 0:
                    return False
                next_seq = conn.execute(
                    "SELECT COALESCE(MAX(event_seq), 0) + 1 "
                    "FROM nexura_studio_run_events WHERE run_id = ?",
                    (run_id,),
                ).fetchone()[0]
                conn.execute(
                    """
                    INSERT INTO nexura_studio_run_events (
                        run_id, event_seq, event_type, node_id, data_json, timestamp
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        next_seq,
                        "RUN_INTERRUPTED",
                        None,
                        json.dumps({"reason": reason}, ensure_ascii=False),
                        now,
                    ),
                )
            return True

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
        journal_workspace: str | None = None,
    ) -> None:
        """Record a new ``running`` run owned by this process.

        ``journal_workspace``: the workspace its execution journal is keyed by, when the
        engine journals it (A6 S9). Such a run is never swept when a store opens; the
        service decides from its run lease whether it is interrupted.
        """
        await asyncio.to_thread(
            self._record_run_start_sync,
            run_id,
            workflow_id,
            spec,
            initial_state,
            forked_from,
            started_by,
            journal_workspace,
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
        pending_effect: dict[str, Any] | None = None,
    ) -> bool:
        """Replace the run row's live fields; ``cancel_reason`` is kept once set.

        ``pending_effect``: the effect a ``paused`` run waits on (``pause_reason``
        ``effect_unknown``); cleared by any write without it.

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
            pending_effect,
        )

    async def list_journaled_runs(
        self,
        statuses: Sequence[str] = ("running", "interrupted"),
        *,
        workspace_id: str | None = None,
        run_ids: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Journaled runs (A6 S9) in ``statuses``: run id, status, journal workspace, owner."""
        return await asyncio.to_thread(
            self._list_journaled_runs_sync, tuple(statuses), workspace_id, run_ids
        )

    async def mark_interrupted(self, run_id: str, *, error: str, reason: str) -> bool:
        """Compare-and-set ``running`` -> ``interrupted`` plus a ``RUN_INTERRUPTED`` event.

        Returns False (nothing written) when the run is no longer ``running``.
        """
        return await asyncio.to_thread(self._mark_interrupted_sync, run_id, error, reason)

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

    async def get_run(
        self, run_id: str, *, payloads: bool = True, payloads_after_seq: int = 0
    ) -> dict[str, Any] | None:
        """The run with its events.

        ``payloads=False`` reads no blob: an offloaded event payload is then a
        ``DeferredPayload`` placeholder. Use it for status checks and before an ownership check;
        ``load_payloads`` reads the payloads afterwards. ``payloads_after_seq`` reads only the
        payloads of events after that sequence number (live streams).
        """
        return await asyncio.to_thread(self._get_run_sync, run_id, payloads, payloads_after_seq)

    async def load_payloads(self, run: dict[str, Any], *, after_seq: int = 0) -> dict[str, Any]:
        """Replace the ``DeferredPayload`` placeholders of ``run`` (events after ``after_seq``)."""
        if not any(isinstance(e.get("data"), DeferredPayload) for e in run.get("events") or []):
            return run
        return await asyncio.to_thread(self._load_payloads_sync, run, after_seq)

    async def close(self) -> None:
        if isinstance(self.checkpoint_store, SQLiteWorkflowStore):
            await asyncio.to_thread(self._close_sync)
            return
        # Another checkpoint store (Postgres) closes on the event loop its pool runs on.
        await asyncio.to_thread(self._close_connection)
        await self.checkpoint_store.close()
