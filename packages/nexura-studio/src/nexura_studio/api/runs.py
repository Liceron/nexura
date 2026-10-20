# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""API endpoints for starting, inspecting, and approving workflow execution runs."""

from __future__ import annotations

import asyncio
import copy
import inspect
import uuid
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, status
from nexura_agents.errors import (
    ApprovalAlreadyClaimedError,
    WorkflowTerminalError,
    WorkflowValidationError,
)
from pydantic import BaseModel, Field

from nexura_runtime.studio.store import StudioStore
from nexura_runtime.studio.traces import capture_agent_events
from nexura_runtime.workflow import (
    HandlerOutput,
    ModuleRegistry,
    WorkflowEngine,
    WorkflowSpec,
)

router = APIRouter(prefix="/runs", tags=["runs"])


class StartRunRequest(BaseModel):
    workflow_id: str | None = Field(default=None, description="Persisted workflow ID to execute")
    spec: dict[str, Any] | None = Field(default=None, description="Ad-hoc WorkflowSpec definition")
    initial_state: dict[str, Any] = Field(default_factory=dict, description="Initial state inputs")


class DecisionRequest(BaseModel):
    approval_id: str = Field(description="Pending approval ID to decide")
    decision: Literal["approve", "reject"] = Field(
        default="approve", description="Decision: 'approve' to proceed or 'reject' to abort"
    )
    state_update: dict[str, Any] | None = Field(
        default=None, description="Optional state updates applied on approval"
    )


def _parse_output_for_event(raw: Any) -> tuple[dict[str, Any], str | None]:
    if isinstance(raw, HandlerOutput):
        return raw.state_update, raw.branch
    if isinstance(raw, dict):
        d = dict(raw)
        branch = d.pop("_branch", None)
        return d, branch
    return {}, None


