# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Orchestrator for webhook delivery, schedule evaluation, and worker job claims."""

from __future__ import annotations

import secrets
import time
from typing import Any

from nexura_runtime.service.triggers.errors import (
    JobClaimConflictError,
    ScheduleNotFoundError,
    TriggerError,
)
from nexura_runtime.service.triggers.models import (
    DurableTriggerJob,
    MissedRunPolicy,
    ScheduleConfig,
    WebhookConfig,
    WebhookEvent,
    WebhookReceipt,
)
from nexura_runtime.service.triggers.schedules import (
    calculate_next_fire,
    evaluate_missed_runs,
    validate_interval,
)
from nexura_runtime.service.triggers.signatures import verify_webhook_signature
from nexura_runtime.service.triggers.store import MemoryTriggerStore, TriggerStore


class TriggerManager:
    """Manages webhooks, timezone-aware schedules, and durable trigger execution jobs."""

    def __init__(self, store: TriggerStore | None = None) -> None:
        self.store: TriggerStore = store or MemoryTriggerStore()

    def register_webhook(
        self,
        *,
        endpoint_id: str,
        workspace_id: str,
        workflow_id: str,
        release_id: str,
        secret: str,
        replay_window_seconds: int = 300,
        allow_legacy_signature: bool = True,
    ) -> WebhookConfig:
        """Register a new webhook endpoint pinned to an immutable workflow release."""
        cfg = WebhookConfig(
            endpoint_id=endpoint_id,
            workspace_id=workspace_id,
            workflow_id=workflow_id,
            release_id=release_id,
            secret=secret,
            replay_window_seconds=replay_window_seconds,
            is_active=True,
            allow_legacy_signature=allow_legacy_signature,
        )
        # Raises TriggerConflictError if the id belongs to another workspace.
        self.store.save_webhook_config(cfg)
        return cfg

    def handle_webhook(
        self,
        event: WebhookEvent,
        *,
        current_time: float | None = None,
        refuse_legacy: bool = False,
    ) -> tuple[bool, DurableTriggerJob]:
        """Verify webhook signature, enforce replay window, deduplicate, and enqueue job.

        ``refuse_legacy`` refuses v1 signatures even for an endpoint that allows them (remote
        single-operator mode does).

        Returns:
            Tuple of (is_new, job). If is_new is False, the webhook was a duplicate.
        """
        cfg = self.store.get_webhook_config(event.endpoint_id)
        if not cfg or not cfg.is_active:
            raise TriggerError(f"Webhook endpoint '{event.endpoint_id}' not found or inactive.")

        # 1. Cryptographic signature and replay window validation
        verify_webhook_signature(
            cfg.secret,
            event,
            replay_window_seconds=cfg.replay_window_seconds,
            current_time=current_time,
            allow_legacy=cfg.allow_legacy_signature and not refuse_legacy,
        )

        now = current_time if current_time is not None else time.time()
        job_id = f"job_wh_{secrets.token_hex(8)}"
        job = DurableTriggerJob(
            job_id=job_id,
            workspace_id=cfg.workspace_id,
            trigger_type="webhook",
            trigger_source_id=cfg.endpoint_id,
            workflow_id=cfg.workflow_id,
            release_id=cfg.release_id,
            payload=event.payload,
            status="pending",
            created_at=now,
        )
        receipt = WebhookReceipt(
            event_id=event.event_id,
            endpoint_id=cfg.endpoint_id,
            workspace_id=cfg.workspace_id,
            job_id=job_id,
            received_at=now,
        )

        # 2. Atomic receipt recording and job enqueuing (deduplicating duplicates)
        return self.store.record_webhook_receipt_and_enqueue_job(receipt, job)

    def register_schedule(
        self,
        *,
        schedule_id: str,
        workspace_id: str,
        workflow_id: str,
        release_id: str,
        cron_expr: str | None = None,
        interval_seconds: float | None = None,
        timezone: str = "UTC",
        missed_run_policy: MissedRunPolicy = "fire_once",
        payload: dict[str, Any] | None = None,
        start_time: float | None = None,
    ) -> ScheduleConfig:
        """Register a new recurring schedule pinned to an immutable workflow release."""
        if interval_seconds is not None:
            validate_interval(interval_seconds)
        now = start_time if start_time is not None else time.time()
        temp_cfg = ScheduleConfig(
            schedule_id=schedule_id,
            workspace_id=workspace_id,
            workflow_id=workflow_id,
            release_id=release_id,
            cron_expr=cron_expr,
            interval_seconds=interval_seconds,
            timezone=timezone,
            missed_run_policy=missed_run_policy,
            next_run_at=0.0,
            payload=payload or {},
        )
        next_fire = calculate_next_fire(now, temp_cfg)
        temp_cfg.next_run_at = next_fire

        self.store.save_schedule(temp_cfg)
        return temp_cfg

    def reconcile_missed_runs(
        self,
        current_time: float | None = None,
    ) -> list[DurableTriggerJob]:
        """Catch up on missed schedule triggers after application restart."""
        now = current_time if current_time is not None else time.time()
        schedules = self.store.list_schedules()
        enqueued_jobs: list[DurableTriggerJob] = []

        for sched in schedules:
            if not sched.is_active or sched.next_run_at >= now:
                continue

            catchup_times, new_next_fire = evaluate_missed_runs(sched, now)
            enqueued_jobs.extend(
                self._advance_and_enqueue(
                    sched,
                    catchup_times,
                    new_next_fire,
                    catchup_times[-1] if catchup_times else None,
                )
            )

        return enqueued_jobs

    def tick_schedules(
        self,
        current_time: float | None = None,
    ) -> list[DurableTriggerJob]:
        """Poll and trigger all due recurring schedules."""
        now = current_time if current_time is not None else time.time()
        schedules = self.store.list_schedules()
        enqueued_jobs: list[DurableTriggerJob] = []

        for sched in schedules:
            if not sched.is_active or sched.next_run_at > now:
                continue

            if sched.next_run_at < now:
                # Overdue (downtime or a long inline run): honour missed_run_policy and move
                # next_run_at past ``now`` so one overdue schedule cannot burst-fire per poll.
                times, next_fire = evaluate_missed_runs(sched, now)
                last = times[-1] if times else None
            else:
                times = [sched.next_run_at]
                next_fire = calculate_next_fire(sched.next_run_at, sched)
                last = now
            enqueued_jobs.extend(self._advance_and_enqueue(sched, times, next_fire, last))

        return enqueued_jobs

    def _advance_and_enqueue(
        self,
        sched: ScheduleConfig,
        fire_times: list[float],
        next_fire: float,
        last_run_at: float | None,
    ) -> list[DurableTriggerJob]:
        """Atomically advance ``next_run_at`` (compare-and-swap) and enqueue jobs if we won.

        Concurrent workers observing the same due schedule race on the swap; only the winner
        enqueues, so one due fire yields one job set. The swap happens first, so a crash between
        the swap and the enqueue drops that fire rather than duplicating it.
        """
        if not self.store.advance_schedule(
            sched.schedule_id, sched.next_run_at, next_fire, last_run_at
        ):
            return []
        jobs: list[DurableTriggerJob] = []
        for fire_time in fire_times:
            job = DurableTriggerJob(
                job_id=f"job_sched_{secrets.token_hex(8)}",
                workspace_id=sched.workspace_id,
                trigger_type="schedule",
                trigger_source_id=sched.schedule_id,
                workflow_id=sched.workflow_id,
                release_id=sched.release_id,
                payload=sched.payload,
                status="pending",
                created_at=fire_time,
            )
            self.store.enqueue_trigger_job(job)
            jobs.append(job)
        return jobs

    def claim_job(
        self, job_id: str, worker_id: str, lease_seconds: float = 300.0
    ) -> DurableTriggerJob:
        """Atomically claim a pending trigger job for a worker.

        Raises:
            JobClaimConflictError: If the job has already been claimed or completed.
        """
        claimed = self.store.claim_pending_job(job_id, worker_id, lease_seconds=lease_seconds)
        if not claimed:
            raise JobClaimConflictError(job_id)
        return claimed

    def get_schedule(self, schedule_id: str) -> ScheduleConfig:
        """Retrieve a schedule configuration."""
        sched = self.store.get_schedule(schedule_id)
        if not sched:
            raise ScheduleNotFoundError(schedule_id)
        return sched
