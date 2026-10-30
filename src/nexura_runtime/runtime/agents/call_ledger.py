# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""What an agent-coordinator run already did, so a re-run never does it twice.

The coordinator continues a run after a human approval by running it again from the start
with the same ``ExecutionContext``. The ledger records, per run *visit* (workspace, run,
node and the workflow step key or attempt id, the "scope"):

- each tool call by its **position** (agent and its definition digest, handoff count, turn,
  index in the turn), with the tool name, the arguments' canonical digest and its outcome:
  ``started`` right before the tool runs, ``done`` with the result afterwards;
- each model response by position (the turn), for ``replay_model_responses=True``;
- a completion marker per scope, with the approvals the run used.

A re-run replays a ``done`` call's result; a ``started`` call without a result is sent
again only for ``pure``/``idempotent`` tools; a position whose recorded call differs from
the one the model asks for now fails closed (P39 G2 review R2-M2). A completed scope's
rows are deleted, only its marker stays; ``purge`` removes rows older than a TTL.

Two stores: :class:`CoordinatorCallLedger` in an ``ApprovalPolicyManager``'s SQLite
database (survives restarts, shared by processes), and :class:`MemoryCallLedger`, used for
a gate without such a database (one coordinator object only).
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass, replace
from typing import Any

_TABLE = "coordinator_call_ledger"
_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    workspace_id TEXT NOT NULL,
    entry_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    kind TEXT NOT NULL,
    run_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    args_digest TEXT NOT NULL,
    attempt TEXT NOT NULL,
    status TEXT NOT NULL,
    result TEXT,
    updated_at REAL NOT NULL,
    PRIMARY KEY (workspace_id, entry_id)
)
"""
_COLUMNS = "status, result, tool_name, args_digest, attempt, kind, scope, updated_at"


@dataclass(frozen=True)
class CallRecord:
    """One ledger entry: a tool call (``started``/``done``), a model response or a run
    completion marker (``done``)."""

    status: str
    result: str | None
    tool_name: str = ""
    args_digest: str = ""
    attempt: str = ""
    kind: str = "call"
    scope: str = ""
    updated_at: float = 0.0


@dataclass(frozen=True)
class Entry:
    """Where an entry belongs: its scope, kind, run and (for calls) tool and arguments."""

    scope: str
    kind: str
    run_id: str
    attempt: str
    tool_name: str = ""
    args_digest: str = ""


class _LedgerBase:
    """The operations the coordinator uses (see the module docstring)."""

    def get(self, workspace_id: str, entry_id: str) -> CallRecord | None:
        raise NotImplementedError

    def start(self, workspace_id: str, entry_id: str, entry: Entry, *, again: bool) -> bool:
        """Mark a call dispatched; False when another attempt recorded it first."""
        raise NotImplementedError

    def finish(self, workspace_id: str, entry_id: str, entry: Entry, result: str) -> None:
        """Record a finished call's result (or a completion marker)."""
        raise NotImplementedError

    def record_once(self, workspace_id: str, entry_id: str, entry: Entry, value: str) -> str:
        """Store ``value`` unless the entry exists; return what is stored (first wins)."""
        raise NotImplementedError

    def same_call_elsewhere(
        self, workspace_id: str, scope: str, tool_name: str, args_digest: str, attempt: str
    ) -> bool:
        """True when another attempt of this scope recorded the same tool and arguments."""
        raise NotImplementedError

    def clear_scope(self, workspace_id: str, scope: str) -> None:
        """Delete a scope's call and model entries (its completion marker stays)."""
        raise NotImplementedError

    def purge(self, older_than_seconds: float) -> int:
        """Delete entries (markers included) not updated for ``older_than_seconds``."""
        raise NotImplementedError


