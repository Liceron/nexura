# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Durable agent runs: checkpoint stores.

A ``Runner(checkpoint_store=...)`` writes a checkpoint of the run after every step that
matters (see ``docs/durable-runs.md``). A checkpoint is a JSON envelope around a
``RunInterruption`` payload, so the format is the one approvals already use. After a crash,
``Runner.recover_async(agent, run_id)`` continues from the last checkpoint.

Checkpoints contain the full conversation transcript. Protect them like session data.

With the memory, SQLite and Postgres stores the runner also keeps an execution journal
(``nexura_agents.execution_store``) next to the checkpoints: the run's fenced lease from
its first write, one intent per non-pure tool call written just before the call, and
effect records (Execution Contract v1, ``docs/durable-execution.md``).
"""

from __future__ import annotations

import functools
import hashlib
import inspect
import json
import os
import socket
import sqlite3
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from nexura_agents._lazy import LazyModule
from nexura_agents.errors import (
    ApprovalDecisionError,
    CheckpointLeaseLostError,
    RunRecoveryError,
    StorageError,
)

if TYPE_CHECKING:
    import asyncio

    from nexura_agents.execution_store import ExecutionStore
else:
    asyncio = LazyModule("asyncio")

CHECKPOINT_SCHEMA_VERSION = 1
PHASES = ("model_next", "tools_pending", "paused", "completed")
DEFAULT_CLAIM_LEASE_SECONDS = 3600.0
DEFAULT_WORKSPACE = "default"


@dataclass(frozen=True)
class ResolvedOutcome:
    """A person's answer for a tool call whose outcome was unknown after a crash.

    Pass it to ``Runner.resume_async(agent, interruption, outcomes={call_id: ...})`` when
    ``interruption.pause_reason == "effect_unknown"``:

    - ``ResolvedOutcome(result)``: the action happened; ``result`` becomes the tool result
      and the call is not run again.
    - ``ResolvedOutcome.not_applied()``: the action did not happen; the call is sent again
      (with a new idempotency key). A tool that requires approval pauses for a fresh one.
    - ``ResolvedOutcome.failed(reason)``: the action failed; the run fails.

    With a journal-capable store the resolution is recorded on the call's effect record.
    """

    result: Any = None
    resolution: Literal["confirmed", "not_applied", "failed"] = "confirmed"
    reason: str = ""

    def __post_init__(self) -> None:
        if self.resolution not in ("confirmed", "not_applied", "failed"):
            raise ValueError("resolution must be 'confirmed', 'not_applied' or 'failed'")
        if not isinstance(self.reason, str):
            raise TypeError("reason must be a string")

    @classmethod
    def not_applied(cls) -> ResolvedOutcome:
        """The action did not happen: send the call again."""
        return cls(resolution="not_applied")

    @classmethod
    def failed(cls, reason: str = "") -> ResolvedOutcome:
        """The action failed: the run fails with this reason."""
        return cls(resolution="failed", reason=reason)


# ---------------------------------------------------------------- lease owners

_OWNER_PREFIX = "nx1"


@functools.cache
def _host_identity() -> str:
    """Hostname, boot id and pid namespace, hashed: processes that can see each other's pids.

    Without a boot id (macOS, BSD, Windows, a /proc-less sandbox) the hostname alone could
    match another machine (cloned images, ``localhost``), so the identity starts with
    ``u`` ("unverifiable") and ``lease_owner_is_dead`` never declares such an owner dead.
    """
    parts = [socket.gethostname()]
    try:
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        boot_id = ""
    parts.append(boot_id)
    try:
        parts.append(str(os.stat("/proc/self/ns/pid").st_ino))
    except OSError:
        parts.append("")
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()
    return digest[:16] if boot_id else "u" + digest[:15]


def _proc_stat(pid: int) -> list[str]:
    """Fields after the command name in /proc/<pid>/stat (state first); OSError if gone."""
    raw = Path(f"/proc/{pid}/stat").read_text()
    return raw[raw.rfind(")") + 2 :].split()


def _start_ticks(pid: int) -> str:
    try:
        fields = _proc_stat(pid)
    except OSError:
        return "0"
    return fields[19] if len(fields) > 19 else "0"


def new_lease_owner() -> str:
    """A unique run-lease owner that also names this process (host identity, pid, start).

    The process part lets a recoverer on the same host see that the owner process is gone
    (``lease_owner_is_dead``) and take the run over at once instead of waiting for the
    lease to expire. The uuid keeps every owner unique, so a lost lease is never re-used.
    """
    pid = os.getpid()
    return f"{_OWNER_PREFIX}.{_host_identity()}.{pid}.{_start_ticks(pid)}.{uuid.uuid4().hex}"


def lease_owner_is_dead(owner: str | None) -> bool:
    """True only when ``owner`` is a process on this host (same pid namespace) that has exited.

    Conservative: owners from other hosts, other pid namespaces, older formats, hosts
    without a boot id (the hostname alone does not identify a machine) and Windows are
    never reported dead, so their leases are only taken over after they expire. A reused
    pid is detected by its start time on Linux; zombies (killed, not yet reaped) count as
    dead. A pid hidden by ``/proc`` (``hidepid``) is confirmed with ``kill(pid, 0)``:
    ``EPERM`` or success means alive.
    """
    if not isinstance(owner, str):
        return False
    parts = owner.split(".")
    identity = _host_identity()
    if (
        len(parts) != 5
        or parts[0] != _OWNER_PREFIX
        or parts[1] != identity
        or identity.startswith("u")
    ):
        return False
    try:
        pid = int(parts[2])
    except ValueError:
        return False
    if pid <= 0 or pid == os.getpid() or sys.platform == "win32":
        return False
    if Path("/proc/self/stat").exists():
        try:
            fields = _proc_stat(pid)
        except FileNotFoundError:
            # Gone, or invisible to us (/proc mounted hidepid=2): ask the kernel.
            return _kill_says_gone(pid)
        except OSError:
            return False
        if not fields or len(fields) <= 19:
            return False
        if parts[3] != "0" and fields[19] != parts[3]:
            return True  # the pid now belongs to another process
        return fields[0] in ("Z", "X", "x")
    return _kill_says_gone(pid)


def _kill_says_gone(pid: int) -> bool:
    """``kill(pid, 0)``: ESRCH means no such process; EPERM or success means it is alive."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


