# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""PostgreSQL storage repository (asyncpg), with the same contract as ``SQLiteRepository``.

Moved from ``nexura_runtime.storage.repository`` into the ``nexura-postgres`` package. The
runtime's ``get_storage_repository("postgresql://...")`` returns this class when the package is
installed and otherwise names ``pip install nexura-postgres``.
"""

from __future__ import annotations

import copy
import json
import time
from typing import Any

from nexura_agents.contracts.canonical import canonical_json
from nexura_agents.contracts.errors import MissingDependencyError
from nexura_agents.errors import SessionConflictError, StorageError
from nexura_agents.session import SessionSnapshot, deserialize_session, serialize_session
from nexura_agents.types import Message

from nexura_runtime.storage.models import RunEventRecord, RunRecord

#: Raised when asyncpg, a dependency of this package, cannot be imported.
ASYNCPG_MISSING = (
    "PostgreSQL storage requires asyncpg, a dependency of nexura-postgres. "
    "Install it with: pip install nexura-postgres"
)


class PostgresRepository:
    """PostgreSQL storage repository using asyncpg connection pool."""

    def __init__(self, dsn: str | None = None, pool: Any = None) -> None:
        self.dsn = dsn
        self._pool = pool
        if self._pool is None and self.dsn is not None:
            try:
                import asyncpg  # type: ignore[import-untyped]  # noqa: F401
            except ImportError as err:
                raise MissingDependencyError("asyncpg", message=ASYNCPG_MISSING) from err

    async def _get_pool(self) -> Any:
        if self._pool is None:
            if not self.dsn:
                raise StorageError("No PostgreSQL DSN or pool provided.")
            import asyncpg

            self._pool = await asyncpg.create_pool(self.dsn)
        return self._pool

    async def init_db(self) -> None:
        """Create PostgreSQL tables if they do not exist."""
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute("""
                    CREATE TABLE IF NOT EXISTS unified_sessions (
                        workspace_id TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        revision INTEGER NOT NULL,
                        data TEXT NOT NULL,
                        updated_at DOUBLE PRECISION NOT NULL,
                        PRIMARY KEY (workspace_id, session_id)
                    );
                    CREATE TABLE IF NOT EXISTS runs (
                        workspace_id TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        status TEXT NOT NULL,
                        revision INTEGER NOT NULL,
                        input_json TEXT NOT NULL,
                        state_json TEXT NOT NULL,
                        metadata_json TEXT NOT NULL,
                        created_at DOUBLE PRECISION NOT NULL,
                        updated_at DOUBLE PRECISION NOT NULL,
                        PRIMARY KEY (workspace_id, run_id)
                    );
                    CREATE TABLE IF NOT EXISTS run_events (
                        workspace_id TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        seq INTEGER NOT NULL,
                        event_type TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        timestamp DOUBLE PRECISION NOT NULL,
                        PRIMARY KEY (workspace_id, run_id, seq)
                    );
                """)

    async def get_session(self, workspace_id: str, session_id: str) -> SessionSnapshot | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT data, revision FROM unified_sessions "
                "WHERE workspace_id = $1 AND session_id = $2",
                workspace_id,
                session_id,
            )
            if row is None:
                return None
            sess_id, _, msgs = deserialize_session(row["data"])
            return SessionSnapshot(session_id=sess_id, messages=msgs, revision=row["revision"])

    async def save_session(
        self,
        workspace_id: str,
        session_id: str,
        messages: list[Message],
        expected_revision: int,
    ) -> int:
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT revision FROM unified_sessions "
                "WHERE workspace_id = $1 AND session_id = $2 FOR UPDATE",
                workspace_id,
                session_id,
            )
            if row is None:
                if expected_revision != 0:
                    raise SessionConflictError(session_id, expected_revision, 0)
                new_revision = 1
                data_json = serialize_session(session_id, new_revision, messages)
                inserted_revision = await conn.fetchval(
                    "INSERT INTO unified_sessions "
                    "(workspace_id, session_id, revision, data, updated_at) "
                    "VALUES ($1, $2, $3, $4, $5) "
                    "ON CONFLICT (workspace_id, session_id) DO NOTHING RETURNING revision",
                    workspace_id,
                    session_id,
                    new_revision,
                    data_json,
                    time.time(),
                )
                if inserted_revision is None:
                    actual_revision = await conn.fetchval(
                        "SELECT revision FROM unified_sessions "
                        "WHERE workspace_id=$1 AND session_id=$2",
                        workspace_id,
                        session_id,
                    )
                    raise SessionConflictError(session_id, expected_revision, actual_revision)
            else:
                actual_rev = row["revision"]
                if actual_rev != expected_revision:
                    raise SessionConflictError(session_id, expected_revision, actual_rev)
                new_revision = expected_revision + 1
                data_json = serialize_session(session_id, new_revision, messages)
                res = await conn.execute(
                    "UPDATE unified_sessions SET revision = $1, data = $2, updated_at = $3 "
                    "WHERE workspace_id = $4 AND session_id = $5 AND revision = $6",
                    new_revision,
                    data_json,
                    time.time(),
                    workspace_id,
                    session_id,
                    expected_revision,
                )
                if res == "UPDATE 0":
                    raise SessionConflictError(session_id, expected_revision, actual_rev)
            return new_revision

    async def create_run(
        self,
        workspace_id: str,
        run_id: str,
        initial_state: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RunRecord:
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
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
            await conn.execute(
                "INSERT INTO runs (workspace_id, run_id, status, revision, input_json, "
                "state_json, metadata_json, created_at, updated_at) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
                workspace_id,
                run_id,
                rec.status,
                rec.revision,
                canonical_json(rec.input_data),
                canonical_json(rec.state),
                canonical_json(rec.metadata),
                rec.created_at,
                rec.updated_at,
            )
            return rec

    async def _journal(self, workspace_id: str, run_id: str) -> Any:
        """The execution-journal bridge when this run is a journaled durable graph."""
        from nexura_runtime.storage.execution_bridge import journal_bridge

        async def probe() -> bool:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                try:
                    row = await conn.fetchrow(
                        "SELECT 1 FROM nexura_exec_runs WHERE workspace_id = $1 AND run_id = $2",
                        workspace_id,
                        run_id,
                    )
                except Exception as exc:
                    if type(exc).__name__ == "UndefinedTableError":
                        return False  # the journal was never created in this database
                    raise
            return row is not None

        return await journal_bridge(self, workspace_id, run_id, probe)

    async def get_run(self, workspace_id: str, run_id: str) -> RunRecord | None:
        # DurableExecutionEngine runs live in the execution journal; this is their view.
        journal = await self._journal(workspace_id, run_id)
        if journal is not None:
            return await journal.get_run(workspace_id, run_id)  # type: ignore[no-any-return]
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT status, revision, input_json, state_json, metadata_json, "
                "created_at, updated_at FROM runs WHERE workspace_id = $1 AND run_id = $2",
                workspace_id,
                run_id,
            )
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
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT status, revision, input_json, state_json, metadata_json, "
                "created_at FROM runs WHERE workspace_id = $1 AND run_id = $2 FOR UPDATE",
                workspace_id,
                run_id,
            )
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

            res = await conn.execute(
                "UPDATE runs SET status = $1, revision = $2, state_json = $3, "
                "updated_at = $4 WHERE workspace_id = $5 AND run_id = $6 AND revision = $7",
                status,
                new_rev,
                canonical_json(current_state),
                now,
                workspace_id,
                run_id,
                expected_revision,
            )
            if res == "UPDATE 0":
                raise SessionConflictError(run_id, expected_revision, actual_rev)

            if events:
                max_seq = await conn.fetchval(
                    "SELECT COALESCE(MAX(seq), 0) FROM run_events "
                    "WHERE workspace_id = $1 AND run_id = $2",
                    workspace_id,
                    run_id,
                )
                for idx, evt in enumerate(events, start=1):
                    seq = max_seq + idx
                    evt_type = evt.get("type", "custom")
                    await conn.execute(
                        "INSERT INTO run_events (workspace_id, run_id, seq, event_type, "
                        "payload_json, timestamp) VALUES ($1, $2, $3, $4, $5, $6)",
                        workspace_id,
                        run_id,
                        seq,
                        evt_type,
                        canonical_json(evt),
                        now,
                    )

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

    async def get_run_events(self, workspace_id: str, run_id: str) -> list[RunEventRecord]:
        journal = await self._journal(workspace_id, run_id)
        if journal is not None:
            return await journal.get_run_events(workspace_id, run_id)  # type: ignore[no-any-return]
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT seq, event_type, payload_json, timestamp FROM run_events "
                "WHERE workspace_id = $1 AND run_id = $2 ORDER BY seq ASC",
                workspace_id,
                run_id,
            )
            return [
                RunEventRecord(
                    workspace_id=workspace_id,
                    run_id=run_id,
                    seq=r["seq"],
                    event_type=r["event_type"],
                    payload=json.loads(r["payload_json"]),
                    timestamp=r["timestamp"],
                )
                for r in rows
            ]

    async def close(self) -> None:
        journal = getattr(self, "_execution_store", None)
        if journal is not None:
            await journal.close()
            self._execution_store = None
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
