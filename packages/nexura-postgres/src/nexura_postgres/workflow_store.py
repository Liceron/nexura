# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Postgres workflow checkpoint store for ``WorkflowEngine`` (A6 S8).

``WorkflowEngine(store=PostgresWorkflowStore(dsn))`` keeps the store protocol of
``SQLiteWorkflowStore`` on Postgres:

- the latest checkpoint per run, saved with a revision compare-and-swap when the
  checkpoint carries a revision (``WorkflowConflictError`` otherwise);
- an append-only history of every saved checkpoint (inspection, replay and ``fork``);
- atomic create-if-absent for a new run id;
- an atomic ``claim_approval`` (``paused`` -> ``running``) that refuses an expired
  approval, judged by the database clock;
- ``list_children`` and ``list_incomplete`` (top-level ``running`` runs, for recovery);
- the execution journal (``PostgresExecutionStore``) on the same pool, which the engine
  uses for the run lease, step intents and effect records.

Every write is one transaction under a per-run advisory lock, with the row read
``FOR UPDATE``. Times come from ``clock_timestamp()``, so several hosts can share one
database. Tables are created by versioned migrations on first use (or ``init_db()``).
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
from typing import Any

from nexura_agents.errors import WorkflowError

from nexura_postgres.db import noop_reset, normalize_url
from nexura_postgres.execution_store import PostgresExecutionStore
from nexura_runtime.workflow import (
    WorkflowCheckpoint,
    _child_sort_key,
    _is_start_reservation,
    _pending_id,
    _revision_conflict,
    checkpoint_from_dict,
    checkpoint_to_dict,
)

# Serializes concurrent first use (migrations) across processes; distinct from the
# journal's (734658101) and the SDK checkpoint store's (734658102) constants.
_MIGRATION_LOCK = 734658103

#: Versioned migrations: (version, name, statements). Applied once each, in order.
MIGRATIONS: tuple[tuple[int, str, tuple[str, ...]], ...] = (
    (
        1,
        "workflow checkpoints and history",
        (
            """CREATE TABLE IF NOT EXISTS nexura_workflow_checkpoints (
                workflow_run_id TEXT PRIMARY KEY,
                workspace_id TEXT,
                status TEXT NOT NULL,
                current_node_id TEXT,
                step_count INTEGER NOT NULL,
                spec_hash TEXT NOT NULL,
                approval_id TEXT,
                approval_expires_at DOUBLE PRECISION,
                revision BIGINT NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL,
                checkpoint_json TEXT NOT NULL)""",
            """CREATE INDEX IF NOT EXISTS nexura_workflow_checkpoints_status
                ON nexura_workflow_checkpoints (workspace_id, status, updated_at)""",
            """CREATE INDEX IF NOT EXISTS nexura_workflow_checkpoints_prefix
                ON nexura_workflow_checkpoints (workflow_run_id text_pattern_ops)""",
            """CREATE TABLE IF NOT EXISTS nexura_workflow_checkpoint_history (
                seq BIGSERIAL PRIMARY KEY,
                workflow_run_id TEXT NOT NULL,
                workspace_id TEXT,
                revision BIGINT NOT NULL,
                checkpoint_json TEXT NOT NULL,
                created_at DOUBLE PRECISION NOT NULL)""",
            """CREATE INDEX IF NOT EXISTS nexura_workflow_history_run
                ON nexura_workflow_checkpoint_history (workflow_run_id, seq)""",
        ),
    ),
)


_INSERT = """INSERT INTO nexura_workflow_checkpoints (
    workflow_run_id, workspace_id, status, current_node_id, step_count,
    spec_hash, approval_id, approval_expires_at, revision, updated_at,
    checkpoint_json)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)"""