@runtime_checkable
class RunCheckpointStore(Protocol):
    """Where a durable runner keeps the latest checkpoint of each unfinished run."""

    async def save(self, run_id: str, checkpoint_json: str) -> None:
        """Atomically replace the checkpoint of ``run_id``."""
        ...

    async def load(self, run_id: str) -> str | None:
        """Return the checkpoint of ``run_id``, or None when there is none."""
        ...

    async def delete(self, run_id: str) -> None:
        """Remove the checkpoint of ``run_id`` (no error when it does not exist)."""
        ...

    async def list_incomplete(self, limit: int = 100) -> list[str]:
        """Run ids that still have a checkpoint, oldest update first."""
        ...

    # Optional (stores without them skip recovery claims; concurrent recoverers are then
    # the caller's responsibility): ``claim(run_id, owner, lease_seconds) -> bool`` takes an
    # exclusive recovery lease atomically, ``release(run_id, owner)`` gives it back.
    # A store whose ``save`` accepts ``owner=`` is passed the recovery lease owner (None
    # outside recovery) and refuses stale writes with ``CheckpointLeaseLostError`` (see
    # ``check_fence``); a save by the lease holder renews the lease. Likewise
    # ``delete(run_id, owner=..., fenced=True)`` is refused for a stale caller; a plain
    # ``delete(run_id)`` (an operator purge) always deletes.


def check_fence(
    run_id: str, owner: str | None, held: tuple[str, float] | None, *, now: float | None = None
) -> None:
    """Refuse a checkpoint write that could overwrite another caller's newer progress.

    ``held`` is the current recovery lease as ``(holder, expires_at)``, or None. A writer
    without a lease (``owner=None``: the original run or ``resume``) is refused while
    another caller's lease is live. A recoverer (``owner`` set) is refused once its lease
    is gone or belongs to someone else, even if that lease has expired since.
    """
    if held is None:
        if owner is not None:
            raise CheckpointLeaseLostError(
                f"Recovery lease of run '{run_id}' is gone; checkpoint not written."
            )
        return
    holder, expires_at = held
    if holder == owner:
        return
    if owner is not None or expires_at > (time.time() if now is None else now):
        raise CheckpointLeaseLostError(
            f"Run '{run_id}' is being recovered by another caller; checkpoint not written."
        )


