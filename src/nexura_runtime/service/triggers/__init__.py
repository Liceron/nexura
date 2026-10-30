# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Webhooks, schedules, and durable trigger services."""

from __future__ import annotations

from nexura_runtime.modules.registry import default_registry
from nexura_runtime.service.triggers.errors import (
    DuplicateWebhookError,
    InvalidScheduleError,
    InvalidWebhookSignatureError,
    JobClaimConflictError,
    ReplayWindowExceededError,
    ScheduleNotFoundError,
    TriggerError,
    WorkflowReleaseNotFoundError,
)
from nexura_runtime.service.triggers.manager import TriggerManager
from nexura_runtime.service.triggers.models import (
    DurableTriggerJob,
    JobStatus,
    MissedRunPolicy,
    ScheduleConfig,
    TriggerType,
    WebhookConfig,
    WebhookEvent,
    WebhookReceipt,
)
from nexura_runtime.service.triggers.module import TriggerServiceModule
from nexura_runtime.service.triggers.schedules import (
    calculate_next_cron_fire,
    calculate_next_fire,
    evaluate_missed_runs,
)
from nexura_runtime.service.triggers.signatures import (
    compute_webhook_signature,
    verify_webhook_signature,
)
from nexura_runtime.service.triggers.store import (
    MemoryTriggerStore,
    SqliteTriggerStore,
    TriggerStore,
)

# Register default TriggerServiceModule
_trigger_module = TriggerServiceModule()
if not default_registry.has(_trigger_module.manifest.module_id):
    default_registry.register(_trigger_module)

__all__ = [
    "DuplicateWebhookError",
    "DurableTriggerJob",
    "InvalidScheduleError",
    "InvalidWebhookSignatureError",
    "JobClaimConflictError",
    "JobStatus",
    "MemoryTriggerStore",
    "MissedRunPolicy",
    "ReplayWindowExceededError",
    "ScheduleConfig",
    "ScheduleNotFoundError",
    "SqliteTriggerStore",
    "TriggerError",
    "TriggerManager",
    "TriggerServiceModule",
    "TriggerStore",
    "TriggerType",
    "WebhookConfig",
    "WebhookEvent",
    "WebhookReceipt",
    "WorkflowReleaseNotFoundError",
    "calculate_next_cron_fire",
    "calculate_next_fire",
    "compute_webhook_signature",
    "evaluate_missed_runs",
    "verify_webhook_signature",
]