_HISTORY = """
INSERT INTO nexura_workflow_checkpoint_history
    (workflow_run_id, workspace_id, revision, checkpoint_json, created_at)
SELECT $1, $2, $9, $11, $10 FROM latest"""
#: Store a checkpoint as the run's latest (MVP WP11a: one statement with its history row).
_WRITE = (
    _INSERT
    + """
ON CONFLICT (workflow_run_id) DO UPDATE SET
    workspace_id = EXCLUDED.workspace_id,
    status = EXCLUDED.status,
    current_node_id = EXCLUDED.current_node_id,
    step_count = EXCLUDED.step_count,
    spec_hash = EXCLUDED.spec_hash,
    approval_id = EXCLUDED.approval_id,
    approval_expires_at = EXCLUDED.approval_expires_at,
    revision = EXCLUDED.revision,
    updated_at = EXCLUDED.updated_at,
    checkpoint_json = EXCLUDED.checkpoint_json"""
)
_WRITE_WITH_HISTORY = "WITH latest AS (" + _WRITE + " RETURNING 1)" + _HISTORY
#: A new run's first checkpoint; no row is returned when the id is taken.
_CREATE = _INSERT + " ON CONFLICT (workflow_run_id) DO NOTHING RETURNING 1"
_CREATE_WITH_HISTORY = "WITH latest AS (" + _CREATE + ")" + _HISTORY + " RETURNING 1"


class _SharedPoolExecutionStore(PostgresExecutionStore):
    """The execution journal on its workflow store's pool (the store owns the pool)."""

    def __init__(self, owner: PostgresWorkflowStore) -> None:
        super().__init__(owner._dsn, pool=owner._pool)
        self._owner = owner
        self._owns_pool = False

    async def _get_pool(self) -> Any:
        return await self._owner._get_pool()

    async def close(self) -> None:
        """No-op: the pool belongs to the workflow store (close that instead)."""