class MemoryCheckpointStore:
    """In-process store for tests and demos; checkpoints do not survive the process.

    Its execution journal is a ``MemoryExecutionStore`` (pass ``execution_store=`` to share
    one between stores of the same process). Journal rows of finished runs stay in memory
    for the life of the store.
    """

    def __init__(
        self,
        *,
        execution_store: ExecutionStore | None = None,
        workspace_id: str = DEFAULT_WORKSPACE,
    ) -> None:
        if not isinstance(workspace_id, str) or not workspace_id:
            raise ValueError("workspace_id must be a nonempty string")
        self.workspace_id = workspace_id
        self._execution_store = execution_store
        self._data: dict[str, tuple[float, str]] = {}
        self._claims: dict[str, tuple[str, float, float]] = {}  # owner, expires_at, lease
        self._approvals: dict[str, tuple[str, float, bool]] = {}
        self._approval_lock = threading.Lock()

    async def open_execution_store(self) -> ExecutionStore:
        """The execution journal the runner keeps next to these checkpoints."""
        if self._execution_store is None:
            from nexura_agents.execution_store import MemoryExecutionStore

            self._execution_store = MemoryExecutionStore()
        return self._execution_store

    async def save_approval(self, approval_id: str, payload: str, expires_at: float) -> None:
        """Keep immutable approval identity independently of checkpoint cleanup."""
        with self._approval_lock:
            old = self._approvals.get(approval_id)
            if old is not None and old[:2] != (payload, expires_at):
                raise ApprovalDecisionError("Stored interruption differs from approval envelope.")
            if old is None:
                self._approvals[approval_id] = (payload, expires_at, False)

    async def load_approval(self, approval_id: str) -> str | None:
        """Return the immutable approval payload, including after consumption."""
        with self._approval_lock:
            entry = self._approvals.get(approval_id)
            return entry[0] if entry is not None else None

    async def consume_approval(self, approval_id: str, payload: str) -> bool:
        """Atomically consume a matching, unexpired approval once."""
        with self._approval_lock:
            entry = self._approvals.get(approval_id)
            if entry is None or entry[0] != payload or entry[1] <= time.time() or entry[2]:
                return False
            self._approvals[approval_id] = (entry[0], entry[1], True)
            return True

    async def claim(
        self, run_id: str, owner: str, lease_seconds: float = DEFAULT_CLAIM_LEASE_SECONDS
    ) -> bool:
        """Take the exclusive recovery lease of ``run_id``; False when another owner holds it."""
        now = time.time()
        held = self._claims.get(run_id)
        if held is not None and held[1] > now and held[0] != owner:
            return False
        self._claims[run_id] = (owner, now + lease_seconds, lease_seconds)
        return True

    async def release(self, run_id: str, owner: str) -> None:
        """Give the lease back (no-op when ``owner`` does not hold it)."""
        held = self._claims.get(run_id)
        if held is not None and held[0] == owner:
            del self._claims[run_id]

    async def save(self, run_id: str, checkpoint_json: str, owner: str | None = None) -> None:
        """Store the checkpoint of ``run_id``; fenced by the recovery lease."""
        held = self._claims.get(run_id)
        check_fence(run_id, owner, None if held is None else held[:2])
        now = time.time()
        self._data[run_id] = (now, checkpoint_json)
        if held is not None and owner is not None:
            self._claims[run_id] = (owner, now + held[2], held[2])

    async def load(self, run_id: str) -> str | None:
        """Return the checkpoint of ``run_id`` or None."""
        entry = self._data.get(run_id)
        return entry[1] if entry else None

    async def delete(self, run_id: str, owner: str | None = None, *, fenced: bool = False) -> None:
        """Remove the checkpoint of ``run_id`` if present (``fenced``: only if not stale)."""
        if fenced:
            held = self._claims.get(run_id)
            check_fence(run_id, owner, None if held is None else held[:2])
        self._data.pop(run_id, None)
        self._claims.pop(run_id, None)

    async def list_incomplete(self, limit: int = 100) -> list[str]:
        """Run ids with a checkpoint, oldest update first."""
        ordered = sorted(self._data.items(), key=lambda item: item[1][0])
        return [run_id for run_id, _ in ordered[: max(limit, 0)]]


