# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""REST API router for schedules, signed webhooks, trigger jobs, and dry-run outbox."""

from __future__ import annotations

import asyncio
import json
import math
import os
import secrets
import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status

from nexura_runtime.integrations.channels.models import ChannelType, OutboxReceipt
from nexura_runtime.integrations.channels.outbox import ChannelStore
from nexura_runtime.observability.run_diagnostics import redact_run_content
from nexura_runtime.service.api.models import (
    CreateScheduleRequest,
    RegisterWebhookRequest,
    TestDispatchOutboxRequest,
    TestWebhookRequest,
)
from nexura_runtime.service.api.operator_token import (
    DELIVERY_REFUSED_MESSAGE,
    delivery_token_store,
    record_delivery_failure,
    single_operator_mode,
)
from nexura_runtime.service.auth.errors import PermissionDeniedError
from nexura_runtime.service.auth.models import ActionPermission, Principal, WorkspaceRole
from nexura_runtime.service.auth.permissions import check_permission
from nexura_runtime.service.storage_backend import store_call
from nexura_runtime.service.triggers.errors import (
    InvalidScheduleError,
    InvalidWebhookSignatureError,
    ReplayWindowExceededError,
    TriggerConflictError,
    TriggerError,
)
from nexura_runtime.service.triggers.manager import TriggerManager
from nexura_runtime.service.triggers.models import (
    ScheduleConfig,
    WebhookEvent,
)
from nexura_runtime.service.triggers.schedules import (
    calculate_next_cron_fire,
    calculate_next_fire,
    get_zoneinfo,
    validate_interval,
)
from nexura_runtime.service.triggers.signatures import (
    compute_webhook_signature,
    is_valid_event_id,
)

router = APIRouter(prefix="/triggers", tags=["triggers"])

DEFAULT_MIN_SCHEDULE_INTERVAL_SECONDS = 60.0
MIN_INTERVAL_ENV = "NEXURA_MIN_SCHEDULE_INTERVAL_SECONDS"
MAX_WEBHOOK_BODY_BYTES = 1_000_000


def _get_caller_context(request: Request) -> tuple[str, Principal, WorkspaceRole]:
    caller_ws: str = getattr(request.state, "workspace_id", "default")
    principal: Principal = getattr(
        request.state,
        "principal",
        Principal(principal_id="local-owner", profile="local-owner"),
    )
    raw_role = getattr(request.state, "role", "admin")
    role: WorkspaceRole = (
        "admin" if raw_role == "admin" else ("editor" if raw_role == "editor" else "viewer")
    )
    return caller_ws, principal, role


def _require(*permissions: ActionPermission) -> Any:
    """Route dependency enforcing workspace permissions (local-owner is always allowed).

    Every listed permission is required (role and API-key scopes are intersected for each).
    """

    def dependency(request: Request) -> None:
        _, principal, role = _get_caller_context(request)
        try:
            for permission in permissions:
                check_permission(principal, role, permission)
        except PermissionDeniedError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "PERMISSION_DENIED", "message": str(exc)},
            ) from exc

    return Depends(dependency)


def _min_schedule_interval(request: Request) -> float:
    configured = getattr(request.app.state, "min_schedule_interval_seconds", None)
    if configured is None:
        raw = os.environ.get(MIN_INTERVAL_ENV)
        try:
            configured = float(raw) if raw else DEFAULT_MIN_SCHEDULE_INTERVAL_SECONDS
        except ValueError:
            configured = DEFAULT_MIN_SCHEDULE_INTERVAL_SECONDS
    return float(max(configured, 0.01))


def _delivery_refused(request: Request, reason: str, original: HTTPException) -> HTTPException:
    """In remote single-operator mode every refused delivery is the same counted 401 (M3).

    The answer for an unknown or inactive endpoint, a bad signature, an expired timestamp or a
    malformed header is identical, so nobody can enumerate endpoint ids or tell which check
    failed. Elsewhere the original, more specific error is kept.
    """
    if not single_operator_mode(request.app):
        return original
    record_delivery_failure(request.app, request.scope, reason)
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "UNAUTHENTICATED", "message": DELIVERY_REFUSED_MESSAGE},
        headers={"WWW-Authenticate": 'Bearer realm="nexura"'},
    )


def _bad_request(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST, detail={"code": code, "message": message}
    )


