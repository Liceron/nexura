# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""API endpoints for starting, inspecting, cancelling, and streaming workflow runs."""

from __future__ import annotations

import asyncio
import base64
import copy
import inspect
import json
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine, Mapping
from typing import Any, Literal, TypeVar

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi import status as http_status
from fastapi.responses import JSONResponse, StreamingResponse
from nexura_agents.contracts.execution import (
    DefinitionMismatchError,
    ExecutionId,
    LeaseLostError,
)
from nexura_agents.errors import (
    ApprovalAlreadyClaimedError,
    ApprovalError,
    WorkflowConflictError,
    WorkflowError,
    WorkflowSpecMismatchError,
    WorkflowTerminalError,
)
from nexura_agents.network.redaction import redact_secret_values
from nexura_agents.usage.models import BudgetConfig
from pydantic import ValidationError

from nexura_runtime.observability.run_diagnostics import redact_run_content
from nexura_runtime.runtime.approvals.errors import WrongWorkspaceError
from nexura_runtime.service.api.models import (
    CancelRunRequest,
    DecisionRequest,
    ForkRunRequest,
    PaginatedResponse,
    ProvideInputRequest,
    RecoverRunRequest,
    RunResponse,
    StartRunRequest,
)
from nexura_runtime.service.api.policy import check_approval_policy, enforce_approval_policy
from nexura_runtime.service.api.run_revocation import ACCESS_REVOKED, cancel_message
from nexura_runtime.service.auth.errors import (
    CrossWorkspaceAccessError,
    PermissionDeniedError,
)
from nexura_runtime.service.auth.models import Principal, WorkspaceRole
from nexura_runtime.service.auth.permissions import check_permission
from nexura_runtime.service.auth.resources import require_resource
from nexura_runtime.service.repro_capture import (
    capture_recorder,
    load_captured_bundle,
    settle_failed_run_capture,
)
from nexura_runtime.service.run_liveness import (
    LIVE,
    SHUTDOWN_ERROR,
    SHUTDOWN_REASON,
    journal_workspace,
    reconcile_quietly,
    run_journal,
    run_liveness,
    show_live,
    workflow_lease_ttl,
)
from nexura_runtime.service.run_liveness import journal_decides as _journal_decides
from nexura_runtime.service.workspaces.repository import UNOWNED_WORKSPACE, WorkspaceRepository
from nexura_runtime.service.workspaces.scoping import ResourceScopeEnforcer
from nexura_runtime.studio.store import StudioStore
from nexura_runtime.studio.traces import capture_agent_events
from nexura_runtime.workflow import (
    HandlerOutput,
    ModuleRegistry,
    WorkflowEngine,
    WorkflowRunResult,
    WorkflowSpec,
    _LeaseBusyError,
    checkpoint_to_dict,
)

router = APIRouter(prefix="/runs", tags=["runs"])

_T = TypeVar("_T")


def _get_caller_context(request: Request) -> tuple[str, Principal | None, WorkspaceRole]:
    """Extract (workspace_id, principal, role) from request state."""
    workspace_id: str = getattr(request.state, "workspace_id", "default")
    principal: Principal | None = getattr(request.state, "principal", None)
    raw_role = getattr(
        request.state,
        "role",
        "admin" if (principal and principal.profile == "local-owner") else "viewer",
    )
    role: WorkspaceRole = (
        "admin" if raw_role == "admin" else ("editor" if raw_role == "editor" else "viewer")
    )
    return workspace_id, principal, role


def _api_key_id(principal: Principal | None) -> str | None:
    """The API key id a request authenticated with, or None (JWT, local owner)."""
    if principal is None or principal.metadata.get("auth_kind") != "api_key":
        return None
    key_id = principal.metadata.get("key_id")
    return str(key_id) if key_id else None


def _started_by(principal_id: str, api_key_id: str | None) -> dict[str, Any]:
    """Who started a run, stored on the run row (B40 finds an API key's runs with it)."""
    started: dict[str, Any] = {"principal_id": principal_id}
    if api_key_id:
        started["api_key_id"] = api_key_id
    return started


def _access_ended_check(
    app_state: Any, workspace_id: str, principal: Principal | None
) -> Callable[[], str | None] | None:
    """A check that the caller still has the access this request was authorised with (B40).

    Call it before the request's first ``await``. The returned check runs synchronously right
    after a new run's ownership is recorded and returns ``access_revoked`` when the caller's
    membership ended or its API key was revoked meanwhile. A revocation landing later finds
    the run by its ownership and cancels it, so neither order leaves a live run behind.
    """
    if principal is None or principal.profile == "local-owner":
        return None
    ws_repo: WorkspaceRepository = app_state.workspace_repo
    principal_id = principal.principal_id
    was_member = ws_repo.get_member(workspace_id, principal_id) is not None
    key_id = _api_key_id(principal)
    key_manager = getattr(getattr(app_state, "profile_manager", None), "api_key_manager", None)

    def access_ended() -> str | None:
        if was_member and ws_repo.get_member(workspace_id, principal_id) is None:
            return ACCESS_REVOKED
        if key_id is not None and key_manager is not None:
            key = key_manager.get_api_key(key_id)
            if key is None or key.is_revoked:
                return ACCESS_REVOKED
        return None

    return access_ended


async def _cancel_if_access_ended(
    app_state: Any, run_id: str, access_ended: Callable[[], str | None] | None
) -> None:
    """Cancel a just-recorded run whose starter lost access meanwhile, and refuse with 409.

    Call with no await since the run's ownership was recorded (see :func:`_access_ended_check`).
    """
    ended = access_ended() if access_ended is not None else None
    if ended is None:
        return
    await cancel_run_record(app_state, run_id, reason=cancel_message(ended), cancel_reason=ended)
    raise HTTPException(
        status_code=http_status.HTTP_409_CONFLICT,
        detail={
            "code": "RUN_ACCESS_REVOKED",
            "message": f"Access ended before run '{run_id}' could start ({ended}).",
        },
    )


def _assert_run_resource(request: Request, run: Mapping[str, Any]) -> None:
    """A resource-scoped API key may read only runs it started, of workflows it lists."""
    _, principal, _ = _get_caller_context(request)
    if principal is None or principal.resources is None:
        return
    try:
        require_resource(principal, "workflow", run.get("workflow_id"))
        ws_repo: WorkspaceRepository = request.app.state.workspace_repo
        ownership = ws_repo.get_resource_ownership("run", str(run.get("run_id", "")))
        if ownership is None or ownership[1] != principal.principal_id:
            raise PermissionDeniedError(
                "This API key can only read runs it started.", code="RESOURCE_SCOPE_DENIED"
            )
    except PermissionDeniedError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc


def _assert_run_ownership(request: Request, run_id: str) -> None:
    """Verify that the execution run belongs to the caller's workspace."""
    caller_ws, principal, _ = _get_caller_context(request)
    ws_repo: WorkspaceRepository = request.app.state.workspace_repo
    owner_ws = ws_repo.get_resource_workspace("run", run_id)

    # In local-owner mode with unmapped legacy resources, default workspace owns it
    if (
        owner_ws is None
        and principal
        and principal.profile == "local-owner"
        and caller_ws == "default"
    ):
        owner_ws = "default"
        ws_repo.record_resource_ownership("run", run_id, "default", principal.principal_id)

    if owner_ws == UNOWNED_WORKSPACE:
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail={
                "code": "CROSS_WORKSPACE_ACCESS_DENIED",
                "message": "Resource has no workspace owner; access denied.",
            },
        )
    if owner_ws is not None and owner_ws != caller_ws:
        if principal and principal.profile == "local-owner":
            # One person owns every local workspace; say why instead of "not found".
            raise HTTPException(
                status_code=http_status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "CROSS_WORKSPACE_ACCESS_DENIED",
                    "message": (
                        f"Cross-workspace access denied: run '{run_id}' belongs to "
                        f"workspace '{owner_ws}', not '{caller_ws}'."
                    ),
                },
            )
        # Shared service: same response as a missing run so ids cannot be probed
        # across tenants.
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail={"code": "RUN_NOT_FOUND", "message": f"Run '{run_id}' not found."},
        )


def _parse_output_for_event(raw: Any) -> tuple[dict[str, Any], str | None]:
    if isinstance(raw, HandlerOutput):
        return raw.state_update, raw.branch
    if isinstance(raw, dict):
        d = dict(raw)
        branch = d.pop("_branch", None)
        return d, branch
    return {}, None


_REDACTED_RUN_FIELDS = (
    "input_state",
    "current_state",
    "pending_approval",
    "pending_input",
    "forked_from",
    "error",
    "pending_effect",
)