class RunEventRegistry(ModuleRegistry):
    """Wrapper ModuleRegistry that intercepts node handler execution to emit lifecycle events."""

    def __init__(self, inner: ModuleRegistry, store: StudioStore, run_id: str) -> None:
        super().__init__()
        self._inner = inner
        self._store = store
        self._run_id = run_id

    def has_handler(self, name: str) -> bool:
        return self._inner.has_handler(name)

    def get(self, name: str) -> Any:
        fn = self._inner.get(name)

        async def wrapped(state: dict[str, Any]) -> Any:
            await self._store.record_run_event(
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
                await self._store.record_run_event(
                    self._run_id,
                    "NODE_FAILED",
                    node_id=name,
                    data={"error": str(exc)},
                )
                raise

            state_update, branch = _parse_output_for_event(out)
            await self._store.record_run_event(
                self._run_id,
                "NODE_COMPLETED",
                node_id=name,
                data={"state_update": state_update, "branch": branch},
            )
            return out

        return wrapped


_INPUT_UNSUPPORTED = (
    "This workflow asked for human input with interrupt() at node '{node}', which the "
    "local Studio server cannot answer. Run it with `nexura-runtime serve` (POST "
    "/api/v1/runs/{{run_id}}/input) or the SDK's WorkflowEngine.resume_interrupt()."
)


async def _fail_unanswerable_input(store: StudioStore, run_id: str, result: Any) -> None:
    """Record a run paused by interrupt() as failed, with a message saying where to answer it."""
    node = (result.interrupt or {}).get("node_id", result.current_node_id)
    message = _INPUT_UNSUPPORTED.format(node=node)
    await store.record_run_event(
        run_id=run_id, event_type="RUN_FAILED", node_id=node, data={"error": message}
    )
    await store.update_run(
        run_id=run_id,
        status="failed",
        current_node_id=result.current_node_id,
        current_state=result.state,
        step_count=result.step_count,
        error=message,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def start_run(request: Request, payload: StartRunRequest) -> dict[str, Any]:
    """Start a new workflow execution run."""
    store: StudioStore = request.app.state.store
    base_registry: ModuleRegistry = request.app.state.registry

    # 1. Resolve workflow specification
    if payload.spec is not None:
        try:
            spec = WorkflowSpec.from_dict(payload.spec)
            spec.validate(base_registry)
        except Exception as exc:
            raise WorkflowValidationError(f"Invalid workflow specification: {exc}") from exc
        workflow_id = payload.workflow_id
    elif payload.workflow_id is not None:
        wf_record = await store.get_workflow(payload.workflow_id)
        if wf_record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "WORKFLOW_NOT_FOUND",
                    "message": f"Workflow '{payload.workflow_id}' not found.",
                },
            )
        spec = WorkflowSpec.from_dict(wf_record["spec"])
        spec.validate(base_registry)
        workflow_id = payload.workflow_id
    else:
        raise WorkflowValidationError("Must provide either 'workflow_id' or 'spec'.")

    run_id = f"run-{uuid.uuid4().hex[:12]}"
    initial_state = copy.deepcopy(payload.initial_state)

    # 2. Bind run snapshot to original spec in StudioStore
    await store.record_run_start(
        run_id=run_id,
        workflow_id=workflow_id,
        spec=spec,
        initial_state=initial_state,
    )

    # 3. Create engine with checkpoint store and event recording registry
    event_registry = RunEventRegistry(base_registry, store, run_id)
    engine = WorkflowEngine(
        spec=spec,
        registry=event_registry,
        store=store.checkpoint_store,
    )

    try:
        result = await engine.run(initial_state, workflow_run_id=run_id)

        if result.status == "paused" and result.pending_approval is None:
            await _fail_unanswerable_input(store, run_id, result)
        elif result.status == "paused":
            assert result.pending_approval is not None
            await store.record_run_event(
                run_id=run_id,
                event_type="APPROVAL_REQUIRED",
                node_id=result.pending_approval.node_id,
                data={
                    "approval_id": result.pending_approval.approval_id,
                    "input_state": result.pending_approval.input_state,
                },
            )
            await store.update_run(
                run_id=run_id,
                status="paused",
                current_node_id=result.current_node_id,
                current_state=result.state,
                step_count=result.step_count,
                pending_approval=result.pending_approval,
            )
        elif result.status == "completed":
            await store.record_run_event(
                run_id=run_id,
                event_type="RUN_COMPLETED",
                node_id=result.current_node_id,
                data={"final_state": result.state, "step_count": result.step_count},
            )
            await store.update_run(
                run_id=run_id,
                status="completed",
                current_node_id=result.current_node_id,
                current_state=result.state,
                step_count=result.step_count,
            )
        else:
            await store.update_run(
                run_id=run_id,
                status=result.status,
                current_node_id=result.current_node_id,
                current_state=result.state,
                step_count=result.step_count,
                error=result.error,
            )

    except Exception as exc:
        err_msg = str(exc)
        await store.record_run_event(
            run_id=run_id,
            event_type="RUN_FAILED",
            node_id=None,
            data={"error": err_msg},
        )
        await store.update_run(
            run_id=run_id,
            status="failed",
            current_node_id=None,
            current_state=initial_state,
            step_count=0,
            error=err_msg,
        )
        raise

    run_record = await store.get_run(run_id)
    assert run_record is not None
    return run_record


@router.get("", response_model=dict[str, list[dict[str, Any]]])
async def list_runs(
    request: Request,
    workflow_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, list[dict[str, Any]]]:
    """List execution runs, optionally filtered by workflow ID."""
    store: StudioStore = request.app.state.store
    runs = await store.list_runs(workflow_id=workflow_id, limit=limit)
    return {"runs": runs}


@router.get("/{run_id}")
async def get_run(request: Request, run_id: str) -> dict[str, Any]:
    """Retrieve full details and event logs for a specific run."""
    store: StudioStore = request.app.state.store
    run_record = await store.get_run(run_id)
    if run_record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "RUN_NOT_FOUND", "message": f"Run '{run_id}' not found."},
        )
    return run_record