class CoordinatorCallLedger(_LedgerBase):
    """The ledger in an ``ApprovalPolicyManager``'s SQLite database (rows written with
    autocommit; a primary-key insert fences two attempts of the same call)."""

    def __init__(self, manager: Any) -> None:
        self.manager = manager
        self._ready = False

    @classmethod
    def for_gate(cls, gate: Any) -> CoordinatorCallLedger | None:
        """The ledger of an approval gate whose manager has a SQLite database, else None."""
        manager = getattr(gate, "manager", None)
        if manager is None or not callable(getattr(manager, "_connection", None)):
            return None
        return cls(manager)

    def _conn(self) -> Any:
        return self.manager._connection()

    def _ensure(self, conn: sqlite3.Connection) -> None:
        if not self._ready:
            conn.execute(_SCHEMA)
            self._ready = True

    def get(self, workspace_id: str, entry_id: str) -> CallRecord | None:
        with self._conn() as conn:
            self._ensure(conn)
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM {_TABLE} WHERE workspace_id = ? AND entry_id = ?",
                (workspace_id, entry_id),
            ).fetchone()
        return None if row is None else CallRecord(*tuple(row))

    def _insert(
        self, conn: sqlite3.Connection, ws: str, entry_id: str, e: Entry, status: str, res: Any
    ) -> None:
        conn.execute(
            f"INSERT INTO {_TABLE} (workspace_id, entry_id, scope, kind, run_id, tool_name, "
            "args_digest, attempt, status, result, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ws,
                entry_id,
                e.scope,
                e.kind,
                e.run_id,
                e.tool_name,
                e.args_digest,
                e.attempt,
                status,
                res,
                time.time(),
            ),
        )

    def start(self, workspace_id: str, entry_id: str, entry: Entry, *, again: bool) -> bool:
        with self._conn() as conn:
            self._ensure(conn)
            if again:
                cur = conn.execute(
                    f"UPDATE {_TABLE} SET updated_at = ?, attempt = ? "
                    "WHERE workspace_id = ? AND entry_id = ? AND status = 'started'",
                    (time.time(), entry.attempt, workspace_id, entry_id),
                )
                return bool(cur.rowcount)
            try:
                self._insert(conn, workspace_id, entry_id, entry, "started", None)
            except sqlite3.IntegrityError:
                return False
        return True

    def finish(self, workspace_id: str, entry_id: str, entry: Entry, result: str) -> None:
        with self._conn() as conn:
            self._ensure(conn)
            conn.execute(
                f"INSERT INTO {_TABLE} (workspace_id, entry_id, scope, kind, run_id, tool_name, "
                "args_digest, attempt, status, result, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'done', ?, ?) "
                "ON CONFLICT (workspace_id, entry_id) DO UPDATE SET status = 'done', "
                "result = excluded.result, updated_at = excluded.updated_at",
                (
                    workspace_id,
                    entry_id,
                    entry.scope,
                    entry.kind,
                    entry.run_id,
                    entry.tool_name,
                    entry.args_digest,
                    entry.attempt,
                    result,
                    time.time(),
                ),
            )

    def record_once(self, workspace_id: str, entry_id: str, entry: Entry, value: str) -> str:
        with self._conn() as conn:
            self._ensure(conn)
            try:
                self._insert(conn, workspace_id, entry_id, entry, "done", value)
                return value
            except sqlite3.IntegrityError:
                row = conn.execute(
                    f"SELECT result FROM {_TABLE} WHERE workspace_id = ? AND entry_id = ?",
                    (workspace_id, entry_id),
                ).fetchone()
        return str(row[0]) if row is not None and row[0] is not None else value

    def same_call_elsewhere(
        self, workspace_id: str, scope: str, tool_name: str, args_digest: str, attempt: str
    ) -> bool:
        with self._conn() as conn:
            self._ensure(conn)
            row = conn.execute(
                f"SELECT 1 FROM {_TABLE} WHERE workspace_id = ? AND scope = ? AND kind = 'call' "
                "AND tool_name = ? AND args_digest = ? AND attempt != ? LIMIT 1",
                (workspace_id, scope, tool_name, args_digest, attempt),
            ).fetchone()
        return row is not None

    def clear_scope(self, workspace_id: str, scope: str) -> None:
        with self._conn() as conn:
            self._ensure(conn)
            conn.execute(
                f"DELETE FROM {_TABLE} WHERE workspace_id = ? AND scope = ? "
                "AND kind IN ('call', 'model')",
                (workspace_id, scope),
            )

    def purge(self, older_than_seconds: float) -> int:
        cutoff = time.time() - float(older_than_seconds)
        with self._conn() as conn:
            self._ensure(conn)
            cur = conn.execute(f"DELETE FROM {_TABLE} WHERE updated_at < ?", (cutoff,))
            return int(cur.rowcount or 0)


