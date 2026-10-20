# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Authoritative persistent and in-memory stores for webhooks, schedules, and jobs."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
from collections.abc import Collection
from typing import Protocol

from nexura_runtime.service.triggers.errors import TriggerConflictError
from nexura_runtime.service.triggers.models import (
    DurableTriggerJob,
    ScheduleConfig,
    WebhookConfig,
    WebhookReceipt,
)
from nexura_runtime.service.triggers.secretbox import SecretBox

DEFAULT_LEASE_SECONDS = 300.0
DEFAULT_MAX_ATTEMPTS = 3


def lease_expired_message(attempts: int) -> str:
    """Error recorded on a job whose lease expired on every allowed attempt."""
    return f"worker lease expired {attempts} times"


class TriggerStore(Protocol):
    """Authoritative storage contract for webhook receipts, schedules, and trigger jobs."""

    def save_webhook_config(self, config: WebhookConfig) -> None: ...

    def get_webhook_config(self, endpoint_id: str) -> WebhookConfig | None: ...

    def list_webhook_configs(self, workspace_id: str | None = None) -> list[WebhookConfig]: ...

    def delete_webhook_config(self, endpoint_id: str) -> bool: ...

    def record_webhook_receipt_and_enqueue_job(
        self,
        receipt: WebhookReceipt,
        job: DurableTriggerJob,
    ) -> tuple[bool, DurableTriggerJob]:
        """Atomically record receipt and enqueue job. Returns (is_new, job)."""
        ...

    def save_schedule(self, schedule: ScheduleConfig) -> None: ...

    def get_schedule(self, schedule_id: str) -> ScheduleConfig | None: ...

    def list_schedules(self, workspace_id: str | None = None) -> list[ScheduleConfig]: ...

    def delete_schedule(self, schedule_id: str) -> bool: ...

    def update_schedule_next_fire(
        self,
        schedule_id: str,
        next_run_at: float,
        last_run_at: float | None = None,
    ) -> None: ...

    def advance_schedule(
        self,
        schedule_id: str,
        expected_next_run_at: float,
        next_run_at: float,
        last_run_at: float | None = None,
    ) -> bool: ...

    def enqueue_trigger_job(self, job: DurableTriggerJob) -> DurableTriggerJob: ...

    def claim_pending_job(
        self,
        job_id: str,
        worker_id: str,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        now: float | None = None,
    ) -> DurableTriggerJob | None:
        """Atomically claim a pending job with a lease and count the attempt."""
        ...

    def renew_job_lease(
        self,
        job_id: str,
        worker_id: str,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        now: float | None = None,
    ) -> bool:
        """Extend the lease; False when ``worker_id`` no longer owns the claimed job."""
        ...

    def set_job_run(self, job_id: str, worker_id: str, run_id: str) -> bool:
        """Record the latest run for a claimed job; False when ``worker_id`` lost ownership."""
        ...

    def reclaim_expired_jobs(
        self,
        worker_id: str,
        *,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        limit: int = 20,
        now: float | None = None,
        exclude_job_ids: Collection[str] = (),
    ) -> list[DurableTriggerJob]:
        """Atomically take over claimed jobs whose lease expired.

        Jobs with attempts left are re-claimed by ``worker_id`` (status ``claimed``, the previous
        run id moved to ``previous_run_ids``); at most ``limit`` are retried per call. Jobs out
        of attempts become ``failed``. Only the caller whose compare-and-swap wins gets a job.
        """
        ...

    def list_pending_jobs(
        self, workspace_id: str | None = None, limit: int = 50
    ) -> list[DurableTriggerJob]: ...

    def get_job(self, job_id: str) -> DurableTriggerJob | None: ...

    def complete_job(
        self,
        job_id: str,
        error_message: str | None = None,
        run_id: str | None = None,
        expected_worker: str | None = None,
    ) -> DurableTriggerJob | None:
        """Finish a job; with ``expected_worker`` it is a no-op unless that worker still owns it."""
        ...


def _lease_end(job: DurableTriggerJob, default_lease: float) -> float:
    """Lease expiry; jobs claimed before leases existed expire ``default_lease`` after claim."""
    if job.lease_expires_at is not None:
        return job.lease_expires_at
    return (job.claimed_at or 0.0) + default_lease


