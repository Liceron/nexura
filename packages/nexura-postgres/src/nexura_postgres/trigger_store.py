# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Postgres trigger store: webhooks, receipts, schedules and the trigger job queue (A6 S10b).

``PostgresTriggerStore(dsn)`` has the contract of ``SqliteTriggerStore``
(``nexura_runtime.service.triggers.store.TriggerStore``), so ``TriggerManager`` and
``TriggerWorker`` work unchanged on it:

- webhook endpoints (signing secrets encrypted at rest) and schedules, each owned by one
  workspace (an id owned by another workspace raises ``TriggerConflictError``);
- an atomic "record receipt and enqueue job" that de-duplicates deliveries per
  ``(endpoint_id, event_id)``;
- a schedule ``advance_schedule`` compare-and-swap, so several workers never double-fire;
- the job queue: ``claim_pending_job`` (compare-and-swap ``pending`` -> ``claimed`` with a
  lease), ``renew_job_lease`` (owner only), ``reclaim_expired_jobs`` (rows locked
  ``FOR UPDATE SKIP LOCKED``, so two reclaimers never take the same job) and an
  owner-guarded ``complete_job``;
- named leases (``acquire_lease`` / ``release_lease``, MVP WP9): one row per name, taken by an
  insert-or-take-over-if-expired statement, so the trigger tick runs one pass at a time across
  instances.

Every operation is one transaction. Lease times come from the database clock
(``clock_timestamp()``) unless the caller passes ``now``, so workers on several hosts agree
on expiry. The job lease only coordinates the queue: the run lease in the execution journal
decides who executes a job's run (Execution Contract v1).

