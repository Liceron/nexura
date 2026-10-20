# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Durable agent runs: checkpoint stores.

A ``Runner(checkpoint_store=...)`` writes a checkpoint of the run after every step that
matters (see ``docs/durable-runs.md``). A checkpoint is a JSON envelope around a
``RunInterruption`` payload, so the format is the one approvals already use. After a crash,
``Runner.recover_async(agent, run_id)`` continues from the last checkpoint.

Checkpoints contain the full conversation transcript. Protect them like session data.
"""

from __future__ import annotations

import inspect
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from nexura_agents._lazy import LazyModule
from nexura_agents.errors import CheckpointLeaseLostError, RunRecoveryError, StorageError

if TYPE_CHECKING:
    import asyncio
else:
    asyncio = LazyModule("asyncio")

CHECKPOINT_SCHEMA_VERSION = 1
PHASES = ("model_next", "tools_pending", "paused", "completed")
DEFAULT_CLAIM_LEASE_SECONDS = 3600.0


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


def check_fence(run_id: str, owner: str | None, held: tuple[str, float] | None) -> None:
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
    if owner is not None or expires_at > time.time():
        raise CheckpointLeaseLostError(
            f"Run '{run_id}' is being recovered by another caller; checkpoint not written."
        )


class MemoryCheckpointStore:
    """In-process store for tests and demos; checkpoints do not survive the process."""

    def __init__(self) -> None:
        self._data: dict[str, tuple[float, str]] = {}
        self._claims: dict[str, tuple[str, float, float]] = {}  # owner, expires_at, lease

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


_SCHEMA_VERSION = 1


class SQLiteCheckpointStore:
    """Durable checkpoint store on stdlib ``sqlite3``.

    The database file is created with mode 0600 (owner only) because checkpoints hold the
    conversation transcript. Writes are atomic upserts and I/O runs in a worker thread.
    Saves are fenced by the recovery lease in the same transaction, across processes.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).resolve()
        self._lock = threading.Lock()
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
            elif row[0] != str(_SCHEMA_VERSION):
                raise StorageError(
                    f"Unsupported checkpoint database schema {row[0]!r} "
                    f"(expected {_SCHEMA_VERSION})."
                )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS nexura_run_checkpoints ("
                "run_id TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at REAL NOT NULL)"
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS nexura_run_claims ("
                "run_id TEXT PRIMARY KEY, owner TEXT NOT NULL, expires_at REAL NOT NULL, "
                "lease_seconds REAL)"
            )
            columns = {r[1] for r in self._conn.execute("PRAGMA table_info(nexura_run_claims)")}
            if "lease_seconds" not in columns:
                try:
                    self._conn.execute(
                        "ALTER TABLE nexura_run_claims ADD COLUMN lease_seconds REAL"
                    )
                except sqlite3.OperationalError as exc:
                    # Another process migrated the table between our check and the ALTER.
                    if "duplicate column" not in str(exc).lower():
                        raise

    def _open(self) -> sqlite3.Connection:
        if self._closed or self._conn is None:
            raise StorageError("SQLiteCheckpointStore connection is closed.")
        return self._conn

    def _save_sync(self, run_id: str, data: str, owner: str | None) -> None:
        with self._lock, self._open() as conn:
            # Lock the database before reading the lease so check and write are one step.
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT owner, expires_at, lease_seconds FROM nexura_run_claims WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            check_fence(run_id, owner, None if row is None else (str(row[0]), float(row[1])))
            now = time.time()
            conn.execute(
                "INSERT INTO nexura_run_checkpoints (run_id, data, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET data = excluded.data, "
                "updated_at = excluded.updated_at",
                (run_id, data, now),
            )
            if row is not None and owner is not None:
                lease = DEFAULT_CLAIM_LEASE_SECONDS if row[2] is None else float(row[2])
                conn.execute(
                    "UPDATE nexura_run_claims SET expires_at = ? WHERE run_id = ? AND owner = ?",
                    (now + lease, run_id, owner),
                )

    def _load_sync(self, run_id: str) -> str | None:
        with self._lock:
            row = (
                self._open()
                .execute("SELECT data FROM nexura_run_checkpoints WHERE run_id = ?", (run_id,))
                .fetchone()
            )
        return None if row is None else str(row[0])

    def _delete_sync(self, run_id: str, owner: str | None, fenced: bool) -> None:
        with self._lock, self._open() as conn:
            if fenced:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT owner, expires_at FROM nexura_run_claims WHERE run_id = ?", (run_id,)
                ).fetchone()
                check_fence(run_id, owner, None if row is None else (str(row[0]), float(row[1])))
            conn.execute("DELETE FROM nexura_run_checkpoints WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM nexura_run_claims WHERE run_id = ?", (run_id,))

    def _claim_sync(self, run_id: str, owner: str, lease: float) -> bool:
        now = time.time()
        with self._lock, self._open() as conn:
            cur = conn.execute(
                "INSERT INTO nexura_run_claims (run_id, owner, expires_at, lease_seconds) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET owner = excluded.owner, "
                "expires_at = excluded.expires_at, lease_seconds = excluded.lease_seconds "
                "WHERE nexura_run_claims.owner = excluded.owner "
                "OR nexura_run_claims.expires_at <= ?",
                (run_id, owner, now + lease, lease, now),
            )
            return cur.rowcount == 1

    def _release_sync(self, run_id: str, owner: str) -> None:
        with self._lock, self._open() as conn:
            conn.execute(
                "DELETE FROM nexura_run_claims WHERE run_id = ? AND owner = ?", (run_id, owner)
            )

    def _list_sync(self, limit: int) -> list[str]:
        with self._lock:
            rows = (
                self._open()
                .execute(
                    "SELECT run_id FROM nexura_run_checkpoints ORDER BY updated_at, run_id LIMIT ?",
                    (max(int(limit), 0),),
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
        """Close the database connection."""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
            self._conn = None
            self._closed = True


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
