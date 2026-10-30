# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""B00/B01 module contract adapter for webhooks, schedules, and durable triggers."""

from __future__ import annotations

from typing import Any

from nexura_agents.contracts.base import BaseModule
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.manifest import EffectPolicy, ModuleManifest
from nexura_agents.contracts.result import ModuleResult

from nexura_runtime.service.triggers.errors import TriggerError
from nexura_runtime.service.triggers.manager import TriggerManager
from nexura_runtime.service.triggers.models import MissedRunPolicy, WebhookEvent


class TriggerServiceModule(BaseModule):
    """B00/B01 module wrapping webhook ingestion, schedule evaluation, and job claims."""

    def __init__(
        self,
        manager: TriggerManager | None = None,
        *,
        module_id: str = "nexura.service.triggers",
        version: str = "1.0.0",
        effect_policy: EffectPolicy = "idempotent_write",
    ) -> None:
        self.manager = manager or TriggerManager()

        self._manifest = ModuleManifest(
            schema_version=1,
            module_id=module_id,
            version=version,
            kind="service",
            internal=True,
            title="Webhooks, Schedules and Durable Triggers",
            description=(
                "Starts immutable workflow releases from signed webhook events "
                "and timezone-aware schedules through durable job queues."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": [
                            "register_webhook",
                            "handle_webhook",
                            "register_schedule",
                            "tick_schedules",
                            "reconcile_missed_runs",
                            "claim_job",
                        ],
                    },
                    "endpoint_id": {"type": "string"},
                    "workspace_id": {"type": "string"},
                    "workflow_id": {"type": "string"},
                    "release_id": {"type": "string"},
                    "secret": {"type": "string"},
                    "event_id": {"type": "string"},
                    "payload": {"type": "object"},
                    "timestamp": {"type": "number"},
                    "signature": {"type": "string"},
                    "schedule_id": {"type": "string"},
                    "cron_expr": {"type": "string"},
                    "interval_seconds": {"type": "number"},
                    "timezone": {"type": "string"},
                    "missed_run_policy": {
                        "type": "string",
                        "enum": ["skip", "fire_once", "fire_all"],
                    },
                    "job_id": {"type": "string"},
                    "worker_id": {"type": "string"},
                },
                "required": ["operation"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                    "job_id": {"type": "string"},
                    "enqueued_jobs": {"type": "array"},
                },
            },
            config_schema={"type": "object"},
            named_ports=[],
            capabilities={"streaming": False, "network": False},
            effect_policy=effect_policy,
            dependency_extra=None,
            min_sdk_version="0.3.0",
            timeout_seconds=30.0,
            output_limit_bytes=500_000,
            required_credential_types=[],
            supported_modalities=["text"],
            supported_locales=["all"],
            source_reference="service:triggers",
            verification_status="mock_verified",
        )

    @property
    def manifest(self) -> ModuleManifest:
        return self._manifest

    async def invoke(
        self,
        context: ExecutionContext,
        inputs: dict[str, Any],
        config: dict[str, Any] | None = None,
    ) -> ModuleResult:
        op = str(inputs.get("operation", ""))
        try:
            if op == "register_webhook":
                ep_id = str(inputs.get("endpoint_id", ""))
                ws_id = str(inputs.get("workspace_id") or context.workspace_id)
                wf_id = str(inputs.get("workflow_id", ""))
                rel_id = str(inputs.get("release_id", ""))
                secret = str(inputs.get("secret", ""))
                replay_win = int(inputs.get("replay_window_seconds", 300))
                cfg = self.manager.register_webhook(
                    endpoint_id=ep_id,
                    workspace_id=ws_id,
                    workflow_id=wf_id,
                    release_id=rel_id,
                    secret=secret,
                    replay_window_seconds=replay_win,
                )
                return ModuleResult.success(
                    outputs={
                        "status": "registered",
                        "endpoint_id": cfg.endpoint_id,
                        "workspace_id": cfg.workspace_id,
                    }
                )

            elif op == "handle_webhook":
                ep_id = str(inputs.get("endpoint_id", ""))
                evt_id = str(inputs.get("event_id", ""))
                payload = dict(inputs.get("payload") or {})
                ts = float(inputs.get("timestamp", 0.0))
                sig = str(inputs.get("signature", ""))
                event = WebhookEvent(
                    event_id=evt_id,
                    endpoint_id=ep_id,
                    payload=payload,
                    timestamp=ts,
                    signature=sig,
                )
                is_new, job = self.manager.handle_webhook(event)
                return ModuleResult.success(
                    outputs={
                        "status": "accepted" if is_new else "duplicate_accepted",
                        "is_new": is_new,
                        "job_id": job.job_id,
                        "workflow_id": job.workflow_id,
                        "release_id": job.release_id,
                    }
                )

            elif op == "register_schedule":
                sched_id = str(inputs.get("schedule_id", ""))
                ws_id = str(inputs.get("workspace_id") or context.workspace_id)
                wf_id = str(inputs.get("workflow_id", ""))
                rel_id = str(inputs.get("release_id", ""))
                cron = inputs.get("cron_expr")
                interval = inputs.get("interval_seconds")
                interval_f = float(interval) if interval is not None else None
                tz = str(inputs.get("timezone", "UTC"))
                raw_pol = str(inputs.get("missed_run_policy", "fire_once"))
                if raw_pol == "skip":
                    pol: MissedRunPolicy = "skip"
                elif raw_pol == "fire_all":
                    pol = "fire_all"
                else:
                    pol = "fire_once"
                sched = self.manager.register_schedule(
                    schedule_id=sched_id,
                    workspace_id=ws_id,
                    workflow_id=wf_id,
                    release_id=rel_id,
                    cron_expr=cron,
                    interval_seconds=interval_f,
                    timezone=tz,
                    missed_run_policy=pol,
                )
                return ModuleResult.success(
                    outputs={
                        "status": "registered",
                        "schedule_id": sched.schedule_id,
                        "next_run_at": sched.next_run_at,
                    }
                )

            elif op == "tick_schedules":
                ts_arg = inputs.get("current_time")
                current_time = float(ts_arg) if ts_arg is not None else None
                jobs = self.manager.tick_schedules(current_time=current_time)
                return ModuleResult.success(
                    outputs={
                        "status": "ticked",
                        "enqueued_jobs": [j.model_dump() for j in jobs],
                        "count": len(jobs),
                    }
                )

            elif op == "reconcile_missed_runs":
                ts_arg = inputs.get("current_time")
                current_time = float(ts_arg) if ts_arg is not None else None
                jobs = self.manager.reconcile_missed_runs(current_time=current_time)
                return ModuleResult.success(
                    outputs={
                        "status": "reconciled",
                        "enqueued_jobs": [j.model_dump() for j in jobs],
                        "count": len(jobs),
                    }
                )

            elif op == "claim_job":
                j_id = str(inputs.get("job_id", ""))
                w_id = str(inputs.get("worker_id", "worker_default"))
                claimed = self.manager.claim_job(j_id, w_id)
                return ModuleResult.success(
                    outputs={
                        "status": "claimed",
                        "job_id": claimed.job_id,
                        "claimed_by": claimed.claimed_by,
                    }
                )

            else:
                return ModuleResult.failure(
                    error_message=f"Unknown trigger operation: '{op}'",
                    error_code="INVALID_OPERATION",
                )

        except TriggerError as exc:
            return ModuleResult.failure(error_message=str(exc), error_code=exc.code)
        except Exception as exc:
            return ModuleResult.failure(error_message=str(exc), error_code="INTERNAL_ERROR")