The trigger store protocol is synchronous (the service and worker call it from sync and async
code alike). The store runs its asyncpg pool on a private event loop in a daemon thread and
waits for each call. Tables are created by versioned migrations on first use (or
``init_db()``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import threading
from collections.abc import Awaitable, Callable, Collection, Coroutine
from typing import Any, TypeVar

from nexura_postgres.db import noop_reset, normalize_url
from nexura_runtime.service.triggers.errors import TriggerConflictError, TriggerError
from nexura_runtime.service.triggers.models import (
    DurableTriggerJob,
    ScheduleConfig,
    WebhookConfig,
    WebhookReceipt,
)
from nexura_runtime.service.triggers.secretbox import ENV_KEY, SecretBox
from nexura_runtime.service.triggers.store import (
    DEFAULT_LEASE_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    lease_expired_message,
    require_keep_run,
)

_T = TypeVar("_T")

_NOW = "SELECT EXTRACT(EPOCH FROM clock_timestamp())::double precision"
# Serializes concurrent first use (migrations) across processes; distinct from the
# journal's (734658101), the SDK checkpoint store's (734658102) and the workflow store's
# (734658103) constants.
_MIGRATION_LOCK = 734658104

#: Versioned migrations: (version, name, statements). Applied once each, in order.
MIGRATIONS: tuple[tuple[int, str, tuple[str, ...]], ...] = (
    (
        1,
        "webhooks, receipts, schedules and trigger jobs",
        (
            """CREATE TABLE IF NOT EXISTS nexura_trigger_webhooks (
                endpoint_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                workflow_id TEXT NOT NULL,
                release_id TEXT NOT NULL,
                secret TEXT NOT NULL,
                replay_window_seconds INTEGER NOT NULL,
                is_active BOOLEAN NOT NULL,
                allow_legacy_signature BOOLEAN NOT NULL DEFAULT TRUE)""",
            """CREATE TABLE IF NOT EXISTS nexura_trigger_receipts (
                endpoint_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                job_id TEXT NOT NULL,
                received_at DOUBLE PRECISION NOT NULL,
                PRIMARY KEY (endpoint_id, event_id))""",
            """CREATE TABLE IF NOT EXISTS nexura_trigger_schedules (
                schedule_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                workflow_id TEXT NOT NULL,
                release_id TEXT NOT NULL,
                cron_expr TEXT,
                interval_seconds DOUBLE PRECISION,
                timezone TEXT NOT NULL,
                missed_run_policy TEXT NOT NULL,
                next_run_at DOUBLE PRECISION NOT NULL,
                last_run_at DOUBLE PRECISION,
                is_active BOOLEAN NOT NULL,
                payload_json TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS nexura_trigger_jobs (
                job_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                trigger_type TEXT NOT NULL,
                trigger_source_id TEXT NOT NULL,
                workflow_id TEXT NOT NULL,
                release_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL,
                claimed_by TEXT,
                claimed_at DOUBLE PRECISION,
                created_at DOUBLE PRECISION NOT NULL,
                completed_at DOUBLE PRECISION,
                error_message TEXT,
                run_id TEXT,
                lease_expires_at DOUBLE PRECISION,
                attempts INTEGER NOT NULL DEFAULT 0,
                previous_run_ids_json TEXT)""",
            """CREATE INDEX IF NOT EXISTS nexura_trigger_jobs_status
                ON nexura_trigger_jobs (status, created_at)""",
        ),
    ),
    (
        2,
        "named leases (the trigger tick, MVP WP9)",
        (
            """CREATE TABLE IF NOT EXISTS nexura_trigger_leases (
                name TEXT PRIMARY KEY,
                holder TEXT NOT NULL,
                expires_at DOUBLE PRECISION NOT NULL)""",
        ),
    ),
)

_JOB_INSERT = """INSERT INTO nexura_trigger_jobs
    (job_id, workspace_id, trigger_type, trigger_source_id, workflow_id, release_id,
     payload_json, status, claimed_by, claimed_at, created_at, completed_at, error_message)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)"""


def _job_values(job: DurableTriggerJob) -> tuple[Any, ...]:
    return (
        job.job_id,
        job.workspace_id,
        job.trigger_type,
        job.trigger_source_id,
        job.workflow_id,
        job.release_id,
        json.dumps(job.payload),
        job.status,
        job.claimed_by,
        job.claimed_at,
        job.created_at,
        job.completed_at,
        job.error_message,
    )


def _row_to_job(row: Any) -> DurableTriggerJob:
    previous = row["previous_run_ids_json"]
    return DurableTriggerJob(
        job_id=row["job_id"],
        workspace_id=row["workspace_id"],
        trigger_type=row["trigger_type"],
        trigger_source_id=row["trigger_source_id"],
        workflow_id=row["workflow_id"],
        release_id=row["release_id"],
        payload=json.loads(row["payload_json"]),
        status=row["status"],
        claimed_by=row["claimed_by"],
        claimed_at=row["claimed_at"],
        created_at=row["created_at"],
        completed_at=row["completed_at"],
        error_message=row["error_message"],
        run_id=row["run_id"],
        lease_expires_at=row["lease_expires_at"],
        attempts=row["attempts"] or 0,
        previous_run_ids=json.loads(previous) if previous else [],
    )


def _row_to_schedule(row: Any) -> ScheduleConfig:
    return ScheduleConfig(
        schedule_id=row["schedule_id"],
        workspace_id=row["workspace_id"],
        workflow_id=row["workflow_id"],
        release_id=row["release_id"],
        cron_expr=row["cron_expr"],
        interval_seconds=row["interval_seconds"],
        timezone=row["timezone"],
        missed_run_policy=row["missed_run_policy"],
        next_run_at=row["next_run_at"],
        last_run_at=row["last_run_at"],
        is_active=bool(row["is_active"]),
        payload=json.loads(row["payload_json"]),
    )


def _affected(status: str) -> int:
    """Rows affected, from an asyncpg command tag such as ``UPDATE 1`` or ``INSERT 0 1``."""
    last = status.rsplit(" ", 1)[-1]
    return int(last) if last.isdigit() else 0


class _LoopThread:
    """A private event loop in a daemon thread; sync callers wait for coroutines on it."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    def _ensure(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is not None and self._thread is not None and self._thread.is_alive():
                return self._loop
            loop = asyncio.new_event_loop()
            ready = threading.Event()

            def main() -> None:
                asyncio.set_event_loop(loop)
                loop.call_soon(ready.set)
                loop.run_forever()

            thread = threading.Thread(target=main, name=self._name, daemon=True)
            thread.start()
            ready.wait()
            self._loop, self._thread = loop, thread
            return loop

    def run(self, coro: Coroutine[Any, Any, _T], timeout: float) -> _T:
        if threading.current_thread() is self._thread:
            coro.close()
            raise RuntimeError("PostgresTriggerStore was called from its own event loop thread")
        loop = self._ensure()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result(timeout)
        except TimeoutError:
            future.cancel()
            raise

    def stop(self, before: Callable[[], Coroutine[Any, Any, None]] | None = None) -> None:
        with self._lock:
            loop, thread = self._loop, self._thread
            self._loop, self._thread = None, None
        if loop is None or thread is None:
            return
        if before is not None and thread.is_alive():
            with contextlib.suppress(Exception):  # closing must not fail
                asyncio.run_coroutine_threadsafe(before(), loop).result(10)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(10)
        if not thread.is_alive():
            loop.close()


class PostgresTriggerStore:
    """Durable, multi-host trigger store (the contract of ``SqliteTriggerStore``).

    ``dsn`` names the database (a pool of up to ``max_pool_size`` connections is opened on
    first use and closed by ``close()``; using the store again reopens it). Webhook signing
    secrets are encrypted with the key from ``NEXURA_TRIGGER_SECRET_KEY`` or, when that is
    unset, from ``secret_key_file`` (created with mode 0600 when absent). Every process that
    shares the database must use the same key: on several hosts set
    ``NEXURA_TRIGGER_SECRET_KEY``. Without either, webhook secrets cannot be stored or read
    (schedules and jobs still work).

    ``call_timeout``: seconds a call waits for the database before ``TimeoutError``.
    """

    def __init__(
        self,
        dsn: str,
        *,
        secret_key_file: str | os.PathLike[str] | None = None,
        max_pool_size: int = 4,
        call_timeout: float = 60.0,
    ) -> None:
        if not isinstance(dsn, str) or not dsn:
            raise ValueError("Provide a Postgres DSN")
        if isinstance(max_pool_size, bool) or not isinstance(max_pool_size, int):
            raise ValueError("max_pool_size must be an integer >= 1")
        if max_pool_size < 1:
            raise ValueError("max_pool_size must be an integer >= 1")
        if not call_timeout > 0:
            raise ValueError("call_timeout must be a positive number of seconds")
        self._dsn = dsn
        self._secret_key_file = secret_key_file
        self._secrets: SecretBox | None = None
        self._max_pool_size = max_pool_size
        self._call_timeout = float(call_timeout)
        self._loop = _LoopThread("nexura-postgres-triggers")
        self._pool: Any = None
        self._pool_lock: asyncio.Lock | None = None
        self._ready = False

    # ------------------------------------------------------------------ plumbing

    def _call(self, operation: Callable[[Any], Awaitable[_T]]) -> _T:
        """Run ``operation(conn)`` in one transaction on the store's loop and wait for it."""

        async def go() -> _T:
            pool = await self._get_pool()
            async with pool.acquire() as conn, conn.transaction():
                return await operation(conn)

        return self._loop.run(go(), self._call_timeout)

    async def _get_pool(self) -> Any:
        if self._pool is not None and self._ready:
            return self._pool
        if self._pool_lock is None:  # created on the store's loop (one loop at a time)
            self._pool_lock = asyncio.Lock()
        async with self._pool_lock:
            if self._pool is None:
                import asyncpg  # type: ignore[import-untyped]

                self._pool = await asyncpg.create_pool(
                    normalize_url(self._dsn, strict=False),
                    min_size=1,
                    max_size=self._max_pool_size,
                    statement_cache_size=0,
                    reset=noop_reset,
                )
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
            """CREATE TABLE IF NOT EXISTS nexura_trigger_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at DOUBLE PRECISION NOT NULL)"""
        )
        applied = {
            int(row["version"])
            for row in await conn.fetch("SELECT version FROM nexura_trigger_migrations")
        }
        known = {version for version, _, _ in MIGRATIONS}
        unknown = sorted(applied - known)
        if unknown:
            raise TriggerError(
                f"The trigger tables were migrated to version {unknown[-1]} by a newer "
                "nexura-postgres; upgrade this package to use them."
            )
        for version, name, statements in MIGRATIONS:
            if version in applied:
                continue
            for statement in statements:
                await conn.execute(statement)
            await conn.execute(
                "INSERT INTO nexura_trigger_migrations (version, name, applied_at) "
                "VALUES ($1, $2, EXTRACT(EPOCH FROM clock_timestamp()))",
                version,
                name,
            )

    @staticmethod
    async def _clock(conn: Any, now: float | None) -> float:
        """``now`` when the caller gives one, else the database clock."""
        if now is not None:
            return float(now)
        value: float = await conn.fetchval(_NOW)
        return value

    def _box(self) -> SecretBox:
        if self._secrets is None:
            if not os.environ.get(ENV_KEY) and self._secret_key_file is None:
                raise TriggerError(
                    "PostgresTriggerStore needs a key to encrypt webhook secrets: set "
                    f"{ENV_KEY}, or pass secret_key_file."
                )
            self._secrets = SecretBox("", key_file=self._secret_key_file)
        return self._secrets

    def init_db(self) -> None:
        """Apply the trigger tables' migrations (otherwise done on first use)."""

        async def noop(conn: Any) -> None:
            return None

        self._call(noop)

    def close(self) -> None:
        """Close the pool and stop the store's loop thread (reopened on next use)."""

        async def close_pool() -> None:
            pool, self._pool = self._pool, None
            if pool is not None:
                await pool.close()

        self._loop.stop(close_pool)
        self._pool = None
        self._pool_lock = None
        self._ready = False

    # ------------------------------------------------------------------ webhooks

    def _webhook(self, row: Any) -> WebhookConfig:
        return WebhookConfig(
            endpoint_id=row["endpoint_id"],
            workspace_id=row["workspace_id"],
            workflow_id=row["workflow_id"],
            release_id=row["release_id"],
            secret=self._box().decrypt(row["secret"]),
            replay_window_seconds=row["replay_window_seconds"],
            is_active=bool(row["is_active"]),
            allow_legacy_signature=bool(row["allow_legacy_signature"]),
        )

    def save_webhook_config(self, config: WebhookConfig) -> None:
        secret = self._box().encrypt(config.secret)

        async def op(conn: Any) -> str:
            result: str = await conn.execute(
                """INSERT INTO nexura_trigger_webhooks
                   (endpoint_id, workspace_id, workflow_id, release_id, secret,
                    replay_window_seconds, is_active, allow_legacy_signature)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                   ON CONFLICT (endpoint_id) DO UPDATE SET
                       workflow_id = EXCLUDED.workflow_id,
                       release_id = EXCLUDED.release_id,
                       secret = EXCLUDED.secret,
                       replay_window_seconds = EXCLUDED.replay_window_seconds,
                       is_active = EXCLUDED.is_active,
                       allow_legacy_signature = EXCLUDED.allow_legacy_signature
                   WHERE nexura_trigger_webhooks.workspace_id = EXCLUDED.workspace_id""",
                config.endpoint_id,
                config.workspace_id,
                config.workflow_id,
                config.release_id,
                secret,
                config.replay_window_seconds,
                config.is_active,
                config.allow_legacy_signature,
            )
            return result

        if _affected(self._call(op)) == 0:  # id owned by another workspace: never take it over
            raise TriggerConflictError("Webhook endpoint", config.endpoint_id)

    def get_webhook_config(self, endpoint_id: str) -> WebhookConfig | None:
        async def op(conn: Any) -> Any:
            return await conn.fetchrow(
                "SELECT * FROM nexura_trigger_webhooks WHERE endpoint_id = $1", endpoint_id
            )

        row = self._call(op)
        return self._webhook(row) if row is not None else None

    def list_webhook_configs(self, workspace_id: str | None = None) -> list[WebhookConfig]:
        async def op(conn: Any) -> list[Any]:
            if workspace_id is None:
                rows: list[Any] = await conn.fetch(
                    "SELECT * FROM nexura_trigger_webhooks ORDER BY endpoint_id"
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM nexura_trigger_webhooks WHERE workspace_id = $1 "
                    "ORDER BY endpoint_id",
                    workspace_id,
                )
            return rows

        return [self._webhook(row) for row in self._call(op)]

    def delete_webhook_config(self, endpoint_id: str) -> bool:
        async def op(conn: Any) -> str:
            result: str = await conn.execute(
                "DELETE FROM nexura_trigger_webhooks WHERE endpoint_id = $1", endpoint_id
            )
            return result

        return _affected(self._call(op)) > 0

    def record_webhook_receipt_and_enqueue_job(
        self,
        receipt: WebhookReceipt,
        job: DurableTriggerJob,
    ) -> tuple[bool, DurableTriggerJob]:
        async def op(conn: Any) -> tuple[bool, Any]:
            inserted = await conn.fetchval(
                """INSERT INTO nexura_trigger_receipts
                   (endpoint_id, event_id, workspace_id, job_id, received_at)
                   VALUES ($1, $2, $3, $4, $5)
                   ON CONFLICT (endpoint_id, event_id) DO NOTHING
                   RETURNING job_id""",
                receipt.endpoint_id,
                receipt.event_id,
                receipt.workspace_id,
                receipt.job_id,
                receipt.received_at,
            )
            if inserted is None:
                existing = await conn.fetchrow(
                    """SELECT j.* FROM nexura_trigger_receipts r
                       JOIN nexura_trigger_jobs j ON j.job_id = r.job_id
                       WHERE r.endpoint_id = $1 AND r.event_id = $2""",
                    receipt.endpoint_id,
                    receipt.event_id,
                )
                if existing is None:
                    raise TriggerError(
                        f"Webhook receipt '{receipt.event_id}' has no job; the store is damaged."
                    )
                return False, existing
            await conn.execute(_JOB_INSERT, *_job_values(job))
            return True, None

        is_new, row = self._call(op)
        if is_new:
            return True, job
        return False, _row_to_job(row)

    # ------------------------------------------------------------------ schedules

    def save_schedule(self, schedule: ScheduleConfig) -> None:
        async def op(conn: Any) -> str:
            result: str = await conn.execute(
                """INSERT INTO nexura_trigger_schedules
                   (schedule_id, workspace_id, workflow_id, release_id, cron_expr,
                    interval_seconds, timezone, missed_run_policy, next_run_at, last_run_at,
                    is_active, payload_json)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                   ON CONFLICT (schedule_id) DO UPDATE SET
                       workflow_id = EXCLUDED.workflow_id,
                       release_id = EXCLUDED.release_id,
                       cron_expr = EXCLUDED.cron_expr,
                       interval_seconds = EXCLUDED.interval_seconds,
                       timezone = EXCLUDED.timezone,
                       missed_run_policy = EXCLUDED.missed_run_policy,
                       next_run_at = EXCLUDED.next_run_at,
                       last_run_at = EXCLUDED.last_run_at,
                       is_active = EXCLUDED.is_active,
                       payload_json = EXCLUDED.payload_json
                   WHERE nexura_trigger_schedules.workspace_id = EXCLUDED.workspace_id""",
                schedule.schedule_id,
                schedule.workspace_id,
                schedule.workflow_id,
                schedule.release_id,
                schedule.cron_expr,
                schedule.interval_seconds,
                schedule.timezone,
                schedule.missed_run_policy,
                schedule.next_run_at,
                schedule.last_run_at,
                schedule.is_active,
                json.dumps(schedule.payload),
            )
            return result

        if _affected(self._call(op)) == 0:  # id owned by another workspace: never take it over
            raise TriggerConflictError("Schedule", schedule.schedule_id)

    def get_schedule(self, schedule_id: str) -> ScheduleConfig | None:
        async def op(conn: Any) -> Any:
            return await conn.fetchrow(
                "SELECT * FROM nexura_trigger_schedules WHERE schedule_id = $1", schedule_id
            )

        row = self._call(op)
        return _row_to_schedule(row) if row is not None else None

    def list_schedules(self, workspace_id: str | None = None) -> list[ScheduleConfig]:
        async def op(conn: Any) -> list[Any]:
            if workspace_id is None:
                rows: list[Any] = await conn.fetch(
                    "SELECT * FROM nexura_trigger_schedules ORDER BY schedule_id"
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM nexura_trigger_schedules WHERE workspace_id = $1 "
                    "ORDER BY schedule_id",
                    workspace_id,
                )
            return rows

        return [_row_to_schedule(row) for row in self._call(op)]

    def delete_schedule(self, schedule_id: str) -> bool:
        async def op(conn: Any) -> str:
            result: str = await conn.execute(
                "DELETE FROM nexura_trigger_schedules WHERE schedule_id = $1", schedule_id
            )
            return result

        return _affected(self._call(op)) > 0

    def update_schedule_next_fire(
        self,
        schedule_id: str,
        next_run_at: float,
        last_run_at: float | None = None,
    ) -> None:
        async def op(conn: Any) -> None:
            await conn.execute(
                """UPDATE nexura_trigger_schedules
                   SET next_run_at = $2, last_run_at = COALESCE($3, last_run_at)
                   WHERE schedule_id = $1""",
                schedule_id,
                next_run_at,
                last_run_at,
            )

        self._call(op)

    def advance_schedule(
        self,
        schedule_id: str,
        expected_next_run_at: float,
        next_run_at: float,
        last_run_at: float | None = None,
    ) -> bool:
        """Compare-and-swap ``next_run_at``; True only for the caller that won the advance."""

        async def op(conn: Any) -> str:
            result: str = await conn.execute(
                """UPDATE nexura_trigger_schedules
                   SET next_run_at = $3, last_run_at = COALESCE($4, last_run_at)
                   WHERE schedule_id = $1 AND next_run_at = $2""",
                schedule_id,
                expected_next_run_at,
                next_run_at,
                last_run_at,
            )
            return result

        return _affected(self._call(op)) == 1

    # ------------------------------------------------------------------ jobs

    def enqueue_trigger_job(self, job: DurableTriggerJob) -> DurableTriggerJob:
        async def op(conn: Any) -> None:
            await conn.execute(
                _JOB_INSERT
                + """ ON CONFLICT (job_id) DO UPDATE SET
                       status = EXCLUDED.status,
                       payload_json = EXCLUDED.payload_json""",
                *_job_values(job),
            )

        self._call(op)
        return job

    def claim_pending_job(
        self,
        job_id: str,
        worker_id: str,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        now: float | None = None,
    ) -> DurableTriggerJob | None:
        async def op(conn: Any) -> Any:
            current = await self._clock(conn, now)
            return await conn.fetchrow(
                """UPDATE nexura_trigger_jobs
                   SET status = 'claimed', claimed_by = $2, claimed_at = $3::float8,
                       lease_expires_at = $3::float8 + $4::float8, attempts = attempts + 1
                   WHERE job_id = $1 AND status = 'pending'
                   RETURNING *""",
                job_id,
                worker_id,
                current,
                float(lease_seconds),
            )

        row = self._call(op)
        return _row_to_job(row) if row is not None else None

    def renew_job_lease(
        self,
        job_id: str,
        worker_id: str,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        now: float | None = None,
    ) -> bool:
        async def op(conn: Any) -> str:
            current = await self._clock(conn, now)
            result: str = await conn.execute(
                """UPDATE nexura_trigger_jobs SET lease_expires_at = $3::float8 + $4::float8
                   WHERE job_id = $1 AND status = 'claimed' AND claimed_by = $2""",
                job_id,
                worker_id,
                current,
                float(lease_seconds),
            )
            return result

        return _affected(self._call(op)) == 1

    def set_job_run(self, job_id: str, worker_id: str, run_id: str) -> bool:
        async def op(conn: Any) -> bool:
            row = await conn.fetchrow(
                """SELECT run_id, previous_run_ids_json FROM nexura_trigger_jobs
                   WHERE job_id = $1 AND status = 'claimed' AND claimed_by = $2
                   FOR UPDATE""",
                job_id,
                worker_id,
            )
            if row is None:
                return False
            previous = json.loads(row["previous_run_ids_json"] or "[]")
            if row["run_id"] and row["run_id"] != run_id:
                previous.append(row["run_id"])
            await conn.execute(
                """UPDATE nexura_trigger_jobs SET run_id = $2, previous_run_ids_json = $3
                   WHERE job_id = $1""",
                job_id,
                run_id,
                json.dumps(previous),
            )
            return True

        return self._call(op)

    def reclaim_expired_jobs(
        self,
        worker_id: str,
        *,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        limit: int = 20,
        now: float | None = None,
        exclude_job_ids: Collection[str] = (),
        keep_run: bool = True,
    ) -> list[DurableTriggerJob]:
        """Take over claimed jobs whose lease expired (see ``TriggerStore``).

        The expired rows are locked ``FOR UPDATE SKIP LOCKED``: a row another reclaimer (or
        its owner renewing it) holds is skipped, so each job has exactly one new owner.
        """
        require_keep_run(keep_run)
        excluded = sorted(exclude_job_ids)

        async def op(conn: Any) -> list[Any]:
            current = await self._clock(conn, now)
            rows = await conn.fetch(
                """SELECT * FROM nexura_trigger_jobs
                   WHERE status = 'claimed'
                     AND COALESCE(lease_expires_at, COALESCE(claimed_at, 0) + $1::float8)
                         <= $2::float8
                     AND NOT (job_id = ANY($3::text[]))
                   ORDER BY claimed_at ASC NULLS FIRST, created_at ASC
                   FOR UPDATE SKIP LOCKED""",
                float(lease_seconds),
                current,
                excluded,
            )
            won: list[Any] = []
            retried = 0
            for row in rows:
                job = _row_to_job(row)
                if job.attempts >= max_attempts:
                    updated = await conn.fetchrow(
                        """UPDATE nexura_trigger_jobs
                           SET status = 'failed', completed_at = $2, lease_expires_at = NULL,
                               error_message = $3
                           WHERE job_id = $1 RETURNING *""",
                        job.job_id,
                        current,
                        lease_expired_message(job.attempts),
                    )
                elif retried >= limit:
                    continue
                else:
                    previous = list(job.previous_run_ids)
                    run_id = job.run_id
                    updated = await conn.fetchrow(
                        """UPDATE nexura_trigger_jobs
                           SET claimed_by = $2, claimed_at = $3::float8,
                               lease_expires_at = $3::float8 + $4::float8,
                               attempts = attempts + 1, run_id = $5,
                               previous_run_ids_json = $6
                           WHERE job_id = $1 RETURNING *""",
                        job.job_id,
                        worker_id,
                        current,
                        float(lease_seconds),
                        run_id,
                        json.dumps(previous),
                    )
                    retried += 1
                won.append(updated)
            return won

        return [_row_to_job(row) for row in self._call(op)]

    def list_pending_jobs(
        self, workspace_id: str | None = None, limit: int = 50
    ) -> list[DurableTriggerJob]:
        async def op(conn: Any) -> list[Any]:
            if workspace_id is None:
                rows: list[Any] = await conn.fetch(
                    """SELECT * FROM nexura_trigger_jobs WHERE status = 'pending'
                       ORDER BY created_at ASC LIMIT $1""",
                    limit,
                )
            else:
                rows = await conn.fetch(
                    """SELECT * FROM nexura_trigger_jobs
                       WHERE status = 'pending' AND workspace_id = $1
                       ORDER BY created_at ASC LIMIT $2""",
                    workspace_id,
                    limit,
                )
            return rows

        return [_row_to_job(row) for row in self._call(op)]

    def get_job(self, job_id: str) -> DurableTriggerJob | None:
        async def op(conn: Any) -> Any:
            return await conn.fetchrow(
                "SELECT * FROM nexura_trigger_jobs WHERE job_id = $1", job_id
            )

        row = self._call(op)
        return _row_to_job(row) if row is not None else None

    def complete_job(
        self,
        job_id: str,
        error_message: str | None = None,
        run_id: str | None = None,
        expected_worker: str | None = None,
    ) -> DurableTriggerJob | None:
        """Finish a job; with ``expected_worker`` it is a no-op unless that worker still owns it."""
        status = "failed" if error_message else "completed"

        async def op(conn: Any) -> Any:
            current = await self._clock(conn, None)
            guard = ""
            params: list[Any] = [job_id, status, current, error_message, run_id]
            if expected_worker is not None:
                guard = " AND status = 'claimed' AND claimed_by = $6"
                params.append(expected_worker)
            await conn.execute(
                f"""UPDATE nexura_trigger_jobs
                    SET status = $2, completed_at = $3, error_message = $4,
                        run_id = COALESCE($5, run_id), lease_expires_at = NULL
                    WHERE job_id = $1{guard}""",
                *params,
            )
            return await conn.fetchrow(
                "SELECT * FROM nexura_trigger_jobs WHERE job_id = $1", job_id
            )

        row = self._call(op)
        return _row_to_job(row) if row is not None else None

    # ------------------------------------------------------------------ leases

    def acquire_lease(
        self, name: str, holder: str, *, ttl_seconds: float, now: float | None = None
    ) -> bool:
        """Take the named lease unless another holder has it unexpired (database clock).

        One statement: insert, or take over a row whose lease expired. Concurrent callers on
        any instance conflict on the primary key; Postgres re-checks the ``WHERE`` on the
        locked, latest row, so exactly one of them gets a row back.
        """
        ttl = float(ttl_seconds)
        if not ttl > 0 or ttl == float("inf"):
            raise ValueError("ttl_seconds must be a positive finite number")

        async def op(conn: Any) -> Any:
            current = await self._clock(conn, now)
            return await conn.fetchrow(
                """INSERT INTO nexura_trigger_leases (name, holder, expires_at)
                   VALUES ($1, $2, $3::float8 + $4::float8)
                   ON CONFLICT (name) DO UPDATE SET
                       holder = EXCLUDED.holder, expires_at = EXCLUDED.expires_at
                   WHERE nexura_trigger_leases.expires_at <= $3::float8
                   RETURNING holder""",
                name,
                holder,
                current,
                ttl,
            )

        return self._call(op) is not None

    def release_lease(self, name: str, holder: str) -> bool:
        """Drop the named lease if ``holder`` still holds it."""

        async def op(conn: Any) -> str:
            result: str = await conn.execute(
                "DELETE FROM nexura_trigger_leases WHERE name = $1 AND holder = $2", name, holder
            )
            return result

        return _affected(self._call(op)) == 1