def _approval_view(value: Any, secrets: list[str]) -> Any:
    """Complete approval content: redact values, never fields based on their names."""
    if isinstance(value, dict):
        return {key: _approval_view(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_approval_view(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in sorted(secrets, key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
        return redact_secret_values(value)
    return value


def _redact_events(run: dict[str, Any], app_state: Any) -> dict[str, Any]:
    """Return the run snapshot with events, input/current state and errors redacted.

    Applied to every run snapshot response so stored legacy rows and state never leak
    credentials that the event stream already masks.
    """
    safe = dict(run)
    events = run.get("events")
    if isinstance(events, list):
        safe["events"] = [
            {**ev, "data": redact_run_content(ev.get("data", {}))} if isinstance(ev, dict) else ev
            for ev in events
        ]
    for field in _REDACTED_RUN_FIELDS:
        if run.get(field) is not None:
            safe[field] = redact_run_content(run[field])
    pending = run.get("pending_approval")
    if isinstance(pending, dict):
        workspace = app_state.workspace_repo.get_resource_workspace("run", run["run_id"])
        credentials = getattr(app_state, "credential_store", None)
        secrets = credentials._redaction_values(workspace or "default") if credentials else []
        reviewed = _approval_view(pending, secrets)
        canonical = pending.get("canonical_payload_json")
        if canonical:
            # Parse before redaction so escaping in canonical JSON cannot conceal a secret.
            parsed = json.loads(canonical)
            cleaned = _approval_view(parsed, secrets)
            reviewed["canonical_payload_json"] = (
                canonical
                if cleaned == parsed
                else json.dumps(cleaned, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            )
        safe["pending_approval"] = reviewed
    if isinstance(run.get("spec"), dict):
        # The bound spec is echoed for display only (runs execute from the stored copy),
        # so node-config secrets get the same scrubbing as an export.
        from nexura_runtime.service.api.export import WorkflowExporter

        safe["spec"] = WorkflowExporter().sanitize_spec_dict(run["spec"])
    return safe


async def _record_event(
    store: StudioStore,
    run_id: str,
    event_type: str,
    *,
    node_id: str | None = None,
    data: Any = None,
) -> Any:
    """Persist a run event with secrets redacted from its payload before storage."""
    safe = redact_run_content(data) if data is not None else data
    return await store.record_run_event(run_id, event_type, node_id=node_id, data=safe)


def _lost_lease(exc: BaseException) -> bool:
    """The run's lease went to another worker (A6): that worker now owns its status."""
    seen: BaseException | None = exc
    for _ in range(5):
        if seen is None:
            return False
        if isinstance(seen, (LeaseLostError, WorkflowConflictError)):
            return True
        seen = seen.__cause__ or seen.__context__
    return False


async def _record_execution_failure(store: StudioStore, run_id: str, exc: Exception) -> None:
    if _lost_lease(exc):
        # A stale owner (its lease expired and another process recovered the run) must not
        # mark the run failed: the recovering process records the outcome (A6 S9).
        await _record_event(
            store, run_id, "RUN_LEASE_LOST", data={"error": str(redact_run_content(str(exc)))}
        )
        return
    checkpoint = await store.checkpoint_store.get(run_id)
    snapshot = await store.get_run(run_id)
    if snapshot is None or snapshot.get("status") == "cancelled":
        # A cancelled run stays cancelled (B40: revocation may land mid-resume).
        return
    message = str(redact_run_content(str(exc)))
    node = checkpoint.current_node_id if checkpoint else snapshot.get("current_node_id")
    await _record_event(store, run_id, "RUN_FAILED", node_id=node, data={"error": message})
    await store.update_run(
        run_id=run_id,
        status="failed",
        current_node_id=node,
        current_state=checkpoint.state if checkpoint else snapshot.get("current_state", {}),
        step_count=checkpoint.step_count if checkpoint else snapshot.get("step_count", 0),
        pending_approval=None,
        error=message,
        unless_status=_CANCEL_TERMINAL_STATUSES,
    )


async def _persist_result(store: StudioStore, run_id: str, res: WorkflowRunResult) -> None:
    """Record the events and run row for any engine result (start, decide, input, fork)."""
    if await store.get_run_status(run_id) == "cancelled":
        # Cancelled is final: a resume racing a cancel or revocation must not revive the run.
        return
    common: dict[str, Any] = {
        "run_id": run_id,
        "current_node_id": res.current_node_id,
        "current_state": res.state,
        "step_count": res.step_count,
        # Compare-and-set: a cancel landing after the check above still wins (B40).
        "unless_status": _CANCEL_TERMINAL_STATUSES,
    }
    if res.status == "paused" and res.interrupt is not None:
        pending_input = {
            "interrupt_id": res.interrupt.get("interrupt_id"),
            "node_id": res.interrupt.get("node_id"),
            "value": res.interrupt.get("value"),
        }
        await _record_event(
            store,
            run_id,
            "RUN_WAITING_FOR_INPUT",
            node_id=res.current_node_id,
            data={
                "interrupt_id": pending_input["interrupt_id"],
                "node_id": pending_input["node_id"],
            },
        )
        await store.update_run(status="waiting_for_input", pending_input=pending_input, **common)
    elif res.status == "paused" and res.pending_effect is not None:
        # A6: an effect's outcome is unknown after a crash; a person resolves it
        # (POST /runs/{id}/effects/{effect_id}/resolve), then POST /runs/{id}/recover.
        await _record_event(
            store,
            run_id,
            "RUN_PAUSED_EFFECT_UNKNOWN",
            node_id=res.current_node_id,
            data={
                "pause_reason": res.pause_reason,
                "effect_id": res.pending_effect.get("effect_id"),
            },
        )
        await store.update_run(status="paused", pending_effect=res.pending_effect, **common)
    elif res.status == "paused":
        app_id = res.pending_approval.approval_id if res.pending_approval else None
        await _record_event(
            store,
            run_id,
            "RUN_PENDING_APPROVAL",
            node_id=res.current_node_id,
            data={"approval_id": app_id},
        )
        await store.update_run(
            status="pending_approval", pending_approval=res.pending_approval, **common
        )
    elif res.status == "completed":
        await _record_event(
            store,
            run_id,
            "RUN_COMPLETED",
            node_id=res.current_node_id,
            data={"final_state": res.state},
        )
        await store.update_run(status="completed", **common)
    elif res.status in ("failed", "rejected"):
        await _record_event(
            store,
            run_id,
            "RUN_FAILED" if res.status == "failed" else "RUN_REJECTED",
            node_id=res.current_node_id,
            data={"error": res.error},
        )
        await store.update_run(status=res.status, error=res.error, **common)


class RunStoreEventSink:
    """Consumes module events and writes them into durable run event storage."""

    def __init__(self, store: StudioStore, run_id: str) -> None:
        self.store = store
        self.run_id = run_id
        self._last_write: asyncio.Task[None] | None = None

    def emit(self, event: Any) -> None:
        loop = asyncio.get_running_loop()
        previous = self._last_write
        ev_type = getattr(event, "type", None)
        type_str = str(getattr(ev_type, "value", ev_type or "EVENT"))

        async def persist() -> None:
            if previous is not None:
                await previous
            await _record_event(
                self.store,
                self.run_id,
                f"MODULE_{type_str.upper()}",
                node_id=getattr(event, "node_id", None),
                data={
                    "node_id": getattr(event, "node_id", None),
                    "module_id": getattr(event, "module_id", None),
                    "version": getattr(event, "version", None),
                    "attempt_id": getattr(event, "attempt_id", None),
                    "emitted_at": getattr(event, "timestamp", None),
                    "payload": getattr(event, "payload", {}),
                },
            )

        self._last_write = loop.create_task(persist())

    async def flush(self) -> None:
        if self._last_write is not None:
            await asyncio.shield(self._last_write)


class RunEventRegistry(ModuleRegistry):
    """Intercepts node execution to emit durable events into the store."""

    def __init__(
        self,
        inner: ModuleRegistry,
        store: StudioStore,
        run_id: str,
        stop_requested: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__()
        self._inner = inner
        self._store = store
        self._run_id = run_id
        self._stop_requested = stop_requested

    @property
    def versioned_registry(self) -> Any:
        return self._inner.versioned_registry

    def has_handler(self, name: str) -> bool:
        return self._inner.has_handler(name)

    def has_module(self, module_id: str, version: str | None = None) -> bool:
        return self._inner.has_module(module_id, version)

    def get_module(self, module_id: str, version: str | None = None) -> Any:
        return self._inner.get_module(module_id, version)

    def describe_module(self, module_id: str, version: str | None = None) -> dict[str, Any] | None:
        return self._inner.describe_module(module_id, version)

    def declared_effect(self, name: str) -> Any:
        # The engine journals a node by the effect class its handler was registered with
        # (``register(..., effect=...)``); without this every handler was non_idempotent.
        return self._inner.declared_effect(name)

    def get(self, name: str) -> Any:
        obj = self._inner.get(name)
        from nexura_agents.contracts.base import BaseModule

        if isinstance(obj, BaseModule) or hasattr(obj, "execute"):
            return obj

        fn = obj

        async def wrapped(state: dict[str, Any]) -> Any:
            if self._stop_requested is not None and self._stop_requested():
                # The run was cancelled from inside its own execution (B40): stop before
                # the next node instead of running it.
                raise asyncio.CancelledError
            await _record_event(
                self._store,
                self._run_id,
                "NODE_STARTED",
                node_id=name,
                data={"input_state": copy.deepcopy(state)},
            )
            try:
                with capture_agent_events(self._store, self._run_id, name):
                    if inspect.iscoroutinefunction(fn):
                        out = await fn(state)
                    else:
                        out = await asyncio.to_thread(fn, state)
            except Exception as exc:
                await _record_event(
                    self._store,
                    self._run_id,
                    "NODE_FAILED",
                    node_id=name,
                    data={"error": str(exc)},
                )
                raise

            state_update, branch = _parse_output_for_event(out)
            await _record_event(
                self._store,
                self._run_id,
                "NODE_COMPLETED",
                node_id=name,
                data={"state_update": state_update, "branch": branch},
            )
            return out

        return wrapped


@router.get("", response_model=PaginatedResponse[dict[str, Any]])
async def list_runs(
    request: Request,
    workflow_id: str | None = Query(default=None, description="Filter by workflow ID"),
    status: str | None = Query(default=None, description="Filter by run status"),
    limit: int = Query(default=20, ge=1, le=100, description="Page size limit"),
    offset: int = Query(default=0, ge=0, description="Page offset"),
) -> PaginatedResponse[dict[str, Any]]:
    """List execution runs scoped to caller workspace with pagination and optional filtering."""
    store: StudioStore = request.app.state.store
    ws_repo: WorkspaceRepository = request.app.state.workspace_repo
    caller_ws, principal, _ = _get_caller_context(request)

    # If workflow_id is provided, verify it belongs to caller workspace
    if workflow_id:
        wf_owner = ws_repo.get_resource_workspace("workflow", workflow_id)
        if (
            wf_owner is None
            and principal
            and principal.profile == "local-owner"
            and caller_ws == "default"
        ):
            wf_owner = "default"
        if wf_owner is not None and wf_owner != caller_ws:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "WORKFLOW_NOT_FOUND",
                    "message": f"Workflow '{workflow_id}' not found.",
                },
            )

    legacy_local = bool(principal and principal.profile == "local-owner" and caller_ws == "default")
    if legacy_local:
        # Local owner: unmapped legacy runs belong to the default workspace. Adopt them
        # (single-user machine), then filter by ownership in SQL like every other caller.
        for row in await store.list_runs(workflow_id=workflow_id, limit=None):
            r_id = row.get("run_id", "")
            if ws_repo.get_resource_workspace("run", r_id) is None:
                ws_repo.record_resource_ownership("run", r_id, "default", principal.principal_id)  # type: ignore[union-attr]
    # A6 S9: journaled runs nobody executes any more become interrupted before filtering.
    live_again = await reconcile_quietly(request.app.state, workspace_id=caller_ws)
    # The workspace filter runs in SQL before LIMIT/OFFSET so other tenants' runs can
    # neither hide nor delay this workspace's runs.
    owned_ids = ws_repo.list_workspace_resource_ids("run", caller_ws)
    total = await store.count_runs(workflow_id=workflow_id, run_ids=owned_ids, status=status)
    page_items = [
        _redact_events(show_live(r, live_again), request.app.state)
        for r in await store.list_runs(
            workflow_id=workflow_id,
            limit=limit,
            offset=offset,
            run_ids=owned_ids,
            status=status,
        )
    ]
    has_more = (offset + limit) < total

    return PaginatedResponse(
        items=page_items,
        total=total,
        limit=limit,
        offset=offset,
        has_more=has_more,
    )


@router.post("", status_code=http_status.HTTP_201_CREATED)
async def start_run(request: Request, payload: StartRunRequest) -> dict[str, Any]:
    """Start a new workflow execution run synchronously or as an async background job."""
    caller_ws, principal, role = _get_caller_context(request)
    if principal:
        try:
            check_permission(principal, role, "run")
            if principal.resources is not None:
                if payload.spec is not None:
                    raise PermissionDeniedError(
                        "This API key is limited to listed workflows; ad-hoc specs are refused.",
                        code="RESOURCE_SCOPE_DENIED",
                    )
                require_resource(principal, "workflow", payload.workflow_id)
        except PermissionDeniedError as exc:
            raise HTTPException(
                status_code=http_status.HTTP_403_FORBIDDEN,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc

    # Before the first await: access ending while this request runs stops its run (B40).
    access_ended = _access_ended_check(request.app.state, caller_ws, principal)
    store: StudioStore = request.app.state.store
    base_registry: ModuleRegistry = request.app.state.registry
    ws_repo: WorkspaceRepository = request.app.state.workspace_repo

    # 1. Resolve workflow spec
    spec: WorkflowSpec
    wf_id: str | None = payload.workflow_id
    if wf_id:
        wf_owner = ws_repo.get_resource_workspace("workflow", wf_id)
        if (
            wf_owner is None
            and principal
            and principal.profile == "local-owner"
            and caller_ws == "default"
        ):
            wf_owner = "default"
        if wf_owner is not None and wf_owner != caller_ws:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "WORKFLOW_NOT_FOUND",
                    "message": f"Workflow '{wf_id}' not found.",
                },
            )

        wf = await store.get_workflow(wf_id)
        if wf is None:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail={"code": "WORKFLOW_NOT_FOUND", "message": f"Workflow '{wf_id}' not found."},
            )
        spec = WorkflowSpec.from_dict(wf["spec"])
    elif payload.spec:
        try:
            spec = WorkflowSpec.from_dict(payload.spec)
            spec.validate(base_registry)
        except Exception as exc:
            raise HTTPException(
                status_code=422,  # literal: the constant was renamed in newer Starlette
                detail={"code": "VALIDATION_ERROR", "message": f"Invalid spec: {exc}"},
            ) from exc
    else:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={"code": "MISSING_SPEC", "message": "Provide 'workflow_id' or 'spec'."},
        )

    principal_id = principal.principal_id if principal else "local-owner"
    return await start_workflow_run(
        request.app.state,
        spec=spec,
        workflow_id=wf_id,
        workspace_id=caller_ws,
        principal_id=principal_id,
        initial_state=payload.initial_state,
        budget_config=payload.budget_config,
        async_execution=payload.async_execution,
        api_key_id=_api_key_id(principal),
        access_ended=access_ended,
    )


async def start_workflow_run(
    app_state: Any,
    *,
    spec: WorkflowSpec,
    workflow_id: str | None,
    workspace_id: str,
    principal_id: str,
    initial_state: dict[str, Any],
    budget_config: dict[str, Any] | None = None,
    async_execution: bool = False,
    trigger: dict[str, Any] | None = None,
    on_run_id: Callable[[str], None] | None = None,
    api_key_id: str | None = None,
    access_ended: Callable[[], str | None] | None = None,
) -> dict[str, Any]:
    """Start a run for a resolved spec; shared by POST /runs and the trigger worker.

    ``app_state`` is the FastAPI ``app.state`` (or any object with the same attributes).
    Raises HTTPException for approval-policy or cross-workspace reference violations.
    ``access_ended`` (B40) is called right after the run's ownership is recorded; when it
    returns a cancel reason the run is cancelled before it executes and a 409
    ``RUN_ACCESS_REVOKED`` is raised.
    """
    store: StudioStore = app_state.store
    base_registry: ModuleRegistry = app_state.registry
    ws_repo: WorkspaceRepository = app_state.workspace_repo

    check_approval_policy(getattr(app_state, "approval_required_handlers", frozenset()), spec)

    # Validate cross-resource references (credentials, artifacts, indexes, datasets, schedules)
    scope_enforcer = ResourceScopeEnforcer(ws_repo)
    try:
        if initial_state:
            scope_enforcer.scan_and_validate_references(workspace_id, initial_state)
        if spec:
            scope_enforcer.scan_and_validate_references(workspace_id, spec.to_dict())
    except CrossWorkspaceAccessError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail={"code": "CROSS_WORKSPACE_ACCESS_DENIED", "message": str(exc)},
        ) from exc

    # Build-time validation (also covers gated handlers nested in sub-workflows) must fail
    # before any run record exists, so a bad spec never leaves an orphan 'running' run.
    try:
        spec.validate(
            base_registry,
            approval_required_handlers=getattr(
                app_state, "approval_required_handlers", frozenset()
            ),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "VALIDATION_ERROR", "message": f"Invalid spec: {exc}"},
        ) from exc

    try:
        cfg = BudgetConfig(**budget_config) if budget_config is not None else None
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_BUDGET_CONFIG", "message": "Invalid budget_config."},
        ) from exc

    run_id = f"run-{uuid.uuid4().hex[:12]}"
    await store.record_run_start(
        run_id=run_id,
        workflow_id=workflow_id,
        spec=spec,
        initial_state=initial_state,
        started_by=_started_by(principal_id, api_key_id),
        journal_workspace=journal_workspace(app_state, workspace_id),
    )

    # Record durable workspace ownership for the run
    ws_repo.record_resource_ownership("run", run_id, workspace_id, principal_id)
    if on_run_id is not None:
        on_run_id(run_id)
    # No await since the ownership row: a revocation before this point is seen here, one
    # after it finds the run by its ownership (B40).
    await _cancel_if_access_ended(app_state, run_id, access_ended)
    if trigger is not None:
        await _record_event(store, run_id, "RUN_TRIGGERED", data=dict(trigger))

    approval_handlers: frozenset[str] = getattr(
        app_state, "approval_required_handlers", frozenset()
    )
    event_registry = RunEventRegistry(
        base_registry, store, run_id, stop_requested=_stop_check(app_state, run_id)
    )
    event_sink = RunStoreEventSink(store, run_id)

    run_budget = None
    budget_mgr = _budget_manager(app_state)
    if cfg is not None:
        from nexura_agents.usage.manager import BudgetManager

        if budget_mgr is None:
            budget_mgr = BudgetManager()
            app_state.budget_manager = budget_mgr
        run_budget = budget_mgr.create_run_budget(
            workspace_id=workspace_id,
            run_id=run_id,
            config=cfg,
        )
    elif budget_mgr is not None:
        run_budget = budget_mgr.get_run_budget(run_id)

    engine = WorkflowEngine(
        spec=spec,
        registry=event_registry,
        store=store.checkpoint_store,
        workspace_id=workspace_id,
        budget=run_budget,
        event_sink=event_sink,
        approval_required_handlers=approval_handlers,
        lease_ttl=workflow_lease_ttl(app_state),
    )

    async def _execute_job() -> None:
        try:
            if await _settled_status(app_state, run_id) == "cancelled":
                # Cancelled (revoked) between the run record and this task (B40).
                return
            # Opt-in failed-run capture records the run as a portable repro bundle.
            recorder = capture_recorder(app_state, run_id, start=True)
            if recorder is not None:
                res = await recorder.run_workflow(engine, initial_state, workflow_run_id=run_id)
            else:
                res = await engine.run(initial_state, workflow_run_id=run_id)
            await event_sink.flush()
            await _persist_result(store, run_id, res)
        except asyncio.CancelledError:
            if run_id in cancelling_run_ids(app_state):
                # cancel_run_record() is cancelling this run and records the outcome itself.
                raise
            existing = await store.get_run(run_id)
            if existing and existing.get("status") in (
                "completed",
                "pending_approval",
                "waiting_for_input",
                "failed",
                "rejected",
                "cancelled",
                "interrupted",
            ):
                raise
            if await _journal_decides(app_state, run_id):
                # A6 S9: the run lease decides. This process released it when the task was
                # cancelled, so the run is interrupted unless another worker holds it now.
                await reconcile_quietly(
                    app_state,
                    run_ids=[run_id],
                    error=SHUTDOWN_ERROR,
                    reason=SHUTDOWN_REASON,
                    stopped_here=True,
                )
                raise
            # Background task was interrupted during execution (e.g. by app shutdown)
            await _record_event(
                store,
                run_id,
                "RUN_INTERRUPTED",
                data={"reason": "Run interrupted by server shutdown; background task cancelled."},
            )
            await store.update_run(
                run_id=run_id,
                status="interrupted",
                current_node_id=existing.get("current_node_id") if existing else None,
                current_state=existing.get("current_state", {}) if existing else {},
                step_count=existing.get("step_count", 0) if existing else 0,
                error="Run was interrupted by server shutdown.",
                unless_status=_CANCEL_TERMINAL_STATUSES,
            )
            raise
        except Exception as exc:
            await event_sink.flush()
            await _record_execution_failure(store, run_id, exc)
        finally:
            await event_sink.flush()
            await settle_failed_run_capture(app_state, run_id, workspace_id)
            active_tasks = getattr(app_state, "active_run_tasks", None)
            if active_tasks is not None and active_tasks.get(run_id) is asyncio.current_task():
                active_tasks.pop(run_id, None)

    if async_execution:
        # Background execution
        task = asyncio.create_task(_execute_job())
        if not hasattr(app_state, "active_run_tasks"):
            app_state.active_run_tasks = {}
        app_state.active_run_tasks[run_id] = task
        task.add_done_callback(lambda _t: _clear_stop(app_state, run_id))

        # Return snapshot immediately
        snapshot = await store.get_run(run_id)
        assert snapshot is not None
        if run_budget is not None:
            snapshot = dict(snapshot)
            snapshot["budget_status"] = run_budget.to_dict()["status"]
        return _redact_events(snapshot, app_state)

    # Synchronous execution, as a task a cancel or revocation can stop (B40)
    await run_in_request(app_state, run_id, _execute_job)
    snapshot = await store.get_run(run_id)
    assert snapshot is not None
    if run_budget is not None:
        snapshot = dict(snapshot)
        snapshot["budget_status"] = run_budget.to_dict()["status"]
    return _redact_events(snapshot, app_state)