# Version 2 (A6 S5 review, H2): checkpoints, claims and approvals are scoped by workspace_id.
_SCHEMA_VERSION = 2
_SQLITE_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS nexura_run_checkpoints (workspace_id TEXT NOT NULL, "
    "run_id TEXT NOT NULL, data TEXT NOT NULL, updated_at REAL NOT NULL, "
    "PRIMARY KEY (workspace_id, run_id))",
    "CREATE TABLE IF NOT EXISTS nexura_run_claims (workspace_id TEXT NOT NULL, "
    "run_id TEXT NOT NULL, owner TEXT NOT NULL, expires_at REAL NOT NULL, lease_seconds REAL, "
    "PRIMARY KEY (workspace_id, run_id))",
    "CREATE TABLE IF NOT EXISTS nexura_sdk_approvals (workspace_id TEXT NOT NULL, "
    "approval_id TEXT NOT NULL, payload TEXT NOT NULL, expires_at REAL NOT NULL, "
    "consumed_at REAL, PRIMARY KEY (workspace_id, approval_id))",
)


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """Add ``workspace_id`` to the version 1 tables; their rows belong to ``"default"``.

    Version 1 had no workspaces (``workspace_id`` arrived with S5, default ``"default"``).
    Runs inside the caller's ``BEGIN IMMEDIATE`` transaction, so it happens once.
    """
    tables = {
        "nexura_run_checkpoints": ("run_id", "data", "updated_at"),
        "nexura_run_claims": ("run_id", "owner", "expires_at", "lease_seconds"),
        "nexura_sdk_approvals": ("approval_id", "payload", "expires_at", "consumed_at"),
    }
    present = {
        str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    for (table, columns), create in zip(tables.items(), _SQLITE_SCHEMA, strict=True):
        if table not in present:
            continue
        have = {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}
        conn.execute(f"ALTER TABLE {table} RENAME TO {table}_v1")
        conn.execute(create)
        select = ", ".join(c if c in have else "NULL" for c in columns)
        conn.execute(
            f"INSERT INTO {table} (workspace_id, {', '.join(columns)}) "  # noqa: S608
            f"SELECT ?, {select} FROM {table}_v1",
            (DEFAULT_WORKSPACE,),
        )
        conn.execute(f"DROP TABLE {table}_v1")
    conn.execute(
        "UPDATE nexura_meta SET v = ? WHERE k = 'checkpoint_schema'", (str(_SCHEMA_VERSION),)
    )


class SQLiteCheckpointStore:
    """Durable checkpoint store on stdlib ``sqlite3``.

    The database file is created with mode 0600 (owner only) because checkpoints hold the
    conversation transcript. Writes are atomic upserts and I/O runs in a worker thread.
    Saves are fenced by the recovery lease in the same transaction, across processes.
    Checkpoints, claims and approvals are scoped by ``workspace_id`` (schema version 2; a
    version 1 file is migrated on open, its rows going to ``"default"``), so a store opened
    with another workspace neither lists nor recovers them. The execution journal
    (``SQLiteExecutionStore``) lives in the same file; it is opened on first use and
    switches the file to WAL mode. Single host only, like all SQLite leases.
    """

    def __init__(self, db_path: str | Path, *, workspace_id: str = DEFAULT_WORKSPACE) -> None:
        if not isinstance(workspace_id, str) or not workspace_id:
            raise ValueError("workspace_id must be a nonempty string")
        self.workspace_id = workspace_id
        self.db_path = Path(db_path).resolve()
        self._lock = threading.Lock()
        self._journal_lock = threading.Lock()
        self._journal: Any = None
        self._journal_conn: sqlite3.Connection | None = None
        self._journal_ready = False
        self._closed = False
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # Create the file ourselves so it is never world-readable, whatever the umask.
        fd = os.open(self.db_path, os.O_RDWR | os.O_CREAT, 0o600)
        os.close(fd)
        os.chmod(self.db_path, 0o600)
        self._conn: sqlite3.Connection | None = sqlite3.connect(
            str(self.db_path), check_same_thread=False, timeout=10.0
        )
        with self._lock, self._conn:
            self._conn.execute("PRAGMA busy_timeout = 5000;")
            self._conn.execute("PRAGMA synchronous = FULL;")
            # One writer migrates; concurrent openers wait here and then see version 2.
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS nexura_meta (k TEXT PRIMARY KEY, v TEXT)"
            )
            row = self._conn.execute(
                "SELECT v FROM nexura_meta WHERE k = 'checkpoint_schema'"
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO nexura_meta (k, v) VALUES ('checkpoint_schema', ?)",
                    (str(_SCHEMA_VERSION),),
                )
            elif row[0] == "1":
                _migrate_v1_to_v2(self._conn)
            elif row[0] != str(_SCHEMA_VERSION):
                raise StorageError(
                    f"Unsupported checkpoint database schema {row[0]!r} "
                    f"(expected {_SCHEMA_VERSION})."
                )
            for statement in _SQLITE_SCHEMA:
                self._conn.execute(statement)

    def _open(self) -> sqlite3.Connection:
        if self._closed or self._conn is None:
            raise StorageError("SQLiteCheckpointStore connection is closed.")
        return self._conn

    async def open_execution_store(self) -> ExecutionStore:
        """The execution journal in the same database file (created on first use)."""
        from nexura_agents.execution_store import SQLiteExecutionStore

        with self._journal_lock:
            self._open()
            if self._journal is None:
                conn = sqlite3.connect(
                    str(self.db_path), timeout=10.0, isolation_level=None, check_same_thread=False
                )
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=FULL")
                self._journal_conn = conn
                self._journal = SQLiteExecutionStore.from_connection(conn)
            journal = self._journal
        if not self._journal_ready:
            await journal.init_db()
            self._journal_ready = True
        result: ExecutionStore = journal
        return result

    def _save_sync(self, run_id: str, data: str, owner: str | None) -> None:
        with self._lock, self._open() as conn:
            # Lock the database before reading the lease so check and write are one step.
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT owner, expires_at, lease_seconds FROM nexura_run_claims "
                "WHERE workspace_id = ? AND run_id = ?",
                (self.workspace_id, run_id),
            ).fetchone()
            check_fence(run_id, owner, None if row is None else (str(row[0]), float(row[1])))
            now = time.time()
            conn.execute(
                "INSERT INTO nexura_run_checkpoints (workspace_id, run_id, data, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(workspace_id, run_id) DO UPDATE SET "
                "data = excluded.data, updated_at = excluded.updated_at",
                (self.workspace_id, run_id, data, now),
            )
            if row is not None and owner is not None:
                lease = DEFAULT_CLAIM_LEASE_SECONDS if row[2] is None else float(row[2])
                conn.execute(
                    "UPDATE nexura_run_claims SET expires_at = ? "
                    "WHERE workspace_id = ? AND run_id = ? AND owner = ?",
                    (now + lease, self.workspace_id, run_id, owner),
                )

    def _approval_sync(
        self, operation: str, approval_id: str, payload: str = "", expires_at: float = 0.0
    ) -> str | bool | None:
        with self._lock, self._open() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if operation == "consume":
                cursor = conn.execute(
                    "UPDATE nexura_sdk_approvals SET consumed_at=? WHERE workspace_id=? "
                    "AND approval_id=? AND payload=? AND consumed_at IS NULL AND expires_at>?",
                    (time.time(), self.workspace_id, approval_id, payload, time.time()),
                )
                return cursor.rowcount == 1
            if operation == "save":
                conn.execute(
                    "INSERT OR IGNORE INTO nexura_sdk_approvals "
                    "(workspace_id, approval_id, payload, expires_at) VALUES (?, ?, ?, ?)",
                    (self.workspace_id, approval_id, payload, expires_at),
                )
            row = conn.execute(
                "SELECT payload, expires_at FROM nexura_sdk_approvals "
                "WHERE workspace_id=? AND approval_id=?",
                (self.workspace_id, approval_id),
            ).fetchone()
            if operation == "save" and row is not None and row != (payload, expires_at):
                raise ApprovalDecisionError("Stored interruption differs from approval envelope.")
            return str(row[0]) if row is not None else None

    async def save_approval(self, approval_id: str, payload: str, expires_at: float) -> None:
        """Persist an immutable approval independently of run checkpoint cleanup."""
        await asyncio.to_thread(self._approval_sync, "save", approval_id, payload, expires_at)

    async def load_approval(self, approval_id: str) -> str | None:
        """Load the original approval payload, including after consumption."""
        result = await asyncio.to_thread(self._approval_sync, "load", approval_id)
        return result if isinstance(result, str) else None

    async def consume_approval(self, approval_id: str, payload: str) -> bool:
        """Consume a matching unexpired approval once, atomically across processes."""
        return await asyncio.to_thread(self._approval_sync, "consume", approval_id, payload) is True

    def _load_sync(self, run_id: str) -> str | None:
        with self._lock:
            row = (
                self._open()
                .execute(
                    "SELECT data FROM nexura_run_checkpoints WHERE workspace_id = ? AND run_id = ?",
                    (self.workspace_id, run_id),
                )
                .fetchone()
            )
        return None if row is None else str(row[0])

    def _delete_sync(self, run_id: str, owner: str | None, fenced: bool) -> None:
        with self._lock, self._open() as conn:
            if fenced:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT owner, expires_at FROM nexura_run_claims "
                    "WHERE workspace_id = ? AND run_id = ?",
                    (self.workspace_id, run_id),
                ).fetchone()
                check_fence(run_id, owner, None if row is None else (str(row[0]), float(row[1])))
            for table in ("nexura_run_checkpoints", "nexura_run_claims"):
                conn.execute(
                    f"DELETE FROM {table} WHERE workspace_id = ? AND run_id = ?",  # noqa: S608
                    (self.workspace_id, run_id),
                )

    def _claim_sync(self, run_id: str, owner: str, lease: float) -> bool:
        now = time.time()
        with self._lock, self._open() as conn:
            cur = conn.execute(
                "INSERT INTO nexura_run_claims "
                "(workspace_id, run_id, owner, expires_at, lease_seconds) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(workspace_id, run_id) DO UPDATE SET owner = excluded.owner, "
                "expires_at = excluded.expires_at, lease_seconds = excluded.lease_seconds "
                "WHERE nexura_run_claims.owner = excluded.owner "
                "OR nexura_run_claims.expires_at <= ?",
                (self.workspace_id, run_id, owner, now + lease, lease, now),
            )
            return cur.rowcount == 1

    def _release_sync(self, run_id: str, owner: str) -> None:
        with self._lock, self._open() as conn:
            conn.execute(
                "DELETE FROM nexura_run_claims WHERE workspace_id = ? AND run_id = ? AND owner = ?",
                (self.workspace_id, run_id, owner),
            )

    def _list_sync(self, limit: int) -> list[str]:
        with self._lock:
            rows = (
                self._open()
                .execute(
                    "SELECT run_id FROM nexura_run_checkpoints WHERE workspace_id = ? "
                    "ORDER BY updated_at, run_id LIMIT ?",
                    (self.workspace_id, max(int(limit), 0)),
                )
                .fetchall()
            )
        return [str(r[0]) for r in rows]

    async def claim(
        self, run_id: str, owner: str, lease_seconds: float = DEFAULT_CLAIM_LEASE_SECONDS
    ) -> bool:
        """Take the exclusive recovery lease of ``run_id`` (atomic across processes)."""
        return await asyncio.to_thread(self._claim_sync, run_id, owner, lease_seconds)

    async def release(self, run_id: str, owner: str) -> None:
        """Give the lease back (no-op when ``owner`` does not hold it)."""
        await asyncio.to_thread(self._release_sync, run_id, owner)

    async def save(self, run_id: str, checkpoint_json: str, owner: str | None = None) -> None:
        """Atomically store the checkpoint of ``run_id``; fenced by the recovery lease."""
        await asyncio.to_thread(self._save_sync, run_id, checkpoint_json, owner)

    async def load(self, run_id: str) -> str | None:
        """Return the checkpoint of ``run_id`` or None."""
        return await asyncio.to_thread(self._load_sync, run_id)

    async def delete(self, run_id: str, owner: str | None = None, *, fenced: bool = False) -> None:
        """Remove the checkpoint of ``run_id`` if present (``fenced``: only if not stale)."""
        await asyncio.to_thread(self._delete_sync, run_id, owner, fenced)

    async def list_incomplete(self, limit: int = 100) -> list[str]:
        """Run ids with a checkpoint, oldest update first."""
        return await asyncio.to_thread(self._list_sync, limit)

    def close(self) -> None:
        """Close the database connections (checkpoints and execution journal)."""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
            self._conn = None
            self._closed = True
        with self._journal_lock:
            if self._journal_conn is not None:
                self._journal_conn.close()
            self._journal_conn = None
            self._journal = None