def _can_see_payloads(request: Request) -> bool:
    """Editors and admins (with an edit-capable credential) may read raw trigger payloads."""
    _, principal, role = _get_caller_context(request)
    try:
        check_permission(principal, role, "edit")
    except PermissionDeniedError:
        return False
    return True


async def _require_workflow_in_workspace(request: Request, workflow_id: str) -> None:
    """The target workflow must exist in the caller workspace (404 otherwise, never a 403)."""
    workspace_id, principal, _ = _get_caller_context(request)
    not_found = HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"code": "WORKFLOW_NOT_FOUND", "message": f"Workflow '{workflow_id}' not found."},
    )
    ws_repo = request.app.state.workspace_repo
    owner = ws_repo.get_resource_workspace("workflow", workflow_id)
    if owner is None and principal.profile == "local-owner" and workspace_id == "default":
        owner = "default"
    if owner is None or owner != workspace_id:
        raise not_found
    if await request.app.state.store.get_workflow(workflow_id) is None:
        raise not_found


def _dedup_storage_key(workspace_id: str, dedup_key: str) -> str:
    """Unambiguous per-workspace key: the length prefix stops 'a'+'b:c' colliding with 'a:b'+'c'."""
    return f"outbox:{len(workspace_id)}:{workspace_id}:{dedup_key}"


def _get_trigger_manager(request: Request) -> TriggerManager:
    return request.app.state.trigger_manager  # type: ignore[no-any-return]


def _get_channel_store(request: Request) -> ChannelStore:
    return request.app.state.channel_store  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------


def _format_schedule(s: ScheduleConfig, *, redact: bool = False) -> dict[str, Any]:
    d = s.model_dump()
    if redact:
        d["payload"] = redact_run_content(d.get("payload"))
    d["paused"] = not s.is_active
    d["is_active"] = s.is_active
    return d


@router.get("/schedules", summary="List schedules")
async def list_schedules(request: Request) -> dict[str, Any]:
    """List recurring schedules registered in the active workspace."""
    workspace_id, _, _ = _get_caller_context(request)
    mgr = _get_trigger_manager(request)
    scheds = mgr.store.list_schedules(workspace_id)
    redact = not _can_see_payloads(request)
    return {
        "schedules": [_format_schedule(s, redact=redact) for s in scheds],
        "total": len(scheds),
    }


@router.post(
    "/schedules",
    summary="Create recurring schedule",
    dependencies=[_require("edit", "run")],
)
async def create_schedule(request: Request, payload: CreateScheduleRequest) -> dict[str, Any]:
    """Register a new timezone-aware execution schedule pinned to a workflow."""
    workspace_id, _, _ = _get_caller_context(request)
    mgr = _get_trigger_manager(request)

    # 1. Validate timezone
    try:
        get_zoneinfo(payload.timezone)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "INVALID_TIMEZONE",
                "message": f"Unknown timezone identifier: '{payload.timezone}'",
                "field": "timezone",
            },
        ) from exc

    # 2. Validate recurrence rule
    if not payload.cron_expr and not payload.interval_seconds:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "MISSING_SCHEDULE_RULE",
                "message": "Either 'cron_expr' or 'interval_seconds' must be provided.",
            },
        )

    if payload.interval_seconds is not None:
        minimum = _min_schedule_interval(request)
        interval = payload.interval_seconds
        try:
            if not math.isfinite(interval) or interval < minimum:
                raise InvalidScheduleError(
                    f"interval_seconds must be at least {minimum:g} seconds "
                    f"(configure {MIN_INTERVAL_ENV} to change the minimum)."
                )
            validate_interval(interval)
        except InvalidScheduleError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "INVALID_INTERVAL",
                    "message": str(exc),
                    "field": "interval_seconds",
                },
            ) from exc

    if payload.cron_expr and payload.interval_seconds is None:
        # Bounded search, off the event loop; impossible expressions are rejected quickly.
        try:
            await asyncio.to_thread(
                calculate_next_cron_fire, time.time(), payload.cron_expr, payload.timezone
            )
        except InvalidScheduleError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "INVALID_CRON", "message": str(exc), "field": "cron_expr"},
            ) from exc

    await _require_workflow_in_workspace(request, payload.workflow_id)

    # 3. Register schedule
    try:
        sched = mgr.register_schedule(
            schedule_id=payload.schedule_id,
            workspace_id=workspace_id,
            workflow_id=payload.workflow_id,
            release_id=payload.release_id,
            cron_expr=payload.cron_expr,
            interval_seconds=payload.interval_seconds,
            timezone=payload.timezone,
            missed_run_policy=payload.missed_run_policy,
            payload=payload.payload,
        )
    except InvalidScheduleError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_CRON", "message": str(exc), "field": "cron_expr"},
        ) from exc
    except TriggerConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "SCHEDULE_ID_CONFLICT", "message": str(exc)},
        ) from exc

    return {
        "success": True,
        "schedule": _format_schedule(sched),
    }