@router.get("/{run_id}", response_model=RunResponse)
async def get_run(request: Request, run_id: str) -> dict[str, Any]:
    """Retrieve full run snapshot and event history, enforcing workspace isolation."""
    store: StudioStore = request.app.state.store
    run = await store.get_run(run_id)
    if run is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail={"code": "RUN_NOT_FOUND", "message": f"Run '{run_id}' not found."},
        )
    _assert_run_ownership(request, run_id)
    _assert_run_resource(request, run)
    if run["status"] in ("running", "interrupted"):
        # A6 S9: the status comes from the run's journal lease, not from who opened the
        # database last: a run another live process executes is never shown interrupted.
        live_again = await reconcile_quietly(request.app.state, run_ids=[run_id])
        run = show_live(await store.get_run(run_id) or run, live_again)
    run = _redact_events(run, request.app.state)
    run = dict(run)
    run["has_subworkflows"] = _spec_has_subworkflows(run.get("spec"))
    run.pop("spec", None)
    budget_mgr = _budget_manager(request.app.state)
    if budget_mgr is not None:
        rb = budget_mgr.get_run_budget(run_id)
        if rb is not None:
            run["budget_status"] = rb.to_dict()["status"]
    return run


def _spec_has_subworkflows(spec: Any) -> bool:
    """True when any node of a run's spec embeds a sub-workflow (also parallel/map over one)."""
    nodes = spec.get("nodes") if isinstance(spec, dict) else None
    if not isinstance(nodes, dict):
        return False
    return any(isinstance(n, dict) and n.get("subgraph") for n in nodes.values())


