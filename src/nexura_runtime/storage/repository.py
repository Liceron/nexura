# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Unified storage repository with transactional CAS and profile support.

SQLite is built in. PostgreSQL (``PostgresRepository``) is the ``nexura-postgres`` package;
``get_storage_repository`` returns it for a ``postgresql://`` URI when it is installed.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from nexura_agents.contracts.canonical import canonical_json
from nexura_agents.errors import SessionConflictError, StorageError
from nexura_agents.session import SessionSnapshot, deserialize_session, serialize_session
from nexura_agents.types import Message

from nexura_runtime.storage.migrations import run_sqlite_migrations
from nexura_runtime.storage.models import RunEventRecord, RunRecord


@runtime_checkable
class StorageRepository(Protocol):
    """Protocol for unified workspace-partitioned storage."""

    async def get_session(self, workspace_id: str, session_id: str) -> SessionSnapshot | None: ...

    async def save_session(
        self,
        workspace_id: str,
        session_id: str,
        messages: list[Message],
        expected_revision: int,
    ) -> int: ...

    async def create_run(
        self,
        workspace_id: str,
        run_id: str,
        initial_state: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RunRecord: ...

    async def get_run(self, workspace_id: str, run_id: str) -> RunRecord | None: ...

    async def update_run(
        self,
        workspace_id: str,
        run_id: str,
        expected_revision: int,
        status: str,
        state_update: dict[str, Any] | None = None,
        events: list[dict[str, Any]] | None = None,
    ) -> RunRecord: ...

    async def get_run_events(self, workspace_id: str, run_id: str) -> list[RunEventRecord]: ...

    async def close(self) -> None: ...


class SQLiteRepository:
    """Thread-safe, transactionally consistent SQLite repository."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self._lock = threading.Lock()
        self._async_lock = asyncio.Lock()
        # Keep a private named in-memory database alive while per-operation
        # connections are opened and closed, just as for file-backed repositories.
        self._memory_uri = (
            f"file:nexura-{uuid.uuid4().hex}?mode=memory&cache=shared"
            if self.db_path == ":memory:"
            else None
        )
        self._memory_keeper = self._get_connection() if self._memory_uri else None

        # Run migrations on startup
        conn = self._get_connection()
        try:
            with conn:
                run_sqlite_migrations(conn)
        finally:
            conn.close()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self._memory_uri or self.db_path,
            uri=self._memory_uri is not None,
            timeout=30.0,
            autocommit=True,
        )
        conn.row_factory = sqlite3.Row
        return conn

    async def get_session(self, workspace_id: str, session_id: str) -> SessionSnapshot | None:
        async with self._async_lock:
            conn = self._get_connection()
            try:
                cursor = conn.execute(
                    "SELECT data, revision FROM unified_sessions "
                    "WHERE workspace_id = ? AND session_id = ?",
                    (workspace_id, session_id),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                data_str, revision = row[0], row[1]
                sess_id, _, msgs = deserialize_session(data_str)
                return SessionSnapshot(session_id=sess_id, messages=msgs, revision=revision)
            finally:
                conn.close()

    async def save_session(
        self,
        workspace_id: str,
        session_id: str,
        messages: list[Message],
        expected_revision: int,
    ) -> int:
        async with self._async_lock:
            conn = self._get_connection()
            try:
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.execute(
                    "SELECT revision FROM unified_sessions "
                    "WHERE workspace_id = ? AND session_id = ?",
                    (workspace_id, session_id),
                )
                row = cursor.fetchone()

                if row is None:
                    if expected_revision != 0:
                        raise SessionConflictError(session_id, expected_revision, 0)
                    new_revision = 1
                    data_json = serialize_session(session_id, new_revision, messages)
                    conn.execute(
                        "INSERT INTO unified_sessions "
                        "(workspace_id, session_id, revision, data, updated_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (workspace_id, session_id, new_revision, data_json, time.time()),
                    )
                else:
                    actual_rev = row["revision"]
                    if actual_rev != expected_revision:
                        raise SessionConflictError(session_id, expected_revision, actual_rev)
                    new_revision = expected_revision + 1
                    data_json = serialize_session(session_id, new_revision, messages)
                    cursor = conn.execute(
                        "UPDATE unified_sessions SET revision = ?, data = ?, updated_at = ? "
                        "WHERE workspace_id = ? AND session_id = ? AND revision = ?",
                        (
                            new_revision,
                            data_json,
                            time.time(),
                            workspace_id,
                            session_id,
                            expected_revision,
                        ),
                    )
                    if cursor.rowcount == 0:
                        raise SessionConflictError(session_id, expected_revision, actual_rev)

                conn.execute("COMMIT")
                return new_revision
            except BaseException:
                with contextlib.suppress(Exception):
                    conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    async def create_run(
        self,
        workspace_id: str,
        run_id: str,
        initial_state: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RunRecord:
        async with self._async_lock:
            conn = self._get_connection()
            try:
                conn.execute("BEGIN IMMEDIATE")
                now = time.time()
                rec = RunRecord(
                    workspace_id=workspace_id,
                    run_id=run_id,
                    status="running",
                    revision=1,
                    input_data=copy.deepcopy(initial_state or {}),
                    state=copy.deepcopy(initial_state or {}),
                    metadata=copy.deepcopy(metadata or {}),
                    created_at=now,
                    updated_at=now,
                )
                conn.execute(
                    "INSERT INTO runs (workspace_id, run_id, status, revision, input_json, "
                    "state_json, metadata_json, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        workspace_id,
                        run_id,
                        rec.status,
                        rec.revision,
                        canonical_json(rec.input_data),
                        canonical_json(rec.state),
                        canonical_json(rec.metadata),
                        rec.created_at,
                        rec.updated_at,
                    ),
                )
                conn.execute("COMMIT")
                return rec
            except BaseException:
                with contextlib.suppress(Exception):
                    conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    async def _journal(self, workspace_id: str, run_id: str) -> Any:
        """The execution-journal bridge when this run is a journaled durable graph."""
        from nexura_runtime.storage.execution_bridge import journal_bridge

        async def probe() -> bool:
            conn = self._get_connection()
            try:
                row = conn.execute(
                    "SELECT 1 FROM nexura_exec_runs WHERE workspace_id = ? AND run_id = ?",
                    (workspace_id, run_id),
                ).fetchone()
            except sqlite3.OperationalError as exc:
                if "no such table" in str(exc):
                    return False  # the journal was never created in this database
                raise
            finally:
                conn.close()
            return row is not None

        return await journal_bridge(self, workspace_id, run_id, probe)

    async def get_run(self, workspace_id: str, run_id: str) -> RunRecord | None:
        # Runs of the removed DurableExecutionEngine live in the execution journal;
        # this is their read-only view (an update of one is refused).
        journal = await self._journal(workspace_id, run_id)
        if journal is not None:
            return await journal.get_run(workspace_id, run_id)  # type: ignore[no-any-return]
        async with self._async_lock:
            conn = self._get_connection()
            try:
                cursor = conn.execute(
                    "SELECT status, revision, input_json, state_json, metadata_json, "
                    "created_at, updated_at FROM runs WHERE workspace_id = ? AND run_id = ?",
                    (workspace_id, run_id),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                return RunRecord(
                    workspace_id=workspace_id,
                    run_id=run_id,
                    status=row["status"],
                    revision=row["revision"],
                    input_data=json.loads(row["input_json"]),
                    state=json.loads(row["state_json"]),
                    metadata=json.loads(row["metadata_json"]),
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
            finally:
                conn.close()

    async def update_run(
        self,
        workspace_id: str,
        run_id: str,
        expected_revision: int,
        status: str,
        state_update: dict[str, Any] | None = None,
        events: list[dict[str, Any]] | None = None,
    ) -> RunRecord:
        journal = await self._journal(workspace_id, run_id)
        if journal is not None:
            return await journal.update_run(  # type: ignore[no-any-return]
                workspace_id, run_id, expected_revision, status, state_update, events
            )
        async with self._async_lock:
            conn = self._get_connection()
            try:
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.execute(
                    "SELECT status, revision, input_json, state_json, metadata_json, created_at "
                    "FROM runs WHERE workspace_id = ? AND run_id = ?",
                    (workspace_id, run_id),
                )
                row = cursor.fetchone()
                if row is None:
                    raise StorageError(f"Run '{run_id}' not found in workspace '{workspace_id}'.")

                actual_rev = row["revision"]
                if actual_rev != expected_revision:
                    raise SessionConflictError(run_id, expected_revision, actual_rev)

                new_rev = expected_revision + 1
                now = time.time()

                current_state = json.loads(row["state_json"])
                if state_update:
                    current_state.update(state_update)

                cur = conn.execute(
                    "UPDATE runs SET status = ?, revision = ?, state_json = ?, updated_at = ? "
                    "WHERE workspace_id = ? AND run_id = ? AND revision = ?",
                    (
                        status,
                        new_rev,
                        canonical_json(current_state),
                        now,
                        workspace_id,
                        run_id,
                        expected_revision,
                    ),
                )
                if cur.rowcount == 0:
                    raise SessionConflictError(run_id, expected_revision, actual_rev)

                # Append events transactionally
                if events:
                    seq_cur = conn.execute(
                        "SELECT COALESCE(MAX(seq), 0) FROM run_events "
                        "WHERE workspace_id = ? AND run_id = ?",
                        (workspace_id, run_id),
                    )
                    max_seq = seq_cur.fetchone()[0]
                    for idx, evt in enumerate(events, start=1):
                        seq = max_seq + idx
                        evt_type = evt.get("type", "custom")
                        conn.execute(
                            "INSERT INTO run_events "
                            "(workspace_id, run_id, seq, event_type, payload_json, timestamp) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (workspace_id, run_id, seq, evt_type, canonical_json(evt), now),
                        )

                conn.execute("COMMIT")
                return RunRecord(
                    workspace_id=workspace_id,
                    run_id=run_id,
                    status=status,
                    revision=new_rev,
                    input_data=json.loads(row["input_json"]),
                    state=current_state,
                    metadata=json.loads(row["metadata_json"]),
                    created_at=row["created_at"],
                    updated_at=now,
                )
            except BaseException:
                with contextlib.suppress(Exception):
                    conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    async def get_run_events(self, workspace_id: str, run_id: str) -> list[RunEventRecord]:
        journal = await self._journal(workspace_id, run_id)
        if journal is not None:
            return await journal.get_run_events(workspace_id, run_id)  # type: ignore[no-any-return]
        async with self._async_lock:
            conn = self._get_connection()
            try:
                cursor = conn.execute(
                    "SELECT seq, event_type, payload_json, timestamp FROM run_events "
                    "WHERE workspace_id = ? AND run_id = ? ORDER BY seq ASC",
                    (workspace_id, run_id),
                )
                records = []
                for row in cursor.fetchall():
                    records.append(
                        RunEventRecord(
                            workspace_id=workspace_id,
                            run_id=run_id,
                            seq=row["seq"],
                            event_type=row["event_type"],
                            payload=json.loads(row["payload_json"]),
                            timestamp=row["timestamp"],
                        )
                    )
                return records
            finally:
                conn.close()

    async def close(self) -> None:
        journal = getattr(self, "_execution_store", None)
        if journal is not None:
            await journal.close()
            self._execution_store = None
        async with self._async_lock:
            if self._memory_keeper is not None:
                self._memory_keeper.close()
                self._memory_keeper = None


def get_storage_repository(connection_uri: str) -> StorageRepository:
    """Factory to instantiate the appropriate repository profile."""
    if (
        connection_uri.startswith("sqlite://")
        or connection_uri.endswith(".db")
        or connection_uri == ":memory:"
    ):
        db_path = connection_uri.removeprefix("sqlite://")
        return SQLiteRepository(db_path)
    if connection_uri.startswith("postgresql://") or connection_uri.startswith("postgres://"):
        # PostgreSQL storage is the separate nexura-postgres package (it depends on this one).
        from nexura_runtime.modules.optional_packages import POSTGRES_PACKAGE, require_package

        postgres = require_package(POSTGRES_PACKAGE, "PostgreSQL storage")
        repository: StorageRepository = postgres.PostgresRepository(dsn=connection_uri)
        return repository
    raise ValueError(f"Unsupported storage connection URI: {connection_uri}")