def accepts_fencing(method: Any, name: str = "owner") -> bool:
    """True when a store method takes the given fencing argument (``owner`` or ``fenced``)."""
    try:
        return name in inspect.signature(method).parameters
    except (TypeError, ValueError):
        return False


def encode_checkpoint(
    phase: str,
    interruption_json: str,
    *,
    final_text: str | None = None,
    output_attempts: int = 0,
    agent_started_at: int = 0,
) -> str:
    """Wrap a ``RunInterruption`` JSON payload into a checkpoint envelope.

    ``final_text`` (``completed`` phase) is the output the caller got, which is not always
    the last message: a tool result can be the output, and guardrails can rewrite it.
    """
    envelope: dict[str, Any] = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "phase": phase,
        "interruption": interruption_json,
    }
    if final_text is not None:
        envelope["final_text"] = final_text
    if output_attempts:
        # Output retries already spent, so recovery cannot grant a fresh retry budget.
        envelope["output_attempts"] = output_attempts
    if agent_started_at:
        # Where the active agent took over after a handoff (scopes tool_choice resets).
        envelope["agent_started_at"] = agent_started_at
    return json.dumps(envelope, ensure_ascii=False)


def checkpoint_int(raw: str, key: str) -> int:
    """A non-negative counter stored in a checkpoint envelope (0 when absent or invalid)."""
    try:
        value = json.loads(raw).get(key, 0)
    except (ValueError, AttributeError):
        return 0
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def checkpoint_output_attempts(raw: str) -> int:
    """Output retries already used, stored in a checkpoint (0 when absent or invalid)."""
    return checkpoint_int(raw, "output_attempts")


def checkpoint_final_text(raw: str) -> str | None:
    """The final output text stored in a ``completed`` checkpoint, if any."""
    try:
        value = json.loads(raw).get("final_text")
    except (ValueError, AttributeError):
        return None
    return value if isinstance(value, str) else None


def decode_checkpoint(raw: str, run_id: str) -> tuple[str, Any]:
    """Parse an envelope into ``(phase, RunInterruption)``; raises ``RunRecoveryError``."""
    from nexura_agents.types import RunInterruption

    try:
        envelope = json.loads(raw)
        if not isinstance(envelope, dict) or envelope.get("schema_version") != 1:
            raise ValueError(f"unsupported checkpoint schema {envelope.get('schema_version')!r}")
        phase = envelope["phase"]
        if phase not in PHASES:
            raise ValueError(f"unknown phase {phase!r}")
        interruption = RunInterruption.from_json(envelope["interruption"])
    except Exception as exc:
        raise RunRecoveryError(
            f"Checkpoint of run '{run_id}' is corrupt or unsupported: {exc}", original_error=exc
        ) from exc
    if interruption.run_id != run_id:
        raise RunRecoveryError(
            f"Checkpoint stored under '{run_id}' belongs to run '{interruption.run_id}'."
        )
    return phase, interruption
