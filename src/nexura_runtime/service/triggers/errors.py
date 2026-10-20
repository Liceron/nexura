# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Typed error hierarchy for webhooks, schedules, and durable triggers."""

from __future__ import annotations

from nexura_agents.errors import NexuraError


class TriggerError(NexuraError):
    """Base exception for all webhook, schedule, and trigger errors."""

    def __init__(self, message: str, code: str = "TRIGGER_ERROR") -> None:
        super().__init__(message)
        self.code = code


class InvalidWebhookSignatureError(TriggerError):
    """Raised when webhook signature verification fails."""

    def __init__(self, message: str = "Invalid webhook cryptographic signature.") -> None:
        super().__init__(message, code="INVALID_WEBHOOK_SIGNATURE")


class ReplayWindowExceededError(TriggerError):
    """Raised when webhook timestamp falls outside the acceptable replay window."""

    def __init__(
        self,
        message: str = "Webhook timestamp exceeds acceptable replay window.",
    ) -> None:
        super().__init__(message, code="REPLAY_WINDOW_EXCEEDED")


class DuplicateWebhookError(TriggerError):
    """Raised when an identical webhook event has already been delivered and processed."""

    def __init__(self, event_id: str) -> None:
        super().__init__(
            f"Webhook event '{event_id}' has already been processed.",
            code="DUPLICATE_WEBHOOK_DELIVERY",
        )
        self.event_id = event_id


class ScheduleNotFoundError(TriggerError):
    """Raised when a schedule definition is not found."""

    def __init__(self, schedule_id: str) -> None:
        super().__init__(f"Schedule '{schedule_id}' not found.", code="SCHEDULE_NOT_FOUND")
        self.schedule_id = schedule_id


class InvalidScheduleError(TriggerError):
    """Raised when a schedule expression or timezone is invalid."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="INVALID_SCHEDULE")


class JobClaimConflictError(TriggerError):
    """Raised when attempting to claim a job that has already been claimed by another worker."""

    def __init__(self, job_id: str) -> None:
        super().__init__(
            f"Job '{job_id}' is already claimed or no longer pending.",
            code="JOB_CLAIM_CONFLICT",
        )
        self.job_id = job_id


class WorkflowReleaseNotFoundError(TriggerError):
    """Raised when the pinned workflow release for a trigger does not exist."""

    def __init__(self, workflow_id: str, release_id: str) -> None:
        super().__init__(
            f"Workflow release '{release_id}' for workflow '{workflow_id}' not found.",
            code="WORKFLOW_RELEASE_NOT_FOUND",
        )
        self.workflow_id = workflow_id
        self.release_id = release_id


class TriggerConflictError(TriggerError):
    """Raised when an endpoint or schedule id is already owned by another workspace."""

    def __init__(self, kind: str, trigger_id: str) -> None:
        super().__init__(
            f"{kind} id '{trigger_id}' is already in use by another workspace.",
            code="TRIGGER_ID_CONFLICT",
        )
        self.kind = kind
        self.trigger_id = trigger_id