class MemoryCallLedger(_LedgerBase):
    """The same ledger in memory: for a coordinator whose approval gate has no database.

    It covers re-runs through the same ``AgentCoordinator`` object only; a re-run in
    another process or through another coordinator object cannot see it.
    """

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], CallRecord] = {}
        self._lock = threading.Lock()

    def get(self, workspace_id: str, entry_id: str) -> CallRecord | None:
        with self._lock:
            return self._rows.get((workspace_id, entry_id))

    @staticmethod
    def _record(entry: Entry, status: str, result: str | None) -> CallRecord:
        return CallRecord(
            status=status,
            result=result,
            tool_name=entry.tool_name,
            args_digest=entry.args_digest,
            attempt=entry.attempt,
            kind=entry.kind,
            scope=entry.scope,
            updated_at=time.time(),
        )

    def start(self, workspace_id: str, entry_id: str, entry: Entry, *, again: bool) -> bool:
        key = (workspace_id, entry_id)
        with self._lock:
            held = self._rows.get(key)
            if again:
                if held is None or held.status != "started":
                    return False
                self._rows[key] = replace(held, attempt=entry.attempt, updated_at=time.time())
                return True
            if held is not None:
                return False
            self._rows[key] = self._record(entry, "started", None)
        return True

    def finish(self, workspace_id: str, entry_id: str, entry: Entry, result: str) -> None:
        with self._lock:
            self._rows[(workspace_id, entry_id)] = self._record(entry, "done", result)

    def record_once(self, workspace_id: str, entry_id: str, entry: Entry, value: str) -> str:
        key = (workspace_id, entry_id)
        with self._lock:
            held = self._rows.get(key)
            if held is not None and held.result is not None:
                return held.result
            self._rows[key] = self._record(entry, "done", value)
        return value

    def same_call_elsewhere(
        self, workspace_id: str, scope: str, tool_name: str, args_digest: str, attempt: str
    ) -> bool:
        with self._lock:
            return any(
                ws == workspace_id
                and r.scope == scope
                and r.kind == "call"
                and r.tool_name == tool_name
                and r.args_digest == args_digest
                and r.attempt != attempt
                for (ws, _), r in self._rows.items()
            )

    def clear_scope(self, workspace_id: str, scope: str) -> None:
        with self._lock:
            for key in [
                k
                for k, r in self._rows.items()
                if k[0] == workspace_id and r.scope == scope and r.kind in ("call", "model")
            ]:
                del self._rows[key]

    def purge(self, older_than_seconds: float) -> int:
        cutoff = time.time() - float(older_than_seconds)
        with self._lock:
            old = [k for k, r in self._rows.items() if r.updated_at < cutoff]
            for key in old:
                del self._rows[key]
        return len(old)


def purge_coordinator_ledger(manager: Any, older_than_seconds: float) -> int:
    """Delete coordinator ledger entries older than ``older_than_seconds`` from an
    ``ApprovalPolicyManager``'s database (run it periodically, for example daily with a TTL
    longer than your approval expiry). Returns how many rows were deleted."""
    return CoordinatorCallLedger(manager).purge(older_than_seconds)