@router.post(
    "/schedules/{schedule_id}/pause", summary="Pause schedule", dependencies=[_require("edit")]
)
async def pause_schedule(request: Request, schedule_id: str) -> dict[str, Any]:
    """Pause an active schedule to prevent execution fires."""
    workspace_id, _, _ = _get_caller_context(request)
    mgr = _get_trigger_manager(request)
    sched = mgr.store.get_schedule(schedule_id)
    if not sched or sched.workspace_id != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "SCHEDULE_NOT_FOUND",
                "message": f"Schedule '{schedule_id}' not found.",
            },
        )

    sched.is_active = False
    mgr.store.save_schedule(sched)
    return {"success": True, "schedule": _format_schedule(sched)}


@router.post(
    "/schedules/{schedule_id}/resume",
    summary="Resume schedule",
    dependencies=[_require("edit", "run")],
)
async def resume_schedule(request: Request, schedule_id: str) -> dict[str, Any]:
    """Resume a paused schedule and recalculate next fire time cleanly."""
    workspace_id, _, _ = _get_caller_context(request)
    mgr = _get_trigger_manager(request)
    sched = mgr.store.get_schedule(schedule_id)
    if not sched or sched.workspace_id != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "SCHEDULE_NOT_FOUND",
                "message": f"Schedule '{schedule_id}' not found.",
            },
        )

    now = time.time()
    try:
        next_run_at = await asyncio.to_thread(calculate_next_fire, now, sched)
    except InvalidScheduleError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_CRON", "message": str(exc), "field": "cron_expr"},
        ) from exc
    sched.is_active = True
    sched.next_run_at = next_run_at
    mgr.store.save_schedule(sched)
    return {"success": True, "schedule": _format_schedule(sched)}


@router.delete(
    "/schedules/{schedule_id}", summary="Delete schedule", dependencies=[_require("edit")]
)
async def delete_schedule(request: Request, schedule_id: str) -> dict[str, Any]:
    """Remove a schedule from the workspace."""
    workspace_id, _, _ = _get_caller_context(request)
    mgr = _get_trigger_manager(request)
    sched = mgr.store.get_schedule(schedule_id)
    if not sched or sched.workspace_id != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "SCHEDULE_NOT_FOUND",
                "message": f"Schedule '{schedule_id}' not found.",
            },
        )

    mgr.store.delete_schedule(schedule_id)
    return {"deleted": True, "schedule_id": schedule_id}


# ---------------------------------------------------------------------------
# Signed Webhooks
# ---------------------------------------------------------------------------


@router.get("/webhooks", summary="List webhooks")
async def list_webhooks(request: Request) -> dict[str, Any]:
    """List registered webhook trigger endpoints in active workspace."""
    workspace_id, _, _ = _get_caller_context(request)
    mgr = _get_trigger_manager(request)
    configs = mgr.store.list_webhook_configs(workspace_id)
    return {
        "webhooks": [
            {
                "endpoint_id": c.endpoint_id,
                "workspace_id": c.workspace_id,
                "workflow_id": c.workflow_id,
                "release_id": c.release_id,
                "replay_window_seconds": c.replay_window_seconds,
                "is_active": c.is_active,
            }
            for c in configs
        ],
        "total": len(configs),
    }