def _row_to_job(row: sqlite3.Row) -> DurableTriggerJob:
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


_JOB_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("run_id", "run_id TEXT"),
    ("lease_expires_at", "lease_expires_at REAL"),
    ("attempts", "attempts INTEGER NOT NULL DEFAULT 0"),
    ("previous_run_ids_json", "previous_run_ids_json TEXT"),
)


class MemoryTriggerStore:
    """Thread-safe in-memory trigger store with atomic CAS claiming."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._webhooks: dict[str, WebhookConfig] = {}
        # (endpoint_id, event_id) -> WebhookReceipt
        self._receipts: dict[tuple[str, str], WebhookReceipt] = {}
        self._schedules: dict[str, ScheduleConfig] = {}
        self._jobs: dict[str, DurableTriggerJob] = {}

    def save_webhook_config(self, config: WebhookConfig) -> None:
        with self._lock:
            existing = self._webhooks.get(config.endpoint_id)
            if existing is not None and existing.workspace_id != config.workspace_id:
                raise TriggerConflictError("Webhook endpoint", config.endpoint_id)
            self._webhooks[config.endpoint_id] = config.model_copy(deep=True)

    def get_webhook_config(self, endpoint_id: str) -> WebhookConfig | None:
        with self._lock:
            cfg = self._webhooks.get(endpoint_id)
            return cfg.model_copy(deep=True) if cfg else None

    def list_webhook_configs(self, workspace_id: str | None = None) -> list[WebhookConfig]:
        with self._lock:
            if workspace_id is None:
                return [cfg.model_copy(deep=True) for cfg in self._webhooks.values()]
            return [
                cfg.model_copy(deep=True)
                for cfg in self._webhooks.values()
                if cfg.workspace_id == workspace_id
            ]

    def delete_webhook_config(self, endpoint_id: str) -> bool:
        with self._lock:
            return self._webhooks.pop(endpoint_id, None) is not None

    def record_webhook_receipt_and_enqueue_job(
        self,
        receipt: WebhookReceipt,
        job: DurableTriggerJob,
    ) -> tuple[bool, DurableTriggerJob]:
        with self._lock:
            key = (receipt.endpoint_id, receipt.event_id)
            if key in self._receipts:
                existing_receipt = self._receipts[key]
                existing_job = self._jobs[existing_receipt.job_id]
                return False, existing_job.model_copy(deep=True)

            self._receipts[key] = receipt.model_copy(deep=True)
            self._jobs[job.job_id] = job.model_copy(deep=True)
            return True, job.model_copy(deep=True)

    def save_schedule(self, schedule: ScheduleConfig) -> None:
        with self._lock:
            existing = self._schedules.get(schedule.schedule_id)
            if existing is not None and existing.workspace_id != schedule.workspace_id:
                raise TriggerConflictError("Schedule", schedule.schedule_id)
            self._schedules[schedule.schedule_id] = schedule.model_copy(deep=True)

    def get_schedule(self, schedule_id: str) -> ScheduleConfig | None:
        with self._lock:
            s = self._schedules.get(schedule_id)
            return s.model_copy(deep=True) if s else None

    def list_schedules(self, workspace_id: str | None = None) -> list[ScheduleConfig]:
        with self._lock:
            if workspace_id is None:
                return [s.model_copy(deep=True) for s in self._schedules.values()]
            return [
                s.model_copy(deep=True)
                for s in self._schedules.values()
                if s.workspace_id == workspace_id
            ]

    def delete_schedule(self, schedule_id: str) -> bool:
        with self._lock:
            return self._schedules.pop(schedule_id, None) is not None

    def update_schedule_next_fire(
        self,
        schedule_id: str,
        next_run_at: float,
        last_run_at: float | None = None,
    ) -> None:
        with self._lock:
            s = self._schedules.get(schedule_id)
            if s:
                s.next_run_at = next_run_at
                if last_run_at is not None:
                    s.last_run_at = last_run_at

    def advance_schedule(
        self,
        schedule_id: str,
        expected_next_run_at: float,
        next_run_at: float,
        last_run_at: float | None = None,
    ) -> bool:
        """Compare-and-swap ``next_run_at``; True only for the caller that won the advance."""
        with self._lock:
            s = self._schedules.get(schedule_id)
            if s is None or s.next_run_at != expected_next_run_at:
                return False
            s.next_run_at = next_run_at
            if last_run_at is not None:
                s.last_run_at = last_run_at
            return True

    def enqueue_trigger_job(self, job: DurableTriggerJob) -> DurableTriggerJob:
        with self._lock:
            self._jobs[job.job_id] = job.model_copy(deep=True)
            return job.model_copy(deep=True)

    def claim_pending_job(
        self,
        job_id: str,
        worker_id: str,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        now: float | None = None,
    ) -> DurableTriggerJob | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status != "pending":
                return None
            claimed_at = time.time() if now is None else now
            job.status = "claimed"
            job.claimed_by = worker_id
            job.claimed_at = claimed_at
            job.lease_expires_at = claimed_at + lease_seconds
            job.attempts += 1
            return job.model_copy(deep=True)

    def renew_job_lease(
        self,
        job_id: str,
        worker_id: str,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        now: float | None = None,
    ) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status != "claimed" or job.claimed_by != worker_id:
                return False
            job.lease_expires_at = (time.time() if now is None else now) + lease_seconds
            return True

    def set_job_run(self, job_id: str, worker_id: str, run_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status != "claimed" or job.claimed_by != worker_id:
                return False
            job.run_id = run_id
            return True

    def reclaim_expired_jobs(
        self,
        worker_id: str,
        *,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        limit: int = 20,
        now: float | None = None,
        exclude_job_ids: Collection[str] = (),
    ) -> list[DurableTriggerJob]:
        current = time.time() if now is None else now
        results: list[DurableTriggerJob] = []
        retried = 0
        with self._lock:
            candidates = sorted(
                (
                    j
                    for j in self._jobs.values()
                    if j.status == "claimed"
                    and j.job_id not in exclude_job_ids
                    and _lease_end(j, lease_seconds) <= current
                ),
                key=lambda j: (j.claimed_at or 0.0, j.created_at),
            )
            for job in candidates:
                if job.attempts >= max_attempts:
                    job.status = "failed"
                    job.completed_at = current
                    job.lease_expires_at = None
                    job.error_message = lease_expired_message(job.attempts)
                elif retried >= limit:
                    continue
                else:
                    retried += 1
                    if job.run_id:
                        job.previous_run_ids.append(job.run_id)
                        job.run_id = None
                    job.claimed_by = worker_id
                    job.claimed_at = current
                    job.lease_expires_at = current + lease_seconds
                    job.attempts += 1
                results.append(job.model_copy(deep=True))
        return results

    def list_pending_jobs(
        self, workspace_id: str | None = None, limit: int = 50
    ) -> list[DurableTriggerJob]:
        with self._lock:
            pending = [
                j.model_copy(deep=True)
                for j in self._jobs.values()
                if j.status == "pending"
                and (workspace_id is None or j.workspace_id == workspace_id)
            ]
            pending.sort(key=lambda x: x.created_at)
            return pending[:limit]

    def get_job(self, job_id: str) -> DurableTriggerJob | None:
        with self._lock:
            j = self._jobs.get(job_id)
            return j.model_copy(deep=True) if j else None

    def complete_job(
        self,
        job_id: str,
        error_message: str | None = None,
        run_id: str | None = None,
        expected_worker: str | None = None,
    ) -> DurableTriggerJob | None:
        with self._lock:
            j = self._jobs.get(job_id)
            if not j:
                return None
            if expected_worker is not None and (
                j.status != "claimed" or j.claimed_by != expected_worker
            ):
                return j.model_copy(deep=True)
            j.completed_at = time.time()
            j.lease_expires_at = None
            if run_id is not None or j.run_id is None:
                j.run_id = run_id
            if error_message:
                j.status = "failed"
                j.error_message = error_message
            else:
                j.status = "completed"
            return j.model_copy(deep=True)


class SqliteTriggerStore:
    """Persistent SQLite-backed trigger store surviving application restarts."""

    def __init__(self, db_path: str = ":memory:") -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._secrets = SecretBox(db_path)
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS webhook_configs (
                    endpoint_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    workflow_id TEXT NOT NULL,
                    release_id TEXT NOT NULL,
                    secret TEXT NOT NULL,
                    replay_window_seconds INTEGER NOT NULL,
                    is_active INTEGER NOT NULL,
                    allow_legacy_signature INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS webhook_receipts (
                    event_id TEXT NOT NULL,
                    endpoint_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    received_at REAL NOT NULL,
                    PRIMARY KEY (endpoint_id, event_id)
                );

                CREATE TABLE IF NOT EXISTS schedule_configs (
                    schedule_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    workflow_id TEXT NOT NULL,
                    release_id TEXT NOT NULL,
                    cron_expr TEXT,
                    interval_seconds REAL,
                    timezone TEXT NOT NULL,
                    missed_run_policy TEXT NOT NULL,
                    next_run_at REAL NOT NULL,
                    last_run_at REAL,
                    is_active INTEGER NOT NULL,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS durable_trigger_jobs (
                    job_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    trigger_type TEXT NOT NULL,
                    trigger_source_id TEXT NOT NULL,
                    workflow_id TEXT NOT NULL,
                    release_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    claimed_by TEXT,
                    claimed_at REAL,
                    created_at REAL NOT NULL,
                    completed_at REAL,
                    error_message TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_jobs_status
                    ON durable_trigger_jobs (status, created_at);
            """)
            # Safe migrations for databases created before jobs recorded their run and lease.
            columns = {
                row["name"] for row in self._conn.execute("PRAGMA table_info(durable_trigger_jobs)")
            }
            for column, ddl in _JOB_MIGRATIONS:
                if column not in columns:
                    # Suppress: another process may have migrated concurrently.
                    with contextlib.suppress(sqlite3.OperationalError):
                        self._conn.execute(f"ALTER TABLE durable_trigger_jobs ADD COLUMN {ddl}")
            self._migrate_webhooks()

    def _migrate_webhooks(self) -> None:
        """Upgrade older databases: legacy flag column, per-endpoint receipts, encrypted secrets."""
        cfg_cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(webhook_configs)")}
        if "allow_legacy_signature" not in cfg_cols:
            with contextlib.suppress(sqlite3.OperationalError):
                self._conn.execute(
                    "ALTER TABLE webhook_configs ADD COLUMN "
                    "allow_legacy_signature INTEGER NOT NULL DEFAULT 1"
                )
        pk_cols = {
            r["name"] for r in self._conn.execute("PRAGMA table_info(webhook_receipts)") if r["pk"]
        }
        if pk_cols == {"event_id"}:
            # Receipts used to be keyed by event_id alone (cross-tenant suppression).
            self._conn.executescript(
                """
                ALTER TABLE webhook_receipts RENAME TO webhook_receipts_old;
                CREATE TABLE webhook_receipts (
                    event_id TEXT NOT NULL,
                    endpoint_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    received_at REAL NOT NULL,
                    PRIMARY KEY (endpoint_id, event_id)
                );
                INSERT OR IGNORE INTO webhook_receipts
                    SELECT event_id, endpoint_id, workspace_id, job_id, received_at
                    FROM webhook_receipts_old;
                DROP TABLE webhook_receipts_old;
                """
            )
        for row in self._conn.execute("SELECT endpoint_id, secret FROM webhook_configs").fetchall():
            if not SecretBox.is_encrypted(row["secret"]):
                self._conn.execute(
                    "UPDATE webhook_configs SET secret = ? WHERE endpoint_id = ?",
                    (self._secrets.encrypt(row["secret"]), row["endpoint_id"]),
                )

    def save_webhook_config(self, config: WebhookConfig) -> None:
        with self._lock, self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO webhook_configs
                (endpoint_id, workspace_id, workflow_id, release_id,
                 secret, replay_window_seconds, is_active, allow_legacy_signature)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(endpoint_id) DO UPDATE SET
                    workflow_id = excluded.workflow_id,
                    release_id = excluded.release_id,
                    secret = excluded.secret,
                    replay_window_seconds = excluded.replay_window_seconds,
                    is_active = excluded.is_active,
                    allow_legacy_signature = excluded.allow_legacy_signature
                WHERE webhook_configs.workspace_id = excluded.workspace_id
                """,
                (
                    config.endpoint_id,
                    config.workspace_id,
                    config.workflow_id,
                    config.release_id,
                    self._secrets.encrypt(config.secret),
                    config.replay_window_seconds,
                    1 if config.is_active else 0,
                    1 if config.allow_legacy_signature else 0,
                ),
            )
            if cur.rowcount == 0:  # id owned by another workspace: never take it over
                raise TriggerConflictError("Webhook endpoint", config.endpoint_id)

    def get_webhook_config(self, endpoint_id: str) -> WebhookConfig | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM webhook_configs WHERE endpoint_id = ?", (endpoint_id,)
            )
            row = cur.fetchone()
            if not row:
                return None
            return WebhookConfig(
                endpoint_id=row["endpoint_id"],
                workspace_id=row["workspace_id"],
                workflow_id=row["workflow_id"],
                release_id=row["release_id"],
                secret=self._secrets.decrypt(row["secret"]),
                replay_window_seconds=row["replay_window_seconds"],
                is_active=bool(row["is_active"]),
                allow_legacy_signature=bool(row["allow_legacy_signature"]),
            )

    def list_webhook_configs(self, workspace_id: str | None = None) -> list[WebhookConfig]:
        with self._lock:
            if workspace_id is None:
                cur = self._conn.execute("SELECT * FROM webhook_configs ORDER BY endpoint_id ASC")
            else:
                cur = self._conn.execute(
                    "SELECT * FROM webhook_configs WHERE workspace_id = ? ORDER BY endpoint_id ASC",
                    (workspace_id,),
                )
            rows = cur.fetchall()
            return [
                WebhookConfig(
                    endpoint_id=r["endpoint_id"],
                    workspace_id=r["workspace_id"],
                    workflow_id=r["workflow_id"],
                    release_id=r["release_id"],
                    secret=self._secrets.decrypt(r["secret"]),
                    replay_window_seconds=r["replay_window_seconds"],
                    is_active=bool(r["is_active"]),
                    allow_legacy_signature=bool(r["allow_legacy_signature"]),
                )
                for r in rows
            ]

    def delete_webhook_config(self, endpoint_id: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM webhook_configs WHERE endpoint_id = ?", (endpoint_id,)
            )
            return cur.rowcount > 0

    def record_webhook_receipt_and_enqueue_job(
        self,
        receipt: WebhookReceipt,
        job: DurableTriggerJob,
    ) -> tuple[bool, DurableTriggerJob]:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "SELECT job_id FROM webhook_receipts WHERE endpoint_id = ? AND event_id = ?",
                (receipt.endpoint_id, receipt.event_id),
            )
            existing_row = cur.fetchone()
            if existing_row:
                existing_job = self.get_job(existing_row["job_id"])
                assert existing_job is not None
                return False, existing_job

            # Atomically insert receipt and job
            self._conn.execute(
                """
                INSERT INTO webhook_receipts
                (event_id, endpoint_id, workspace_id, job_id, received_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    receipt.event_id,
                    receipt.endpoint_id,
                    receipt.workspace_id,
                    receipt.job_id,
                    receipt.received_at,
                ),
            )
            self._conn.execute(
                """
                INSERT INTO durable_trigger_jobs
                (job_id, workspace_id, trigger_type, trigger_source_id, workflow_id,
                 release_id, payload_json, status, claimed_by, claimed_at,
                 created_at, completed_at, error_message)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
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
                ),
            )
            return True, job

    def save_schedule(self, schedule: ScheduleConfig) -> None:
        with self._lock, self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO schedule_configs
                (schedule_id, workspace_id, workflow_id, release_id, cron_expr, interval_seconds,
                 timezone, missed_run_policy, next_run_at, last_run_at, is_active, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(schedule_id) DO UPDATE SET
                    workflow_id = excluded.workflow_id,
                    release_id = excluded.release_id,
                    cron_expr = excluded.cron_expr,
                    interval_seconds = excluded.interval_seconds,
                    timezone = excluded.timezone,
                    missed_run_policy = excluded.missed_run_policy,
                    next_run_at = excluded.next_run_at,
                    last_run_at = excluded.last_run_at,
                    is_active = excluded.is_active,
                    payload_json = excluded.payload_json
                WHERE schedule_configs.workspace_id = excluded.workspace_id
                """,
                (
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
                    1 if schedule.is_active else 0,
                    json.dumps(schedule.payload),
                ),
            )
            if cur.rowcount == 0:  # id owned by another workspace: never take it over
                raise TriggerConflictError("Schedule", schedule.schedule_id)

    def get_schedule(self, schedule_id: str) -> ScheduleConfig | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM schedule_configs WHERE schedule_id = ?", (schedule_id,)
            )
            row = cur.fetchone()
            if not row:
                return None
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

    def list_schedules(self, workspace_id: str | None = None) -> list[ScheduleConfig]:
        with self._lock:
            if workspace_id is None:
                cur = self._conn.execute("SELECT * FROM schedule_configs")
            else:
                cur = self._conn.execute(
                    "SELECT * FROM schedule_configs WHERE workspace_id = ?", (workspace_id,)
                )
            results: list[ScheduleConfig] = []
            for row in cur.fetchall():
                results.append(
                    ScheduleConfig(
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
                )
            return results

    def delete_schedule(self, schedule_id: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM schedule_configs WHERE schedule_id = ?", (schedule_id,)
            )
            return cur.rowcount > 0

    def update_schedule_next_fire(
        self,
        schedule_id: str,
        next_run_at: float,
        last_run_at: float | None = None,
    ) -> None:
        with self._lock, self._conn:
            if last_run_at is not None:
                self._conn.execute(
                    """
                    UPDATE schedule_configs
                    SET next_run_at = ?, last_run_at = ?
                    WHERE schedule_id = ?
                    """,
                    (next_run_at, last_run_at, schedule_id),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE schedule_configs
                    SET next_run_at = ?
                    WHERE schedule_id = ?
                    """,
                    (next_run_at, schedule_id),
                )

    def advance_schedule(
        self,
        schedule_id: str,
        expected_next_run_at: float,
        next_run_at: float,
        last_run_at: float | None = None,
    ) -> bool:
        """Compare-and-swap ``next_run_at``; True only for the caller that won the advance."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                """
                UPDATE schedule_configs
                SET next_run_at = ?, last_run_at = COALESCE(?, last_run_at)
                WHERE schedule_id = ? AND next_run_at = ?
                """,
                (next_run_at, last_run_at, schedule_id, expected_next_run_at),
            )
            return cur.rowcount == 1

    def enqueue_trigger_job(self, job: DurableTriggerJob) -> DurableTriggerJob:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO durable_trigger_jobs
                (job_id, workspace_id, trigger_type, trigger_source_id, workflow_id,
                 release_id, payload_json, status, claimed_by, claimed_at,
                 created_at, completed_at, error_message)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    status = excluded.status,
                    payload_json = excluded.payload_json
                """,
                (
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
                ),
            )
            return job

    def claim_pending_job(
        self,
        job_id: str,
        worker_id: str,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        now: float | None = None,
    ) -> DurableTriggerJob | None:
        with self._lock, self._conn:
            claimed_at = time.time() if now is None else now
            # Atomic CAS: update only if status is still pending
            cur = self._conn.execute(
                """
                UPDATE durable_trigger_jobs
                SET status = 'claimed', claimed_by = ?, claimed_at = ?,
                    lease_expires_at = ?, attempts = attempts + 1
                WHERE job_id = ? AND status = 'pending'
                """,
                (worker_id, claimed_at, claimed_at + lease_seconds, job_id),
            )
            if cur.rowcount == 0:
                # Lost race or already claimed
                return None
            return self.get_job(job_id)

    def renew_job_lease(
        self,
        job_id: str,
        worker_id: str,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        now: float | None = None,
    ) -> bool:
        with self._lock, self._conn:
            current = time.time() if now is None else now
            cur = self._conn.execute(
                """
                UPDATE durable_trigger_jobs SET lease_expires_at = ?
                WHERE job_id = ? AND status = 'claimed' AND claimed_by = ?
                """,
                (current + lease_seconds, job_id, worker_id),
            )
            return cur.rowcount == 1

    def set_job_run(self, job_id: str, worker_id: str, run_id: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                """
                UPDATE durable_trigger_jobs SET run_id = ?
                WHERE job_id = ? AND status = 'claimed' AND claimed_by = ?
                """,
                (run_id, job_id, worker_id),
            )
            return cur.rowcount == 1

    def reclaim_expired_jobs(
        self,
        worker_id: str,
        *,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        limit: int = 20,
        now: float | None = None,
        exclude_job_ids: Collection[str] = (),
    ) -> list[DurableTriggerJob]:
        current = time.time() if now is None else now
        results: list[DurableTriggerJob] = []
        retried = 0
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM durable_trigger_jobs
                WHERE status = 'claimed'
                  AND COALESCE(lease_expires_at, COALESCE(claimed_at, 0) + ?) <= ?
                ORDER BY claimed_at ASC, created_at ASC
                """,
                (lease_seconds, current),
            ).fetchall()
            for row in rows:
                job = _row_to_job(row)
                if job.job_id in exclude_job_ids:
                    continue
                # Compare-and-swap on the exact claim we observed: if another worker reclaimed,
                # renewed or completed the job meanwhile, the WHERE clause matches nothing.
                guard = (
                    job.job_id,
                    job.claimed_by,
                    job.claimed_at,
                    job.lease_expires_at,
                    job.attempts,
                )
                if job.attempts >= max_attempts:
                    with self._conn:
                        cur = self._conn.execute(
                            """
                            UPDATE durable_trigger_jobs
                            SET status = 'failed', completed_at = ?, lease_expires_at = NULL,
                                error_message = ?
                            WHERE job_id = ? AND status = 'claimed' AND claimed_by IS ?
                              AND claimed_at IS ? AND lease_expires_at IS ? AND attempts = ?
                            """,
                            (current, lease_expired_message(job.attempts), *guard),
                        )
                elif retried >= limit:
                    continue
                else:
                    previous = list(job.previous_run_ids)
                    if job.run_id:
                        previous.append(job.run_id)
                    with self._conn:
                        cur = self._conn.execute(
                            """
                            UPDATE durable_trigger_jobs
                            SET claimed_by = ?, claimed_at = ?, lease_expires_at = ?,
                                attempts = attempts + 1, run_id = NULL,
                                previous_run_ids_json = ?
                            WHERE job_id = ? AND status = 'claimed' AND claimed_by IS ?
                              AND claimed_at IS ? AND lease_expires_at IS ? AND attempts = ?
                            """,
                            (
                                worker_id,
                                current,
                                current + lease_seconds,
                                json.dumps(previous),
                                *guard,
                            ),
                        )
                    if cur.rowcount == 1:
                        retried += 1
                if cur.rowcount == 1:
                    won = self.get_job(job.job_id)
                    if won is not None:
                        results.append(won)
        return results

    def list_pending_jobs(
        self, workspace_id: str | None = None, limit: int = 50
    ) -> list[DurableTriggerJob]:
        with self._lock:
            if workspace_id is None:
                cur = self._conn.execute(
                    """
                    SELECT * FROM durable_trigger_jobs
                    WHERE status = 'pending'
                    ORDER BY created_at ASC
                    LIMIT ?
                    """,
                    (limit,),
                )
            else:
                cur = self._conn.execute(
                    """
                    SELECT * FROM durable_trigger_jobs
                    WHERE status = 'pending' AND workspace_id = ?
                    ORDER BY created_at ASC
                    LIMIT ?
                    """,
                    (workspace_id, limit),
                )
            results: list[DurableTriggerJob] = []
            for row in cur.fetchall():
                results.append(_row_to_job(row))
            return results

    def get_job(self, job_id: str) -> DurableTriggerJob | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM durable_trigger_jobs WHERE job_id = ?", (job_id,)
            )
            row = cur.fetchone()
            if not row:
                return None
            return _row_to_job(row)

    def complete_job(
        self,
        job_id: str,
        error_message: str | None = None,
        run_id: str | None = None,
        expected_worker: str | None = None,
    ) -> DurableTriggerJob | None:
        with self._lock, self._conn:
            now = time.time()
            status = "failed" if error_message else "completed"
            owner_guard = ""
            params: tuple[object, ...] = (status, now, error_message, run_id, job_id)
            if expected_worker is not None:
                owner_guard = " AND status = 'claimed' AND claimed_by = ?"
                params = (*params, expected_worker)
            self._conn.execute(
                f"""
                UPDATE durable_trigger_jobs
                SET status = ?, completed_at = ?, error_message = ?,
                    run_id = COALESCE(?, run_id), lease_expires_at = NULL
                WHERE job_id = ?{owner_guard}
                """,
                params,
            )
            return self.get_job(job_id)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