def _child_key(parent_id: str, child_id: str) -> str:
    """Opaque URL-safe key for a child run id of ``parent_id``."""
    suffix = child_id[len(parent_id) + 1 :]
    return base64.urlsafe_b64encode(suffix.encode()).decode().rstrip("=")


@router.post("/{run_id}/cancel")
async def cancel_run(
    request: Request, run_id: str, payload: CancelRunRequest | None = None
) -> dict[str, Any]:
    """Explicitly cancel an active run and halt running background execution."""
    caller_ws, principal, role = _get_caller_context(request)
    if principal:
        try:
            check_permission(principal, role, "run")
        except PermissionDeniedError as exc:
            raise HTTPException(
                status_code=http_status.HTTP_403_FORBIDDEN,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc

    store: StudioStore = request.app.state.store
    run = await store.get_run(run_id)
    if run is None:
        if principal is not None and principal.resources is not None:
            # A resource-scoped key learns nothing about runs it did not start.
            raise HTTPException(
                status_code=http_status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "RESOURCE_SCOPE_DENIED",
                    "message": "This API key can only act on runs it started.",
                },
            )
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail={"code": "RUN_NOT_FOUND", "message": f"Run '{run_id}' not found."},
        )
    _assert_run_ownership(request, run_id)
    _assert_run_resource(request, run)

    current_status = run["status"]
    if current_status in _CANCEL_TERMINAL_STATUSES:
        raise _already_terminal(run_id, current_status)

    reason = payload.reason if payload and payload.reason else "Cancelled by user"
    updated = await cancel_run_record(request.app.state, run_id, reason=reason)
    if updated is None:
        # The run finished while its background task was being stopped.
        latest = await store.get_run(run_id)
        raise _already_terminal(run_id, latest["status"] if latest else current_status)
    return _redact_events(updated, request.app.state)


#: Statuses a run cannot be cancelled from.
_CANCEL_TERMINAL_STATUSES = frozenset({"completed", "failed", "rejected", "cancelled"})


def _already_terminal(run_id: str, status: str) -> HTTPException:
    return HTTPException(
        status_code=http_status.HTTP_409_CONFLICT,
        detail={
            "code": "RUN_ALREADY_TERMINAL",
            "message": f"Run '{run_id}' already in terminal state '{status}'.",
        },
    )


def cancelling_run_ids(app_state: Any) -> set[str]:
    """Run ids whose background task is being stopped by :func:`cancel_run_record`."""
    ids: set[str] | None = getattr(app_state, "cancelling_run_ids", None)
    if ids is None:
        ids = set()
        app_state.cancelling_run_ids = ids
    return ids


def _execution_tasks(app_state: Any) -> dict[str, set[asyncio.Task[Any]]]:
    """Engine calls running inside a request (sync start, decide, input, fork), per run."""
    tasks: dict[str, set[asyncio.Task[Any]]] | None = getattr(
        app_state, "run_execution_tasks", None
    )
    if tasks is None:
        tasks = {}
        app_state.run_execution_tasks = tasks
    return tasks


def _cancel_settled(app_state: Any) -> dict[str, asyncio.Event]:
    """Set once the :func:`cancel_run_record` in progress for a run has written its result."""
    events: dict[str, asyncio.Event] | None = getattr(app_state, "run_cancel_settled", None)
    if events is None:
        events = {}
        app_state.run_cancel_settled = events
    return events