@router.post(
    "/webhooks",
    summary="Register webhook endpoint",
    dependencies=[_require("edit", "run")],
)
async def register_webhook(request: Request, payload: RegisterWebhookRequest) -> dict[str, Any]:
    """Configure a new cryptographically signed webhook endpoint."""
    workspace_id, _, _ = _get_caller_context(request)
    mgr = _get_trigger_manager(request)

    await _require_workflow_in_workspace(request, payload.workflow_id)
    secret = payload.secret or secrets.token_hex(24)
    try:
        cfg = mgr.register_webhook(
            endpoint_id=payload.endpoint_id,
            workspace_id=workspace_id,
            workflow_id=payload.workflow_id,
            release_id=payload.release_id,
            secret=secret,
            replay_window_seconds=payload.replay_window_seconds,
            allow_legacy_signature=False,  # v2 signatures (covering event_id) only
        )
    except TriggerConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "WEBHOOK_ID_CONFLICT", "message": str(exc)},
        ) from exc

    extra: dict[str, Any] = {}
    tokens = delivery_token_store(request.app)
    if tokens is not None:
        # Remote single-operator mode: senders authenticate with this endpoint's own delivery
        # token, never the operator token. Shown here once; only its hash is stored.
        extra = {
            "delivery_token": tokens.issue(cfg.endpoint_id),
            "delivery_token_header": "Authorization: Bearer <delivery_token>",
        }

    return {
        **extra,
        "success": True,
        "endpoint_id": cfg.endpoint_id,
        "workspace_id": cfg.workspace_id,
        "workflow_id": cfg.workflow_id,
        "release_id": cfg.release_id,
        # The secret is returned only here, at registration; list/get never expose it.
        "secret": cfg.secret,
        "replay_window_seconds": cfg.replay_window_seconds,
        "signature_scheme": "v2",
    }


@router.delete(
    "/webhooks/{endpoint_id}", summary="Delete webhook endpoint", dependencies=[_require("edit")]
)
async def delete_webhook(request: Request, endpoint_id: str) -> dict[str, Any]:
    """Remove a webhook endpoint."""
    workspace_id, _, _ = _get_caller_context(request)
    mgr = _get_trigger_manager(request)
    cfg = mgr.store.get_webhook_config(endpoint_id)
    if not cfg or cfg.workspace_id != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "WEBHOOK_NOT_FOUND", "message": f"Webhook '{endpoint_id}' not found."},
        )
    mgr.store.delete_webhook_config(endpoint_id)
    tokens = delivery_token_store(request.app)
    if tokens is not None:
        tokens.delete(endpoint_id)
    return {"deleted": True, "endpoint_id": endpoint_id}


@router.post(
    "/webhooks/{endpoint_id}/delivery-credential",
    summary="Issue a new delivery token for a webhook (remote single-operator mode)",
    dependencies=[_require("edit")],
)
async def rotate_delivery_token(request: Request, endpoint_id: str) -> dict[str, Any]:
    """Replace the endpoint's delivery token and return the new one, once.

    Only in remote single-operator mode (``serve --auth token``); elsewhere 404
    ``DELIVERY_TOKENS_NOT_USED``. The previous token stops working immediately.
    """
    tokens = delivery_token_store(request.app)
    if tokens is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "DELIVERY_TOKENS_NOT_USED",
                "message": "Delivery tokens are only used in remote single-operator mode.",
            },
        )
    workspace_id, _, _ = _get_caller_context(request)
    mgr = _get_trigger_manager(request)
    cfg = mgr.store.get_webhook_config(endpoint_id)
    if not cfg or cfg.workspace_id != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "WEBHOOK_NOT_FOUND", "message": f"Webhook '{endpoint_id}' not found."},
        )
    return {
        "endpoint_id": cfg.endpoint_id,
        "delivery_token": tokens.issue(cfg.endpoint_id),
        "delivery_token_header": "Authorization: Bearer <delivery_token>",
    }


