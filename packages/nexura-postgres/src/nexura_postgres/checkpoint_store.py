# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Postgres checkpoint store for durable SDK agent runs (A6 S5).

``Runner(checkpoint_store=PostgresCheckpointStore(dsn))`` keeps the same
``RunCheckpointStore`` protocol as the SQLite store (checkpoints, the recovery claim with
fenced saves and deletes, and single-use approvals), on Postgres, plus the execution
journal (``PostgresExecutionStore``) in the same database: the run's fenced lease from its
first write, per-call intents and effect records. Lease expiry uses the database clock, so
several hosts can share one database. Every operation on a run is one transaction under a
per-run advisory lock.
"""

from __future__ import annotations

import asyncio
from typing import Any

from nexura_agents.durable import DEFAULT_CLAIM_LEASE_SECONDS, DEFAULT_WORKSPACE, check_fence
from nexura_agents.errors import ApprovalDecisionError

from nexura_postgres.db import noop_reset, normalize_url
from nexura_postgres.execution_store import PostgresExecutionStore

_NOW = "SELECT EXTRACT(EPOCH FROM clock_timestamp())::double precision"
_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS nexura_sdk_checkpoints (
        workspace_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        data TEXT NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL,
        PRIMARY KEY (workspace_id, run_id))""",
    """CREATE INDEX IF NOT EXISTS nexura_sdk_checkpoints_updated
        ON nexura_sdk_checkpoints (workspace_id, updated_at, run_id)""",
    """CREATE TABLE IF NOT EXISTS nexura_sdk_run_claims (
        workspace_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        owner TEXT NOT NULL,
        expires_at DOUBLE PRECISION NOT NULL,
        lease_seconds DOUBLE PRECISION NOT NULL,
        PRIMARY KEY (workspace_id, run_id))""",
    """CREATE TABLE IF NOT EXISTS nexura_sdk_approvals (
        workspace_id TEXT NOT NULL,
        approval_id TEXT NOT NULL,
        payload TEXT NOT NULL,
        expires_at DOUBLE PRECISION NOT NULL,
        consumed_at DOUBLE PRECISION,
        PRIMARY KEY (workspace_id, approval_id))""",
)