def _stopped_run_ids(app_state: Any) -> set[str]:
    """Runs cancelled from inside their own execution; they stop before their next node."""
    ids: set[str] | None = getattr(app_state, "stopped_run_ids", None)
    if ids is None:
        ids = set()
        app_state.stopped_run_ids = ids
    return ids


def _stop_check(app_state: Any, run_id: str) -> Callable[[], bool]:
    return lambda: run_id in _stopped_run_ids(app_state)


def _clear_stop(app_state: Any, run_id: str) -> None:
    """Forget a run's stop flag once nothing executes it any more."""
    active = getattr(app_state, "active_run_tasks", {}).get(run_id)
    if (active is None or active.done()) and not _execution_tasks(app_state).get(run_id):
        _stopped_run_ids(app_state).discard(run_id)


async def _settled_run(app_state: Any, run_id: str) -> dict[str, Any] | None:
    """The run row once any cancel in progress for it has been written (B40).

    Executions call this right before the engine runs, after their last awaited write, so
    a cancel or revocation that landed meanwhile is seen.
    """
    settled = _cancel_settled(app_state).get(run_id)
    if settled is not None:
        await settled.wait()
    run: dict[str, Any] | None = await app_state.store.get_run(run_id)
    return run


async def _settled_status(app_state: Any, run_id: str) -> str | None:
    settled = _cancel_settled(app_state).get(run_id)
    if settled is not None:
        await settled.wait()
    status: str | None = await app_state.store.get_run_status(run_id)
    return status


async def run_in_request[T](
    app_state: Any, run_id: str, work: Callable[[], Coroutine[Any, Any, T]]
) -> T | None:
    """Run a request's engine call as a task that a cancel or revocation can stop (B40).

    The task is registered for ``run_id`` before it starts, so :func:`cancel_run_record`
    cancels it like a background run. Returns the work's result, or None when the run was
    cancelled (once the cancel has been recorded). Exceptions of the work are re-raised; a
    cancelled request cancels the work too.
    """
    task: asyncio.Task[T] = asyncio.create_task(work())
    _execution_tasks(app_state).setdefault(run_id, set()).add(task)

    def finished(done: asyncio.Task[Any]) -> None:
        tasks = _execution_tasks(app_state)
        live = tasks.get(run_id)
        if live is not None:
            live.discard(done)
            if not live:
                tasks.pop(run_id, None)
        _clear_stop(app_state, run_id)

    task.add_done_callback(finished)
    try:
        await asyncio.wait({task})
    except asyncio.CancelledError:
        task.cancel()
        await asyncio.wait({task}, timeout=5.0)
        raise
    if task.cancelled():
        settled = _cancel_settled(app_state).get(run_id)
        if settled is not None:
            await settled.wait()
        return None
    return task.result()


async def cancel_run_record(
    app_state: Any,
    run_id: str,
    *,
    reason: str,
    cancel_reason: str | None = None,
    wait_seconds: float = 5.0,
) -> dict[str, Any] | None:
    """Stop a run's background task and mark the run cancelled.

    The one cancel path, shared by ``POST /runs/{id}/cancel`` and access revocation
    (:mod:`nexura_runtime.service.api.run_revocation`). The task is cancelled and awaited
    (up to ``wait_seconds``) before the run row is written, so its own cancellation handler
    cannot overwrite the result. ``cancel_reason`` (``access_revoked``,
    ``publication_revoked``) is stored on the run and returned by ``GET /runs/{id}``.

    Engine calls running inside a request (see :func:`run_in_request`) are stopped the same
    way. When the cancel runs inside the run's own execution (an in-process call made by one
    of its nodes) that task cannot wait for itself; it is flagged instead and stops before
    its next node. The final write is a compare-and-set, so a run that finished meanwhile
    keeps its result.

    Returns the updated snapshot, or None when the run is missing or already terminal.
    """
    store: StudioStore = app_state.store
    cancelling = cancelling_run_ids(app_state)
    cancelling.add(run_id)
    settled_events = _cancel_settled(app_state)
    settled = settled_events.get(run_id)
    owns_settled = settled is None
    if settled is None:
        settled = settled_events[run_id] = asyncio.Event()
    try:
        # Synchronously with registering ``settled``: an execution registered after this
        # point waits for ``settled`` and then sees the cancelled row.
        active_tasks: dict[str, asyncio.Task[Any]] = getattr(app_state, "active_run_tasks", {})
        background = active_tasks.pop(run_id, None)
        tasks = {t for t in _execution_tasks(app_state).pop(run_id, set()) if not t.done()}
        if background is not None and not background.done():
            tasks.add(background)
        current = asyncio.current_task()
        if current in tasks:
            tasks.discard(current)
            _stopped_run_ids(app_state).add(run_id)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=wait_seconds)
        run = await store.get_run(run_id)
        if run is None or run["status"] in _CANCEL_TERMINAL_STATUSES:
            return None
        data: dict[str, Any] = {"reason": reason}
        if cancel_reason:
            data["cancel_reason"] = cancel_reason
        await _record_event(store, run_id, "RUN_CANCELLED", data=data)
        written = await store.update_run(
            run_id=run_id,
            status="cancelled",
            current_node_id=run["current_node_id"],
            current_state=run["current_state"],
            step_count=run["step_count"],
            error=f"Cancelled: {reason}",
            cancel_reason=cancel_reason,
            unless_status=_CANCEL_TERMINAL_STATUSES,
        )
        if not written:
            return None
        return await store.get_run(run_id)
    finally:
        cancelling.discard(run_id)
        if owns_settled:
            settled.set()
            if settled_events.get(run_id) is settled:
                settled_events.pop(run_id, None)


def _refuse_if_pending_lost(run: Mapping[str, Any] | None, expected: str) -> None:
    """Refuse to go on when the run left ``expected`` meanwhile (cancelled, revoked, decided)."""
    if run is None or run.get("status") == expected:
        return
    _refuse_if_revoked(run)
    raise HTTPException(
        status_code=http_status.HTTP_409_CONFLICT,
        detail={
            "code": (
                "NOT_PENDING_APPROVAL"
                if expected == "pending_approval"
                else "NOT_WAITING_FOR_INPUT"
            ),
            "message": (
                f"Run '{run['run_id']}' is no longer '{expected}' (status: '{run['status']}')."
            ),
        },
    )


def _refuse_if_revoked(run: Mapping[str, Any]) -> None:
    """A run cancelled because access ended can never be approved or answered."""
    if run.get("status") == "cancelled" and run.get("cancel_reason"):
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail={
                "code": "RUN_ACCESS_REVOKED",
                "message": (
                    f"Run '{run['run_id']}' was cancelled because access ended "
                    f"({run['cancel_reason']}); it cannot be resumed."
                ),
            },
        )


@router.post("/{run_id}/decide")
async def decide_run(request: Request, run_id: str, payload: DecisionRequest) -> dict[str, Any]:
    """Submit a decision for an approval gate, enforcing roles and tenant isolation."""
    caller_ws, principal, role = _get_caller_context(request)
    if principal:
        try:
            check_permission(principal, role, "approve")
        except PermissionDeniedError as exc:
            raise HTTPException(
                status_code=http_status.HTTP_403_FORBIDDEN,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc

    store: StudioStore = request.app.state.store
    run = await store.get_run(run_id)
    if run is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail={"code": "RUN_NOT_FOUND", "message": f"Run '{run_id}' not found."},
        )
    _assert_run_ownership(request, run_id)

    _refuse_if_revoked(run)
    if run["status"] != "pending_approval":
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail={
                "code": "NOT_PENDING_APPROVAL",
                "message": f"Run '{run_id}' not waiting for approval (status: '{run['status']}').",
            },
        )

    pending_dict = run["pending_approval"]
    if not pending_dict or pending_dict.get("approval_id") != payload.approval_id:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail={
                "code": "APPROVAL_NOT_FOUND",
                "message": f"Approval '{payload.approval_id}' not found for run '{run_id}'.",
            },
        )

    if payload.decision == "approve":
        _scan_references(request, caller_ws, payload.state_update)

    bound_spec = WorkflowSpec.from_dict(run["spec"])
    enforce_approval_policy(request, bound_spec)
    engine, event_sink = _build_engine(request, bound_spec, run_id, caller_ws)

    # Decision events are a pre-dispatch boundary: access-revocation hooks must run
    # before the engine claims or executes anything. Record them outside the tracked
    # execution task so a nested revocation can settle without cancelling itself.
    # The event records the submitted decision; engine.resume remains authoritative
    # for claiming it, and competing decisions can still be refused below.
    await _record_event(
        store,
        run_id,
        "APPROVAL_GRANTED" if payload.decision == "approve" else "APPROVAL_REJECTED",
        data={"approval_id": payload.approval_id, "state_update": payload.state_update},
    )

    engine_finished = False

    async def resume() -> None:
        nonlocal engine_finished
        # Re-check after the decision event and any revocation it triggered have settled.
        _refuse_if_pending_lost(await _settled_run(request.app.state, run_id), "pending_approval")
        recorder = capture_recorder(request.app.state, run_id)
        if recorder is not None:
            try:
                res = await recorder.resume_workflow(
                    run_id,
                    payload.approval_id,
                    decision=payload.decision,
                    state_update=payload.state_update if payload.decision == "approve" else None,
                    engine=engine,
                )
            except (ApprovalAlreadyClaimedError, WorkflowTerminalError):
                # Another request decided first: this recording no longer describes the run.
                request.app.state.repro_capture.discard(run_id)
                raise
        else:
            res = await engine.resume(
                workflow_run_id=run_id,
                approval_id=payload.approval_id,
                decision=payload.decision,
                state_update=payload.state_update if payload.decision == "approve" else None,
            )
        engine_finished = True
        await event_sink.flush()
        await _persist_result(store, run_id, res)

    try:
        await run_in_request(request.app.state, run_id, resume)
    except HTTPException:
        raise
    except ApprovalAlreadyClaimedError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail={"code": "APPROVAL_ALREADY_CLAIMED", "message": str(exc)},
        ) from exc
    except WorkflowTerminalError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail={"code": "RUN_ALREADY_TERMINAL", "message": str(exc)},
        ) from exc

    except Exception as exc:
        await event_sink.flush()
        await _record_execution_failure(store, run_id, exc)
    await settle_failed_run_capture(request.app.state, run_id, caller_ws)

    updated = await store.get_run(run_id)
    assert updated is not None
    if not engine_finished:
        # A revoke can cancel the tracked task while the engine is awaiting its claim.
        # Surface that refusal too, rather than reporting a successful decision.
        _refuse_if_revoked(updated)
    return _redact_events(updated, request.app.state)