@router.post("/webhooks/{endpoint_id}/deliver", summary="Deliver signed webhook event")
async def deliver_webhook(
    request: Request,
    endpoint_id: str,
    x_webhook_signature: str | None = Header(None, alias="X-Webhook-Signature"),
    x_webhook_timestamp: str | None = Header(None, alias="X-Webhook-Timestamp"),
    x_webhook_event_id: str | None = Header(None, alias="X-Webhook-Event-ID"),
) -> dict[str, Any]:
    """Ingest a cryptographically signed webhook event with deduplication.

    In remote single-operator mode the request already carried this endpoint's delivery token
    (``OperatorTokenMiddleware``); legacy v1 signatures are refused and every failure below is
    the same counted 401 (``_delivery_refused``).
    """
    mgr = _get_trigger_manager(request)
    remote = single_operator_mode(request.app)

    # 1. Enforce presence of security headers
    if not x_webhook_signature or not x_webhook_timestamp or not x_webhook_event_id:
        raise _delivery_refused(
            request,
            "missing signature headers",
            HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={
                    "code": "MISSING_SIGNATURE_HEADERS",
                    "message": (
                        "Webhook request missing required X-Webhook-Signature, "
                        "X-Webhook-Timestamp, or X-Webhook-Event-ID header."
                    ),
                },
            ),
        )

    try:
        timestamp = float(x_webhook_timestamp)
    except ValueError:
        timestamp = math.nan
    if not math.isfinite(timestamp):
        raise _delivery_refused(
            request,
            "invalid timestamp",
            _bad_request("INVALID_TIMESTAMP", "X-Webhook-Timestamp must be a finite number."),
        )
    if not is_valid_event_id(x_webhook_event_id):
        raise _delivery_refused(
            request,
            "invalid event id",
            _bad_request(
                "INVALID_EVENT_ID",
                "X-Webhook-Event-ID must be 1-200 characters of [A-Za-z0-9_:-].",
            ),
        )

    body_bytes = await request.body()
    if len(body_bytes) > MAX_WEBHOOK_BODY_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={"code": "BODY_TOO_LARGE", "message": "Webhook body is too large."},
        )
    try:
        raw_body = json.loads(body_bytes) if body_bytes else {}
    except (ValueError, RecursionError):
        raw_body = None
    if not isinstance(raw_body, dict):
        raise _delivery_refused(
            request,
            "invalid body",
            _bad_request("INVALID_BODY", "Webhook body must be a JSON object."),
        )

    event = WebhookEvent(
        event_id=x_webhook_event_id,
        endpoint_id=endpoint_id,
        timestamp=timestamp,
        signature=x_webhook_signature,
        payload=raw_body,
    )

    try:
        # Remote mode: v2 signatures only, whatever the endpoint allows (v1 does not cover
        # event_id, so a captured v1 delivery could be replayed under a new id).
        is_new, job = mgr.handle_webhook(event, refuse_legacy=remote)
    except InvalidWebhookSignatureError as exc:
        raise _delivery_refused(
            request,
            "invalid signature",
            HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "INVALID_SIGNATURE", "message": str(exc)},
            ),
        ) from exc
    except ReplayWindowExceededError as exc:
        raise _delivery_refused(
            request,
            "expired timestamp",
            HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "REPLAY_EXPIRED", "message": str(exc)},
            ),
        ) from exc
    except TriggerError as exc:
        raise _delivery_refused(
            request,
            "unknown or inactive endpoint",
            HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "TRIGGER_ERROR", "message": str(exc)},
            ),
        ) from exc

    return {
        "accepted": True,
        "is_duplicate": not is_new,
        "job_id": job.job_id,
        "status": job.status,
    }


@router.post(
    "/webhooks/{endpoint_id}/test",
    summary="Test webhook delivery with HMAC verification",
    dependencies=[_require("admin")],
)
async def test_webhook(
    request: Request, endpoint_id: str, payload: TestWebhookRequest
) -> dict[str, Any]:
    """Simulate a signed webhook delivery event with optional negative cases."""
    workspace_id, _, _ = _get_caller_context(request)
    mgr = _get_trigger_manager(request)
    cfg = mgr.store.get_webhook_config(endpoint_id)
    if not cfg or cfg.workspace_id != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "WEBHOOK_NOT_FOUND", "message": f"Webhook '{endpoint_id}' not found."},
        )

    now = time.time()
    ts = payload.timestamp if payload.timestamp is not None else now
    if payload.expired_timestamp:
        ts = now - (cfg.replay_window_seconds + 60)

    event_id = payload.event_id or f"evt_{uuid.uuid4().hex[:12]}"
    body_data = payload.payload or {"event": "test_ping", "timestamp": now}

    event = WebhookEvent(
        event_id=event_id,
        endpoint_id=endpoint_id,
        timestamp=ts,
        signature="",
        payload=body_data,
    )

    if not is_valid_event_id(event_id):
        raise _bad_request(
            "INVALID_EVENT_ID", "event_id must be 1-200 characters of [A-Za-z0-9_:-]."
        )

    # The computed signature is used internally only and is never returned to the caller:
    # returning it would turn this route into a signing oracle for arbitrary payloads.
    if payload.invalid_signature:
        event.signature = "forged_invalid_signature_hex_1234567890"
    else:
        event.signature = compute_webhook_signature(
            cfg.secret, event.timestamp, event.payload, event_id
        )

    try:
        is_new, job = mgr.handle_webhook(event)
    except InvalidWebhookSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_SIGNATURE", "message": str(exc)},
        ) from exc
    except ReplayWindowExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "REPLAY_EXPIRED", "message": str(exc)},
        ) from exc

    return {
        "success": True,
        "is_duplicate": not is_new,
        "job_id": job.job_id,
        "status": job.status,
        "event_id": event_id,
        "timestamp_used": ts,
    }