class PostgresCheckpointStore:
    """Durable, multi-host checkpoint store for ``Runner(checkpoint_store=...)``.

    Give a DSN (a pool is created on first use and closed by ``close()``) or an asyncpg
    pool you own. Tables are created on first use (or call ``init_db()``). Rows are scoped
    by ``workspace_id``. Checkpoints hold the full transcript: protect the database like
    session data. An asyncpg pool belongs to one event loop; use the store from one loop.
    """

    def __init__(
        self, dsn: str | None = None, *, pool: Any = None, workspace_id: str = DEFAULT_WORKSPACE
    ) -> None:
        if dsn is None and pool is None:
            raise ValueError("Provide a Postgres DSN or pool")
        if not isinstance(workspace_id, str) or not workspace_id:
            raise ValueError("workspace_id must be a nonempty string")
        self.workspace_id = workspace_id
        self._dsn = dsn
        self._pool = pool
        self._owns_pool = pool is None
        self._lock = asyncio.Lock()
        self._ready = False
        self._journal: PostgresExecutionStore | None = None
        self._journal_ready = False

    async def _get_pool(self) -> Any:
        async with self._lock:
            if self._pool is None:
                import asyncpg  # type: ignore[import-untyped]

                self._pool = await asyncpg.create_pool(
                    normalize_url(self._dsn, strict=False),
                    min_size=1,
                    max_size=5,
                    statement_cache_size=0,
                    reset=noop_reset,
                )
            if not self._ready:
                async with self._pool.acquire() as conn, conn.transaction():
                    # Concurrent first use from several processes: create tables once.
                    await conn.execute("SELECT pg_advisory_xact_lock(734658102)")
                    for statement in _SCHEMA:
                        await conn.execute(statement)
                self._ready = True
        return self._pool

    async def init_db(self) -> None:
        """Create the checkpoint tables and apply the execution journal's migrations."""
        await self._get_pool()
        await self.open_execution_store()

    async def open_execution_store(self) -> PostgresExecutionStore:
        """The execution journal on the same pool (migrated on first use)."""
        pool = await self._get_pool()
        if self._journal is None:
            self._journal = PostgresExecutionStore(pool=pool)
        if not self._journal_ready:
            await self._journal.init_db()
            self._journal_ready = True
        return self._journal

    async def _run_lock(self, conn: Any, run_id: str) -> float:
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1::text || '/' || $2::text, 0))",
            self.workspace_id,
            run_id,
        )
        now: float = await conn.fetchval(_NOW)
        return now

    async def _held(self, conn: Any, run_id: str) -> Any:
        return await conn.fetchrow(
            "SELECT owner, expires_at, lease_seconds FROM nexura_sdk_run_claims "
            "WHERE workspace_id = $1 AND run_id = $2",
            self.workspace_id,
            run_id,
        )

    # ------------------------------------------------------------------ checkpoints

    async def save(self, run_id: str, checkpoint_json: str, owner: str | None = None) -> None:
        """Store the checkpoint of ``run_id``; fenced by the recovery claim in one transaction."""
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            now = await self._run_lock(conn, run_id)
            row = await self._held(conn, run_id)
            check_fence(
                run_id,
                owner,
                None if row is None else (str(row["owner"]), float(row["expires_at"])),
                now=now,
            )
            await conn.execute(
                "INSERT INTO nexura_sdk_checkpoints (workspace_id, run_id, data, updated_at) "
                "VALUES ($1, $2, $3, $4) ON CONFLICT (workspace_id, run_id) DO UPDATE "
                "SET data = EXCLUDED.data, updated_at = EXCLUDED.updated_at",
                self.workspace_id,
                run_id,
                checkpoint_json,
                now,
            )
            if row is not None and owner is not None:
                await conn.execute(
                    "UPDATE nexura_sdk_run_claims SET expires_at = $3 "
                    "WHERE workspace_id = $1 AND run_id = $2 AND owner = $4",
                    self.workspace_id,
                    run_id,
                    now + float(row["lease_seconds"]),
                    owner,
                )

    async def load(self, run_id: str) -> str | None:
        """Return the checkpoint of ``run_id`` or None."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            value = await conn.fetchval(
                "SELECT data FROM nexura_sdk_checkpoints WHERE workspace_id = $1 AND run_id = $2",
                self.workspace_id,
                run_id,
            )
        return None if value is None else str(value)

    async def delete(self, run_id: str, owner: str | None = None, *, fenced: bool = False) -> None:
        """Remove the checkpoint and claim of ``run_id`` (``fenced``: only if not stale)."""
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            now = await self._run_lock(conn, run_id)
            if fenced:
                row = await self._held(conn, run_id)
                check_fence(
                    run_id,
                    owner,
                    None if row is None else (str(row["owner"]), float(row["expires_at"])),
                    now=now,
                )
            for table in ("nexura_sdk_checkpoints", "nexura_sdk_run_claims"):
                await conn.execute(
                    f"DELETE FROM {table} WHERE workspace_id = $1 AND run_id = $2",  # noqa: S608
                    self.workspace_id,
                    run_id,
                )

    async def list_incomplete(self, limit: int = 100) -> list[str]:
        """Run ids with a checkpoint, oldest update first."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT run_id FROM nexura_sdk_checkpoints WHERE workspace_id = $1 "
                "ORDER BY updated_at, run_id LIMIT $2",
                self.workspace_id,
                max(int(limit), 0),
            )
        return [str(row["run_id"]) for row in rows]

    async def claim(
        self, run_id: str, owner: str, lease_seconds: float = DEFAULT_CLAIM_LEASE_SECONDS
    ) -> bool:
        """Take the exclusive recovery claim of ``run_id`` (database clock); False when held."""
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            now = await self._run_lock(conn, run_id)
            row = await self._held(conn, run_id)
            if row is not None and float(row["expires_at"]) > now and row["owner"] != owner:
                return False
            await conn.execute(
                "INSERT INTO nexura_sdk_run_claims "
                "(workspace_id, run_id, owner, expires_at, lease_seconds) "
                "VALUES ($1, $2, $3, $4, $5) ON CONFLICT (workspace_id, run_id) DO UPDATE "
                "SET owner = EXCLUDED.owner, expires_at = EXCLUDED.expires_at, "
                "lease_seconds = EXCLUDED.lease_seconds",
                self.workspace_id,
                run_id,
                owner,
                now + float(lease_seconds),
                float(lease_seconds),
            )
            return True

    async def release(self, run_id: str, owner: str) -> None:
        """Give the claim back (no-op when ``owner`` does not hold it)."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM nexura_sdk_run_claims "
                "WHERE workspace_id = $1 AND run_id = $2 AND owner = $3",
                self.workspace_id,
                run_id,
                owner,
            )

    # ------------------------------------------------------------------ approvals

    async def save_approval(self, approval_id: str, payload: str, expires_at: float) -> None:
        """Persist an immutable approval independently of run checkpoint cleanup."""
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO nexura_sdk_approvals (workspace_id, approval_id, payload, expires_at) "
                "VALUES ($1, $2, $3, $4) ON CONFLICT (workspace_id, approval_id) DO NOTHING",
                self.workspace_id,
                approval_id,
                payload,
                float(expires_at),
            )
            row = await conn.fetchrow(
                "SELECT payload, expires_at FROM nexura_sdk_approvals "
                "WHERE workspace_id = $1 AND approval_id = $2",
                self.workspace_id,
                approval_id,
            )
        if row is not None and (row["payload"], float(row["expires_at"])) != (
            payload,
            float(expires_at),
        ):
            raise ApprovalDecisionError("Stored interruption differs from approval envelope.")

    async def load_approval(self, approval_id: str) -> str | None:
        """Load the original approval payload, including after consumption."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            value = await conn.fetchval(
                "SELECT payload FROM nexura_sdk_approvals "
                "WHERE workspace_id = $1 AND approval_id = $2",
                self.workspace_id,
                approval_id,
            )
        return None if value is None else str(value)

    async def consume_approval(self, approval_id: str, payload: str) -> bool:
        """Consume a matching unexpired approval once, atomically across hosts."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            result: str = await conn.execute(
                "UPDATE nexura_sdk_approvals "
                "SET consumed_at = EXTRACT(EPOCH FROM clock_timestamp()) "
                "WHERE workspace_id = $1 AND approval_id = $2 AND payload = $3 "
                "AND consumed_at IS NULL "
                "AND expires_at > EXTRACT(EPOCH FROM clock_timestamp())",
                self.workspace_id,
                approval_id,
                payload,
            )
        return result.rsplit(" ", 1)[-1] == "1"

    async def close(self) -> None:
        """Close a pool this store created; a supplied pool stays open for its owner."""
        if self._pool is not None and self._owns_pool:
            await self._pool.close()
            self._pool = None
            self._ready = False
            self._journal = None
            self._journal_ready = False