def _require_permission(
    request: Request, permission: Literal["run", "approve"]
) -> tuple[str, Principal | None]:
    caller_ws, principal, role = _get_caller_context(request)
    if principal:
        try:
            check_permission(principal, role, permission)
        except PermissionDeniedError as exc:
            raise HTTPException(
                status_code=http_status.HTTP_403_FORBIDDEN,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
    return caller_ws, principal


async def _get_owned_run(request: Request, run_id: str) -> dict[str, Any]:
    run = await request.app.state.store.get_run(run_id)
    if run is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail={"code": "RUN_NOT_FOUND", "message": f"Run '{run_id}' not found."},
        )
    _assert_run_ownership(request, run_id)
    return run  # type: ignore[no-any-return]


def _budget_manager(app_state: Any) -> Any:
    """Restore named run policies and spend from the service's durable SQLite store."""
    from nexura_agents.usage.ledger import UsageLedger
    from nexura_agents.usage.manager import BudgetManager

    manager = getattr(app_state, "budget_manager", None)
    if manager is None:
        path = getattr(app_state.store, "db_path", None)
        manager = BudgetManager(ledger=UsageLedger(str(path) if path else None))
        app_state.budget_manager = manager
    return manager


def _build_engine(
    request: Request, spec: WorkflowSpec, run_id: str, workspace_id: str
) -> tuple[WorkflowEngine, RunStoreEventSink]:
    return _engine_for(request.app.state, spec, run_id, workspace_id)


def _engine_for(
    app_state: Any, spec: WorkflowSpec, run_id: str, workspace_id: str
) -> tuple[WorkflowEngine, RunStoreEventSink]:
    """The engine (and its event sink) that continues an existing service run."""
    store: StudioStore = app_state.store
    approval_handlers: frozenset[str] = getattr(
        app_state, "approval_required_handlers", frozenset()
    )
    event_sink = RunStoreEventSink(store, run_id)
    budget_mgr = _budget_manager(app_state)
    run_budget = budget_mgr.get_run_budget(run_id) if budget_mgr is not None else None
    engine = WorkflowEngine(
        spec=spec,
        registry=RunEventRegistry(
            app_state.registry,
            store,
            run_id,
            stop_requested=_stop_check(app_state, run_id),
        ),
        store=store.checkpoint_store,
        workspace_id=workspace_id,
        budget=run_budget,
        event_sink=event_sink,
        approval_required_handlers=approval_handlers,
        lease_ttl=workflow_lease_ttl(app_state),
    )
    return engine, event_sink


def _cross_workspace_error(exc: Exception) -> HTTPException:
    return HTTPException(
        status_code=http_status.HTTP_403_FORBIDDEN,
        detail={"code": "CROSS_WORKSPACE_ACCESS_DENIED", "message": str(exc)},
    )


def _scan_references(request: Request, workspace_id: str, value: Any) -> None:
    """Refuse values that reference another workspace's resources (same check as POST /runs)."""
    if not value:
        return
    try:
        ResourceScopeEnforcer(request.app.state.workspace_repo).scan_and_validate_references(
            workspace_id, value
        )
    except CrossWorkspaceAccessError as exc:
        raise _cross_workspace_error(exc) from exc


@router.post("/{run_id}/input")
async def provide_run_input(
    request: Request, run_id: str, payload: ProvideInputRequest
) -> dict[str, Any]:
    """Answer an interrupt() question so a run in waiting_for_input continues."""
    caller_ws, _ = _require_permission(request, "approve")
    store: StudioStore = request.app.state.store
    run = await _get_owned_run(request, run_id)

    _refuse_if_revoked(run)
    if run["status"] != "waiting_for_input":
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail={
                "code": "NOT_WAITING_FOR_INPUT",
                "message": f"Run '{run_id}' is not waiting for input (status: '{run['status']}').",
            },
        )
    pending = run.get("pending_input")
    if not pending or pending.get("interrupt_id") != payload.interrupt_id:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail={
                "code": "INTERRUPT_NOT_FOUND",
                "message": f"Interrupt '{payload.interrupt_id}' not found for run '{run_id}'.",
            },
        )

    _scan_references(request, caller_ws, payload.value)
    bound_spec = WorkflowSpec.from_dict(run["spec"])
    enforce_approval_policy(request, bound_spec)
    engine, event_sink = _build_engine(request, bound_spec, run_id, caller_ws)

    async def record_answer() -> None:
        await _record_event(
            store,
            run_id,
            "INPUT_PROVIDED",
            node_id=pending.get("node_id"),
            data={
                "interrupt_id": payload.interrupt_id,
                "value": redact_run_content(payload.value),
            },
        )

    async def resume() -> None:
        # Checked again right before the run continues (B40).
        _refuse_if_pending_lost(await _settled_run(request.app.state, run_id), "waiting_for_input")
        res = await engine.resume_interrupt(
            run_id, payload.interrupt_id, payload.value, workspace_id=caller_ws
        )
        await record_answer()
        await event_sink.flush()
        await _persist_result(store, run_id, res)

    try:
        await run_in_request(request.app.state, run_id, resume)
    except HTTPException:
        raise
    except ApprovalAlreadyClaimedError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail={"code": "ALREADY_ANSWERED", "message": str(exc)},
        ) from exc
    except (WrongWorkspaceError, CrossWorkspaceAccessError) as exc:
        raise _cross_workspace_error(exc) from exc
    except WorkflowTerminalError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail={"code": "RUN_ALREADY_TERMINAL", "message": str(exc)},
        ) from exc
    except WorkflowSpecMismatchError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail={"code": "SPEC_MISMATCH", "message": str(exc)},
        ) from exc
    except ApprovalError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail={"code": "INTERRUPT_NOT_FOUND", "message": str(exc)},
        ) from exc
    except Exception as exc:
        await event_sink.flush()
        await _record_execution_failure(store, run_id, exc)

    updated = await store.get_run(run_id)
    assert updated is not None
    return _redact_events(updated, request.app.state)


#: Studio statuses ``POST /runs/{id}/recover`` continues from (``paused``: an effect_unknown pause).
_RECOVERABLE_STATUSES = frozenset({"running", "interrupted", "paused"})


def _conflict(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=http_status.HTTP_409_CONFLICT, detail={"code": code, "message": message}
    )


async def _recover_execution(
    app_state: Any,
    run_id: str,
    engine: WorkflowEngine,
    event_sink: RunStoreEventSink,
    on_uncertain: Literal["pause", "fail"],
) -> None:
    """Continue ``run_id`` through ``engine.recover`` and record the result on its row.

    Shared by ``POST /runs/{id}/recover`` and the trigger worker (A6 S10). Refusals raised
    before anything ran (lease held, spec or definition mismatch) restore the row's status.
    """
    store: StudioStore = app_state.store
    current = await _settled_run(app_state, run_id)
    if current is None or current["status"] not in _RECOVERABLE_STATUSES:
        if current is not None:
            _refuse_if_revoked(current)
        raise _conflict(
            "RUN_STATUS_CHANGED",
            f"Run '{run_id}' changed while recovering "
            f"(status: '{current['status'] if current else 'missing'}').",
        )
    # This process executes the run from now on (its owner for the B46 fallback).
    await store.update_run(
        run_id=run_id,
        status="running",
        current_node_id=current["current_node_id"],
        current_state=current["current_state"],
        step_count=current["step_count"],
        unless_status=_CANCEL_TERMINAL_STATUSES,
    )
    try:
        res = await engine.recover(run_id, on_uncertain=on_uncertain)
    except (_LeaseBusyError, WorkflowSpecMismatchError, DefinitionMismatchError):
        # Refused before anything ran: the run keeps the status it had.
        await store.update_run(
            run_id=run_id,
            status=current["status"],
            current_node_id=current["current_node_id"],
            current_state=current["current_state"],
            step_count=current["step_count"],
            error=current.get("error"),
            pending_effect=current.get("pending_effect"),
            unless_status=_CANCEL_TERMINAL_STATUSES,
        )
        raise
    except WorkflowTerminalError:
        # The checkpoint finished before the process stopped; only the run row lagged.
        checkpoint = await store.checkpoint_store.get(run_id)
        if checkpoint is None:
            raise
        res = engine._result_from_checkpoint(checkpoint)
    await event_sink.flush()
    await _persist_result(store, run_id, res)