class PostgresWorkflowStore:
    """Durable, multi-host workflow checkpoint store (same protocol as ``SQLiteWorkflowStore``).

    Give a DSN (a pool is created on first use and closed by ``close()``) or an asyncpg
    pool you own. ``workspace_id`` scopes the store to one workspace: runs of other
    workspaces are invisible to it and cannot be written through it. Without it, one
    store serves every workspace and ``WorkflowEngine`` checks the owner of each run, as
    with SQLite. Checkpoints hold workflow state: protect the database accordingly.

    An asyncpg pool belongs to one event loop. A pool this store created is replaced when
    the store is used from another loop, and opened again on use after ``close()``; a
    supplied pool must be used from its own loop.
    """

    def __init__(
        self,
        dsn: str | None = None,
        *,
        pool: Any = None,
        workspace_id: str | None = None,
        max_pool_size: int = 10,
    ) -> None:
        if dsn is None and pool is None:
            raise ValueError("Provide a Postgres DSN or pool")
        if workspace_id is not None and (not isinstance(workspace_id, str) or not workspace_id):
            raise ValueError("workspace_id must be a nonempty string or None")
        if isinstance(max_pool_size, bool) or not isinstance(max_pool_size, int):
            raise ValueError("max_pool_size must be an integer >= 2")
        if max_pool_size < 2:
            raise ValueError("max_pool_size must be an integer >= 2")
        self.workspace_id = workspace_id
        self._dsn = dsn
        self._pool = pool
        self._owns_pool = pool is None
        self._max_pool_size = max_pool_size
        self._pool_loop: asyncio.AbstractEventLoop | None = None
        self._lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None
        self._ready = False
        self._journal = _SharedPoolExecutionStore(self)
        self._journal_ready = False

    # ------------------------------------------------------------------ pool and schema

    def _loop_lock(self, loop: asyncio.AbstractEventLoop) -> asyncio.Lock:
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

    async def _get_pool(self) -> Any:
        loop = asyncio.get_running_loop()
        async with self._loop_lock(loop):
            if self._owns_pool and self._pool is not None and self._pool_loop is not loop:
                # A pool created on another (finished) event loop cannot be used here.
                old, self._pool = self._pool, None
                _terminate(old)
            if self._pool is None:
                import asyncpg  # type: ignore[import-untyped]

                self._pool = await asyncpg.create_pool(
                    normalize_url(self._dsn, strict=False),
                    min_size=1,
                    max_size=self._max_pool_size,
                    statement_cache_size=0,
                    reset=noop_reset,
                )
                self._pool_loop = loop
            if not self._ready:
                async with self._pool.acquire() as conn, conn.transaction():
                    await self._migrate(conn)
                self._ready = True
        return self._pool

    @staticmethod
    async def _migrate(conn: Any) -> None:
        # Concurrent first use from several processes: one migrates, the others wait.
        await conn.execute(f"SELECT pg_advisory_xact_lock({_MIGRATION_LOCK})")
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS nexura_workflow_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at DOUBLE PRECISION NOT NULL)"""
        )
        applied = {
            int(row["version"])
            for row in await conn.fetch("SELECT version FROM nexura_workflow_migrations")
        }
        known = {version for version, _, _ in MIGRATIONS}
        unknown = sorted(applied - known)
        if unknown:
            raise WorkflowError(
                f"The workflow tables were migrated to version {unknown[-1]} by a newer "
                "nexura-postgres; upgrade this package to use them."
            )
        for version, name, statements in MIGRATIONS:
            if version in applied:
                continue
            for statement in statements:
                await conn.execute(statement)
            await conn.execute(
                "INSERT INTO nexura_workflow_migrations (version, name, applied_at) "
                "VALUES ($1, $2, EXTRACT(EPOCH FROM clock_timestamp()))",
                version,
                name,
            )

    async def init_db(self) -> None:
        """Apply the workflow tables' migrations and the execution journal's."""
        await self._get_pool()
        await self.open_execution_store()

    async def open_execution_store(self) -> PostgresExecutionStore:
        """The execution journal on the same pool (migrated on first use)."""
        await self._get_pool()
        if not self._journal_ready:
            await self._journal.init_db()
            self._journal_ready = True
        return self._journal

    @property
    def execution_store(self) -> PostgresExecutionStore:
        """The execution journal on this store's pool (``open_execution_store()`` migrates it).

        Closing it is a no-op; ``close()`` on this store closes the pool both use.
        """
        return self._journal

    # ------------------------------------------------------------------ helpers

    @staticmethod
    async def _run_lock(conn: Any, workflow_run_id: str) -> float:
        """Take the run's advisory lock, then read the database clock (one statement, MVP
        WP11a). The clock is the outer query's projection of the locked subquery's row, so
        it is read after the lock is granted; the subquery has a volatile function, so the
        planner cannot flatten it into the outer query."""
        now: float = await conn.fetchval(
            "SELECT EXTRACT(EPOCH FROM clock_timestamp())::double precision FROM ("
            "SELECT pg_advisory_xact_lock(hashtextextended('nexura-workflow/' || $1::text, 0))"
            ") AS locked",
            workflow_run_id,
        )
        return now

    def _visible(self, row: Any) -> bool:
        return self.workspace_id is None or row["workspace_id"] == self.workspace_id

    @staticmethod
    def _checkpoint(row: Any) -> WorkflowCheckpoint:
        checkpoint = checkpoint_from_dict(json.loads(row["checkpoint_json"]))
        checkpoint.revision = int(row["revision"])
        return checkpoint

    def _scope_sql(self, index: int) -> tuple[str, list[Any]]:
        if self.workspace_id is None:
            return "", []
        return f" AND workspace_id = ${index}", [self.workspace_id]

    # ------------------------------------------------------------------ protocol

    async def get(self, workflow_run_id: str) -> WorkflowCheckpoint | None:
        """The latest checkpoint of a run, or ``None`` if it does not exist."""
        pool = await self._get_pool()
        scope, args = self._scope_sql(2)
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT revision, checkpoint_json FROM nexura_workflow_checkpoints "
                f"WHERE workflow_run_id = $1{scope}",
                workflow_run_id,
                *args,
            )
        return None if row is None else self._checkpoint(row)

    async def run_identities(self, run_ids: list[str]) -> dict[str, dict[str, Any]]:
        """A page of runs' active and starting identities in one query (P38 C6).

        Returns ``{run_id: {"definition_digest", "workflow_version",
        "started_workflow_version"}}`` for the runs visible to this store; missing runs are
        left out. Only the identity fields are read from the checkpoint JSON, never the
        run's state. ``workflow_run_identities`` and ``GET /runs`` use it.
        """
        if not run_ids:
            return {}
        pool = await self._get_pool()
        scope, args = self._scope_sql(2)
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT c.workflow_run_id, c.spec_hash, "
                "c.checkpoint_json::json->'workflow_version' AS version, "
                "(SELECT h.checkpoint_json::json->'workflow_version' "
                "FROM nexura_workflow_checkpoint_history h "
                "WHERE h.workflow_run_id = c.workflow_run_id ORDER BY h.seq LIMIT 1) AS started "
                "FROM nexura_workflow_checkpoints c "
                f"WHERE c.workflow_run_id = ANY($1::text[]){scope}",
                list(run_ids),
                *args,
            )
        return {
            row["workflow_run_id"]: {
                "definition_digest": row["spec_hash"],
                "workflow_version": json.loads(row["version"]) if row["version"] else None,
                "started_workflow_version": json.loads(row["started"]) if row["started"] else None,
            }
            for row in rows
        }

    async def save(self, checkpoint: WorkflowCheckpoint) -> None:
        """Store a checkpoint as the run's latest and append it to its history.

        Fenced when ``checkpoint.revision`` is set: the stored revision must still equal
        it (``WorkflowConflictError`` otherwise); it is then set to the new revision.
        """
        await self._write(checkpoint, create=False)

    async def create(self, checkpoint: WorkflowCheckpoint) -> bool:
        """Save the first checkpoint of a new run; False if the run id is taken."""
        return await self._write(checkpoint, create=True)

    async def _write(self, checkpoint: WorkflowCheckpoint, *, create: bool) -> bool:
        run_id = checkpoint.workflow_run_id
        owner = checkpoint.workspace_id
        if self.workspace_id is not None and owner not in (None, self.workspace_id):
            raise WorkflowError(
                f"Checkpoint of workflow run '{run_id}' belongs to workspace '{owner}', "
                f"not to this store's workspace '{self.workspace_id}'."
            )
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            now = await self._run_lock(conn, run_id)
            if create:
                # MVP WP11a: no row read; the insert itself refuses a taken id (visible to this
                # store or not), as the read did.
                return await self._create_row(conn, checkpoint, now)
            row = await conn.fetchrow(
                "SELECT workspace_id, revision, checkpoint_json FROM nexura_workflow_checkpoints "
                "WHERE workflow_run_id = $1 FOR UPDATE",
                run_id,
            )
            if row is not None and not self._visible(row):
                if create:
                    return False  # the id is taken (by a run this store cannot see)
                raise WorkflowError(
                    f"Workflow run '{run_id}' belongs to another workspace than this "
                    f"store's ('{self.workspace_id}')."
                )
            if create and row is not None:
                return False
            expected = None if create else checkpoint.revision
            current = int(row["revision"]) if row is not None else None
            if expected is not None and current is not None and current != expected:
                raise _revision_conflict(run_id, expected, current)

            stored = copy.copy(checkpoint)
            if row is not None:
                previous = json.loads(row["checkpoint_json"])
                if stored.forked_from is None:
                    stored.forked_from = previous.get("forked_from")
                if stored.workspace_id is None:
                    stored.workspace_id = row["workspace_id"]
            if stored.workspace_id is None:
                stored.workspace_id = self.workspace_id
            stored.updated_at = now
            if create:
                revision = 1
            elif current is not None:
                revision = current + 1
            else:
                revision = (expected or 0) + 1
            document = json.dumps(checkpoint_to_dict(stored))
            pending = stored.pending_approval
            values = (
                run_id,
                stored.workspace_id,
                stored.status,
                stored.current_node_id,
                stored.step_count,
                stored.spec_hash,
                _pending_id(stored),
                pending.expires_at if pending is not None else None,
                revision,
                now,
                document,
            )
            # The latest checkpoint and its history row in one statement (MVP WP11a); a start
            # reservation has no history row.
            history = not (create and _is_start_reservation(stored))
            await conn.execute(_WRITE_WITH_HISTORY if history else _WRITE, *values)
        if create or expected is not None:
            checkpoint.revision = revision
        return True

    async def _create_row(self, conn: Any, checkpoint: WorkflowCheckpoint, now: float) -> bool:
        """The first checkpoint (revision 1), under the run lock; False when the id is taken."""
        stored = copy.copy(checkpoint)
        if stored.workspace_id is None:
            stored.workspace_id = self.workspace_id
        stored.updated_at = now
        pending = stored.pending_approval
        values = (
            checkpoint.workflow_run_id,
            stored.workspace_id,
            stored.status,
            stored.current_node_id,
            stored.step_count,
            stored.spec_hash,
            _pending_id(stored),
            pending.expires_at if pending is not None else None,
            1,
            now,
            json.dumps(checkpoint_to_dict(stored)),
        )
        # A start reservation has no history row.
        sql = _CREATE if _is_start_reservation(stored) else _CREATE_WITH_HISTORY
        if await conn.fetchval(sql, *values) is None:
            return False
        checkpoint.revision = 1
        return True

    async def history(self, workflow_run_id: str) -> list[WorkflowCheckpoint]:
        """Every checkpoint saved for the run, oldest first."""
        pool = await self._get_pool()
        scope, args = self._scope_sql(2)
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT checkpoint_json FROM nexura_workflow_checkpoint_history "
                f"WHERE workflow_run_id = $1{scope} ORDER BY seq",
                workflow_run_id,
                *args,
            )
        return [checkpoint_from_dict(json.loads(row["checkpoint_json"])) for row in rows]

    async def list_children(self, parent_id: str) -> list[WorkflowCheckpoint]:
        """Latest checkpoint of every child run (ids starting ``<parent_id>/``), by id."""
        pool = await self._get_pool()
        scope, args = self._scope_sql(2)
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT workflow_run_id, revision, checkpoint_json "
                "FROM nexura_workflow_checkpoints "
                f"WHERE starts_with(workflow_run_id, $1){scope}",
                f"{parent_id}/",
                *args,
            )
        rows = sorted(rows, key=lambda row: _child_sort_key(row["workflow_run_id"]))
        return [self._checkpoint(row) for row in rows]

    async def list_incomplete(
        self, limit: int = 100, *, workspace_id: str | None = None
    ) -> list[str]:
        """Ids of top-level runs whose latest checkpoint is ``running``, oldest update first.

        These are the candidates for ``WorkflowEngine.recover``: a run is only taken over
        once its journal lease is free or expired. Sub-workflow children are left out
        (their parent's ``recover`` continues them). ``workspace_id`` narrows the list
        (a scoped store lists only its own workspace).
        """
        if workspace_id is not None and self.workspace_id not in (None, workspace_id):
            return []
        wanted = workspace_id if workspace_id is not None else self.workspace_id
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT workflow_run_id FROM nexura_workflow_checkpoints "
                "WHERE status = 'running' AND strpos(workflow_run_id, '/') = 0 "
                "AND ($1::text IS NULL OR workspace_id = $1) "
                "ORDER BY updated_at, workflow_run_id LIMIT $2",
                wanted,
                max(int(limit), 0),
            )
        return [str(row["workflow_run_id"]) for row in rows]

    async def claim_approval(self, workflow_run_id: str, approval_id: str) -> bool:
        """Atomically claim a pending approval or interrupt; ``False`` if already claimed.

        A pending approval past its ``expires_at`` (database clock) is never claimed.
        """
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            now = await self._run_lock(conn, workflow_run_id)
            row = await conn.fetchrow(
                "SELECT workspace_id, status, approval_id, approval_expires_at, revision, "
                "checkpoint_json FROM nexura_workflow_checkpoints "
                "WHERE workflow_run_id = $1 FOR UPDATE",
                workflow_run_id,
            )
            if (
                row is None
                or not self._visible(row)
                or row["status"] != "paused"
                or row["approval_id"] != approval_id
            ):
                return False
            expires_at = row["approval_expires_at"]
            if expires_at is not None and float(expires_at) < now:
                return False  # expired: never claimed (checked under the same lock)
            document = json.loads(row["checkpoint_json"])
            document["status"] = "running"
            document["updated_at"] = now
            await conn.execute(
                "UPDATE nexura_workflow_checkpoints SET status = 'running', updated_at = $2, "
                "revision = revision + 1, checkpoint_json = $3 WHERE workflow_run_id = $1",
                workflow_run_id,
                now,
                json.dumps(document),
            )
        return True

    async def close(self) -> None:
        """Close a pool this store created; a supplied pool stays open for its owner."""
        pool, self._pool = (self._pool, None) if self._owns_pool else (None, self._pool)
        if pool is not None:
            try:
                same_loop = self._pool_loop is asyncio.get_running_loop()
            except RuntimeError:
                same_loop = False
            if same_loop:
                await pool.close()
            else:
                _terminate(pool)
        self._pool_loop = None
        self._ready = False
        self._journal_ready = False

    def close_sync(self) -> None:
        """Close the store from synchronous code (terminates a pool this store created)."""
        pool, self._pool = (self._pool, None) if self._owns_pool else (None, self._pool)
        if pool is not None:
            _terminate(pool)
        self._pool_loop = None
        self._ready = False
        self._journal_ready = False

    async def __aenter__(self) -> PostgresWorkflowStore:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()


def _terminate(pool: Any) -> None:
    """Drop a pool at once. Its event loop may already be closed (a finished
    ``asyncio.run``); its connections then go with that loop."""
    with contextlib.suppress(Exception):
        pool.terminate()