@router.post("/{run_id}/decide")
async def decide_approval(
    request: Request, run_id: str, payload: DecisionRequest
) -> dict[str, Any]:
    """Approve or reject a pending approval request for a paused run."""
    store: StudioStore = request.app.state.store
    base_registry: ModuleRegistry = request.app.state.registry

    run_record = await store.get_run(run_id)
    if run_record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "RUN_NOT_FOUND", "message": f"Run '{run_id}' not found."},
        )

    if run_record["status"] in ("completed", "rejected", "failed", "interrupted"):
        raise WorkflowTerminalError(
            f"Cannot resume run '{run_id}' with terminal status '{run_record['status']}'."
        )

    if run_record["status"] != "paused":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "RUN_NOT_PAUSED",
                "message": (
                    f"Run '{run_id}' is not paused for approval (status: {run_record['status']})."
                ),
            },
        )

    # Bind strictly to original run's spec snapshot
    bound_spec = WorkflowSpec.from_dict(run_record["spec"])
    bound_spec.validate(base_registry)

    # Record decision event
    await store.record_run_event(
        run_id=run_id,
        event_type="APPROVAL_DECISION",
        node_id=run_record.get("current_node_id"),
        data={
            "approval_id": payload.approval_id,
            "decision": payload.decision,
            "state_update": payload.state_update or {},
        },
    )

    event_registry = RunEventRegistry(base_registry, store, run_id)
    engine = WorkflowEngine(
        spec=bound_spec,
        registry=event_registry,
        store=store.checkpoint_store,
    )

    try:
        result = await engine.resume(
            workflow_run_id=run_id,
            approval_id=payload.approval_id,
            decision=payload.decision,
            state_update=payload.state_update,
        )

        if result.status == "rejected":
            await store.record_run_event(
                run_id=run_id,
                event_type="RUN_REJECTED",
                node_id=result.current_node_id,
                data={"error": result.error, "state": result.state},
            )
            await store.update_run(
                run_id=run_id,
                status="rejected",
                current_node_id=result.current_node_id,
                current_state=result.state,
                step_count=result.step_count,
                pending_approval=None,
                error=result.error,
            )
        elif result.status == "completed":
            await store.record_run_event(
                run_id=run_id,
                event_type="RUN_COMPLETED",
                node_id=result.current_node_id,
                data={"final_state": result.state, "step_count": result.step_count},
            )
            await store.update_run(
                run_id=run_id,
                status="completed",
                current_node_id=result.current_node_id,
                current_state=result.state,
                step_count=result.step_count,
                pending_approval=None,
            )
        elif result.status == "paused" and result.pending_approval is None:
            await _fail_unanswerable_input(store, run_id, result)
        elif result.status == "paused":
            assert result.pending_approval is not None
            await store.record_run_event(
                run_id=run_id,
                event_type="APPROVAL_REQUIRED",
                node_id=result.pending_approval.node_id,
                data={
                    "approval_id": result.pending_approval.approval_id,
                    "input_state": result.pending_approval.input_state,
                },
            )
            await store.update_run(
                run_id=run_id,
                status="paused",
                current_node_id=result.current_node_id,
                current_state=result.state,
                step_count=result.step_count,
                pending_approval=result.pending_approval,
            )
        else:
            await store.update_run(
                run_id=run_id,
                status=result.status,
                current_node_id=result.current_node_id,
                current_state=result.state,
                step_count=result.step_count,
                error=result.error,
            )

    except ApprovalAlreadyClaimedError:
        # Re-raise so exception handler maps to 409 Conflict
        raise
    except Exception as exc:
        err_msg = str(exc)
        await store.record_run_event(
            run_id=run_id,
            event_type="RUN_FAILED",
            node_id=None,
            data={"error": err_msg},
        )
        await store.update_run(
            run_id=run_id,
            status="failed",
            current_node_id=None,
            current_state=run_record["current_state"],
            step_count=run_record["step_count"],
            error=err_msg,
        )
        raise

    updated_run = await store.get_run(run_id)
    assert updated_run is not None
    return updated_run


# Alias route: POST /runs/{run_id}/approve calls the same decision handler
@router.post("/{run_id}/approve")
async def approve_run(request: Request, run_id: str, payload: DecisionRequest) -> dict[str, Any]:
    """Alias for POST /runs/{run_id}/decide with approval decision."""
    return await decide_approval(request, run_id, payload)