async def recover_workflow_run(
    app_state: Any,
    *,
    run_id: str,
    workspace_id: str,
    on_uncertain: Literal["pause", "fail"] = "pause",
    event_data: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Continue a journaled service run whose process stopped; return the updated run row.

    Used by the trigger worker when it reclaims a job (A6 S10): the job's run continues
    under its run lease with the run's own bound spec, exactly as
    ``POST /runs/{id}/recover`` does. The caller checks first that the run is ``running``
    or ``interrupted``, has an execution journal and that its lease is not live. Raises
    ``_LeaseBusyError`` while another worker holds the run lease, and
    ``WorkflowSpecMismatchError`` / ``DefinitionMismatchError`` when the stored run does not
    match its spec; any other failure is recorded on the run (``failed``), like the route.
    """
    store: StudioStore = app_state.store
    run = await store.get_run(run_id)
    if run is None:
        raise WorkflowError(f"Run '{run_id}' not found.")
    bound_spec = WorkflowSpec.from_dict(run["spec"])
    engine, event_sink = _engine_for(app_state, bound_spec, run_id, workspace_id)
    await _record_event(
        store,
        run_id,
        "RUN_RECOVERY_STARTED",
        node_id=run.get("current_node_id"),
        data={
            "previous_status": run["status"],
            "on_uncertain": on_uncertain,
            **dict(event_data or {}),
        },
    )

    async def recover() -> None:
        await _recover_execution(app_state, run_id, engine, event_sink, on_uncertain)

    try:
        await run_in_request(app_state, run_id, recover)
    except (_LeaseBusyError, WorkflowSpecMismatchError, DefinitionMismatchError, HTTPException):
        raise
    except Exception as exc:
        await event_sink.flush()
        await _record_execution_failure(store, run_id, exc)
    updated = await store.get_run(run_id)
    assert updated is not None
    return _redact_events(updated, app_state)


@router.post("/{run_id}/recover")
async def recover_run(
    request: Request, run_id: str, payload: RecoverRunRequest | None = None
) -> dict[str, Any]:
    """Continue a run whose process stopped, or an ``effect_unknown`` pause a person resolved.

    Calls ``WorkflowEngine.recover`` under the run's lease (Execution Contract v1, A6 S9).
    A node that never dispatched runs; one that dispatched without a recorded outcome runs
    again only when it is ``pure`` or ``idempotent`` (with the same idempotency key) or a
    ``reconcilable`` probe says it did not happen; a ``non_idempotent`` one pauses the run
    (status ``paused``, ``pause_reason`` ``effect_unknown``, ``pending_effect``) until a
    person resolves the effect (``POST /runs/{id}/effects/{effect_id}/resolve``) and calls
    this again. ``on_uncertain="fail"`` fails the run instead of pausing.

    Needs the ``run`` permission. 409 ``RUN_LEASE_HELD`` while another live process executes
    the run, ``RUN_WAITING_FOR_PERSON`` for a run waiting for an approval or an answer,
    ``RUN_ALREADY_TERMINAL`` for a finished run and ``RUN_NOT_JOURNALED`` for a run without
    an execution journal (recorded before journaled runs: fork it from a checkpoint).
    """
    caller_ws, _ = _require_permission(request, "run")
    app_state = request.app.state
    store: StudioStore = app_state.store
    run = await _get_owned_run(request, run_id)
    _assert_run_resource(request, run)
    _refuse_if_revoked(run)
    status = run["status"]
    if status in _CANCEL_TERMINAL_STATUSES:
        raise _already_terminal(run_id, status)
    waiting = status == "paused" and not run.get("pending_effect")
    if status not in _RECOVERABLE_STATUSES or waiting:
        raise _conflict(
            "RUN_WAITING_FOR_PERSON",
            f"Run '{run_id}' is waiting for a person (status: '{status}'); decide or answer it.",
        )
    journal = await run_journal(app_state)
    snapshot = None
    if journal is not None:
        try:
            snapshot = await journal.get(ExecutionId(caller_ws, run_id))
        except KeyError:
            snapshot = None
    if snapshot is None:
        raise _conflict(
            "RUN_NOT_JOURNALED",
            f"Run '{run_id}' has no execution journal, so what its last step did is unknown; "
            "it cannot be recovered safely. Fork it from a checkpoint instead.",
        )
    if status == "running":
        rows = await store.list_journaled_runs(("running",), run_ids=[run_id]) or [
            {"run_id": run_id, "journal_workspace": caller_ws, "owner_process": None}
        ]
        verdict = (await run_liveness(journal, rows)).get(run_id)
        if verdict == LIVE:
            raise _conflict(
                "RUN_LEASE_HELD",
                f"Run '{run_id}' is being executed by a live process (its run lease is held).",
            )

    on_uncertain = payload.on_uncertain if payload is not None else "pause"
    bound_spec = WorkflowSpec.from_dict(run["spec"])
    enforce_approval_policy(request, bound_spec)
    engine, event_sink = _build_engine(request, bound_spec, run_id, caller_ws)
    await _record_event(
        store,
        run_id,
        "RUN_RECOVERY_STARTED",
        node_id=run.get("current_node_id"),
        data={"previous_status": status, "on_uncertain": on_uncertain},
    )

    async def recover() -> None:
        await _recover_execution(app_state, run_id, engine, event_sink, on_uncertain)

    try:
        await run_in_request(app_state, run_id, recover)
    except HTTPException:
        raise
    except _LeaseBusyError as exc:
        raise _conflict("RUN_LEASE_HELD", str(exc)) from exc
    except (WorkflowSpecMismatchError, DefinitionMismatchError) as exc:
        raise _conflict("SPEC_MISMATCH", str(exc)) from exc
    except (WrongWorkspaceError, CrossWorkspaceAccessError) as exc:
        raise _cross_workspace_error(exc) from exc
    except Exception as exc:
        await event_sink.flush()
        await _record_execution_failure(store, run_id, exc)

    updated = await store.get_run(run_id)
    assert updated is not None
    return _redact_events(updated, app_state)


@router.get("/{run_id}/repro")
async def download_repro_bundle(request: Request, run_id: str) -> JSONResponse:
    """Download the repro bundle captured when this run failed (opt-in capture).

    Replay it offline with ``nexura-runtime repro run FILE``. The bundle was redacted
    when it was written; review it before sharing.
    """
    caller_ws, _, _ = _get_caller_context(request)
    run = await _get_owned_run(request, run_id)
    _assert_run_resource(request, run)
    capture = getattr(request.app.state, "repro_capture", None)
    path = getattr(request.app.state.store, "db_path", None)
    found = (
        capture.load(run_id)
        if capture is not None
        else (load_captured_bundle(path, run_id) if path else None)
    )
    if found is None or found[0] != caller_ws:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail={
                "code": "REPRO_BUNDLE_NOT_FOUND",
                "message": (
                    f"No repro bundle was captured for run '{run_id}'. Bundles are written "
                    "only for runs that fail while failed-run capture is on."
                ),
            },
        )
    filename = "".join(c if c.isalnum() or c in "-_." else "_" for c in run_id)
    return JSONResponse(
        content=found[1],
        headers={"Content-Disposition": f'attachment; filename="{filename}.repro.json"'},
    )


@router.get("/{run_id}/history")
async def run_history(request: Request, run_id: str) -> dict[str, Any]:
    """Every checkpoint of a run, oldest first, with state redacted."""
    caller_ws, _, _ = _get_caller_context(request)
    run = await _get_owned_run(request, run_id)
    engine = WorkflowEngine(
        spec=WorkflowSpec.from_dict(run["spec"]),
        registry=request.app.state.registry,
        store=request.app.state.store.checkpoint_store,
        workspace_id=caller_ws,
    )
    try:
        records = await engine.history(run_id)
    except CrossWorkspaceAccessError as exc:
        raise _cross_workspace_error(exc) from exc
    return {"items": [redact_run_content(checkpoint_to_dict(cp)) for cp in records]}


@router.get("/{run_id}/subruns")
async def run_subruns(
    request: Request,
    run_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """One page of the latest checkpoint of every child run started by sub-workflow nodes.

    Children are in natural id order (numeric parts compare as numbers: ``[2]`` before ``[10]``).
    """
    caller_ws, _, _ = _get_caller_context(request)
    run = await _get_owned_run(request, run_id)
    _assert_run_resource(request, run)
    checkpoint_store = request.app.state.store.checkpoint_store
    all_children = await checkpoint_store.list_children(run_id)
    total = len(all_children)
    children = all_children[offset : offset + limit]
    items = []
    for cp in children:
        if cp.workspace_id is not None and cp.workspace_id != caller_ws:
            raise _cross_workspace_error(
                CrossWorkspaceAccessError(
                    f"Run '{cp.workflow_run_id}' belongs to another workspace."
                )
            )
        item = redact_run_content(checkpoint_to_dict(cp))
        item["child_key"] = _child_key(run_id, cp.workflow_run_id)
        items.append(item)
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + limit < total,
    }


@router.get("/{run_id}/subruns/{child_key}")
async def run_subrun(request: Request, run_id: str, child_key: str) -> dict[str, Any]:
    """One child run: status, steps and redacted checkpoint history (key from /subruns)."""
    caller_ws, _, _ = _get_caller_context(request)
    run = await _get_owned_run(request, run_id)
    _assert_run_resource(request, run)
    checkpoint_store = request.app.state.store.checkpoint_store
    match = None
    for cp in await checkpoint_store.list_children(run_id):
        if _child_key(run_id, cp.workflow_run_id) == child_key:
            match = cp
            break
    if match is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail={
                "code": "RUN_NOT_FOUND",
                "message": f"Sub-run '{child_key}' not found.",
            },
        )
    if match.workspace_id is not None and match.workspace_id != caller_ws:
        raise _cross_workspace_error(
            CrossWorkspaceAccessError(
                f"Run '{match.workflow_run_id}' belongs to another workspace."
            )
        )
    records = await checkpoint_store.history(match.workflow_run_id)
    history = [redact_run_content(checkpoint_to_dict(cp)) for cp in records]
    steps = [
        {
            "index": i,
            "step_count": cp["step_count"],
            "node_id": cp["current_node_id"],
            "status": cp["status"],
            "updated_at": cp["updated_at"],
        }
        for i, cp in enumerate(history)
    ]
    latest = redact_run_content(checkpoint_to_dict(match))
    return {
        "child_key": child_key,
        "run_id": match.workflow_run_id,
        "parent_run_id": run["run_id"],
        "status": latest["status"],
        "step_count": latest["step_count"],
        "current_node_id": latest["current_node_id"],
        "error": latest["error"],
        "steps": steps,
        "history": {"items": history},
    }


@router.post("/{run_id}/fork", status_code=http_status.HTTP_201_CREATED)
async def fork_run(request: Request, run_id: str, payload: ForkRunRequest) -> dict[str, Any]:
    """Start a new run from a past checkpoint of this run, optionally changing its state."""
    caller_ws, principal = _require_permission(request, "run")
    # Before the first await: access ending while this request runs stops the fork (B40).
    access_ended = _access_ended_check(request.app.state, caller_ws, principal)
    store: StudioStore = request.app.state.store
    ws_repo: WorkspaceRepository = request.app.state.workspace_repo
    source = await _get_owned_run(request, run_id)

    _scan_references(request, caller_ws, payload.state_update)
    spec = WorkflowSpec.from_dict(source["spec"])
    enforce_approval_policy(request, spec)
    new_id = f"run-{uuid.uuid4().hex[:12]}"
    engine, event_sink = _build_engine(request, spec, new_id, caller_ws)

    # Validate before creating a run record so a bad request leaves nothing behind.
    try:
        records = await engine.history(run_id)
    except CrossWorkspaceAccessError as exc:
        raise _cross_workspace_error(exc) from exc
    except WorkflowError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={"code": "FORK_FAILED", "message": str(exc)},
        ) from exc
    index = payload.checkpoint_index
    if not records or not -len(records) <= index < len(records):
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "INVALID_CHECKPOINT_INDEX",
                "message": (
                    f"Checkpoint index {index} is out of range; run '{run_id}' has "
                    f"{len(records)} checkpoints."
                ),
            },
        )
    chosen = records[index]
    if chosen.spec_hash != spec.canonical_hash():
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail={
                "code": "SPEC_MISMATCH",
                "message": f"Checkpoint {index} was recorded for a different workflow spec.",
            },
        )
    if chosen.current_node_id is None:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "FORK_AT_END_OF_RUN",
                "message": "That checkpoint is the end of the run; pick an earlier checkpoint.",
            },
        )

    normalized = index if index >= 0 else len(records) + index
    start_state = {**chosen.state, **(payload.state_update or {})}
    await store.record_run_start(
        run_id=new_id,
        workflow_id=source.get("workflow_id"),
        spec=spec,
        initial_state=start_state,
        forked_from={
            "workflow_run_id": run_id,
            "checkpoint_index": normalized,
            "step_count": chosen.step_count,
            "state_changed": bool(payload.state_update),
        },
        started_by=_started_by(
            principal.principal_id if principal else "local-owner", _api_key_id(principal)
        ),
        journal_workspace=journal_workspace(request.app.state, caller_ws),
    )
    principal_id = principal.principal_id if principal else "local-owner"
    ws_repo.record_resource_ownership("run", new_id, caller_ws, principal_id)
    await _cancel_if_access_ended(request.app.state, new_id, access_ended)
    await _record_event(
        store,
        new_id,
        "RUN_FORKED",
        data={
            "source_run_id": run_id,
            "checkpoint_index": normalized,
            "state_update": redact_run_content(payload.state_update),
        },
    )

    async def run_fork() -> None:
        if await _settled_status(request.app.state, new_id) == "cancelled":
            return  # cancelled (revoked) right after it was recorded (B40)
        res = await engine.fork(run_id, index, state_update=payload.state_update, new_run_id=new_id)
        await event_sink.flush()
        await _persist_result(store, new_id, res)

    try:
        await run_in_request(request.app.state, new_id, run_fork)
    except Exception as exc:
        await event_sink.flush()
        await _record_execution_failure(store, new_id, exc)
        if isinstance(exc, CrossWorkspaceAccessError):
            raise _cross_workspace_error(exc) from exc
        if isinstance(exc, WorkflowSpecMismatchError):
            raise HTTPException(
                status_code=http_status.HTTP_409_CONFLICT,
                detail={"code": "SPEC_MISMATCH", "message": str(exc)},
            ) from exc
        if isinstance(exc, WorkflowError):
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST,
                detail={"code": "FORK_FAILED", "message": str(exc)},
            ) from exc

    snapshot = await store.get_run(new_id)
    assert snapshot is not None
    return _redact_events(snapshot, request.app.state)


@router.get("/{run_id}/events")
async def stream_run_events(
    request: Request,
    run_id: str,
    cursor: int = Query(default=0, ge=0, description="Resume sequence cursor"),
    live: bool = Query(default=False, description="Stream live events until terminal state"),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    """Stream lifecycle events via SSE with replay cursor, enforcing tenant ownership."""
    store: StudioStore = request.app.state.store
    run = await store.get_run(run_id)
    if run is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail={"code": "RUN_NOT_FOUND", "message": f"Run '{run_id}' not found."},
        )
    _assert_run_ownership(request, run_id)
    _assert_run_resource(request, run)

    # Determine effective resume cursor (Header takes precedence over query if valid integer)
    effective_cursor = cursor
    if last_event_id and last_event_id.isdigit():
        effective_cursor = int(last_event_id)

    run = _redact_events(run, request.app.state)

    async def event_generator() -> AsyncIterator[str]:
        last_sent_seq = effective_cursor
        all_events = run.get("events", [])
        pending_events = [ev for ev in all_events if ev.get("seq", 0) > effective_cursor]

        for ev in pending_events:
            seq = ev.get("seq", 0)
            if seq > last_sent_seq:
                last_sent_seq = seq
            ev_id = ev.get("seq")
            ev_type = ev.get("type", "message")
            ev_dict = dict(ev.get("data", {}))
            if "node_id" not in ev_dict and ev.get("node_id") is not None:
                ev_dict["node_id"] = ev.get("node_id")
            ev_data = json.dumps(ev_dict)
            yield f"id: {ev_id}\nevent: {ev_type}\ndata: {ev_data}\n\n"

        if not live:
            return

        # Poll store for newly emitted events until run terminates
        max_poll_iterations = 600
        iteration = 0
        while iteration < max_poll_iterations:
            await asyncio.sleep(0.1)
            iteration += 1
            fresh_run = await store.get_run(run_id)
            if fresh_run is None:
                break
            fresh_run = _redact_events(fresh_run, request.app.state)
            for ev in fresh_run.get("events", []):
                seq = ev.get("seq", 0)
                if seq > last_sent_seq:
                    last_sent_seq = seq
                    ev_type = ev.get("type", "message")
                    ev_dict = dict(ev.get("data", {}))
                    if "node_id" not in ev_dict and ev.get("node_id") is not None:
                        ev_dict["node_id"] = ev.get("node_id")
                    ev_data = json.dumps(ev_dict)
                    yield f"id: {seq}\nevent: {ev_type}\ndata: {ev_data}\n\n"

            if fresh_run.get("status") in ("completed", "failed", "rejected", "cancelled"):
                close_data = json.dumps({"status": fresh_run["status"]})
                yield f"event: RUN_STREAM_CLOSED\ndata: {close_data}\n\n"
                break

    return StreamingResponse(event_generator(), media_type="text/event-stream")