# ---------------------------------------------------------------------------
# Trigger Jobs & Observable Dry-Run Outbox
# ---------------------------------------------------------------------------


@router.get("/jobs", summary="List durable trigger jobs")
async def list_jobs(request: Request, limit: int = Query(50, ge=1, le=100)) -> dict[str, Any]:
    """List pending and claimed trigger jobs in active workspace."""
    workspace_id, _, _ = _get_caller_context(request)
    mgr = _get_trigger_manager(request)
    jobs = mgr.store.list_pending_jobs(workspace_id=workspace_id, limit=limit)
    redact = not _can_see_payloads(request)
    out = []
    for j in jobs:
        d = j.model_dump()
        if redact:
            d["payload"] = redact_run_content(d.get("payload"))
            if d.get("error_message"):
                d["error_message"] = redact_run_content(d["error_message"])
        out.append(d)
    return {
        "jobs": out,
        "total": len(jobs),
    }


@router.get("/outbox", summary="List observable dry-run outbox")
async def list_outbox(
    request: Request,
    channel: ChannelType | None = Query(
        None, description="Optional channel filter: email, whatsapp"
    ),
) -> dict[str, Any]:
    """Retrieve all simulated dry-run outbound messages. Guarantees zero real dispatch."""
    workspace_id, principal, _ = _get_caller_context(request)
    channel_store = _get_channel_store(request)
    receipts = await store_call(
        channel_store,
        channel_store.list_outbox,
        channel=channel,
        dry_run=True,
        workspace_id=workspace_id,
        # Workspace-less receipts predate scoping or come from single-tenant local use.
        include_unscoped=principal.profile == "local-owner",
    )
    return {
        "outbox": [r.model_dump() for r in receipts],
        "total": len(receipts),
        "guarantee": (
            "DRY_RUN_ONLY: All outbound messages are captured in outbox and never transmitted to "
            "real carriers."
        ),
    }


@router.post(
    "/outbox/test-dispatch",
    summary="Dispatch simulated message to dry-run outbox",
    dependencies=[_require("edit")],
)
async def test_dispatch_outbox(
    request: Request, payload: TestDispatchOutboxRequest
) -> dict[str, Any]:
    """Dispatch an outbound action to the observable dry-run outbox with deduplication."""
    workspace_id, _, _ = _get_caller_context(request)
    channel_store = _get_channel_store(request)

    # 1. Deduplication check (keys are namespaced per workspace)
    is_duplicate = False
    if payload.dedup_key:
        # One insert that detects the conflict: a key recorded on any instance is a duplicate
        # on every other (Postgres, MVP WP8).
        is_new = await store_call(
            channel_store,
            channel_store.record_dedup,
            _dedup_storage_key(workspace_id, payload.dedup_key),
        )
        if not is_new:
            is_duplicate = True

    msg_id = f"msg_{uuid.uuid4().hex[:12]}"
    receipt_id = f"rcpt_{uuid.uuid4().hex[:12]}"
    now = time.time()

    receipt = OutboxReceipt(
        receipt_id=receipt_id,
        message_id=msg_id,
        channel=payload.channel,
        recipient=payload.recipient,
        status="deduplicated" if is_duplicate else "dry_run",
        dry_run=True,
        rendered_content={
            "subject_or_template": payload.subject_or_template,
            "body": payload.body,
        },
        created_at=now,
        workspace_id=workspace_id,
    )

    await store_call(channel_store, channel_store.record_receipt, receipt)

    return {
        "success": True,
        "receipt": receipt.model_dump(),
        "is_duplicate": is_duplicate,
        "dry_run": True,
        "guarantee": "Simulated in local dry-run outbox without external API dispatch.",
    }
