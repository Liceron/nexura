# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""API endpoints for workflow specification CRUD and validation."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from nexura_agents.errors import WorkflowValidationError
from pydantic import BaseModel, Field

from nexura_runtime.studio.store import StudioStore
from nexura_runtime.workflow import ModuleRegistry, WorkflowSpec

router = APIRouter(prefix="/workflows", tags=["workflows"])


class CreateWorkflowRequest(BaseModel):
    workflow_id: str | None = Field(default=None, description="Optional custom workflow identifier")
    spec: dict[str, Any] = Field(description="WorkflowSpec dictionary definition")


class UpdateWorkflowRequest(BaseModel):
    expected_revision: int = Field(description="Expected current revision number for CAS update")
    spec: dict[str, Any] = Field(description="Updated WorkflowSpec dictionary definition")


class ValidateWorkflowRequest(BaseModel):
    spec: dict[str, Any] = Field(description="WorkflowSpec dictionary definition to validate")


@router.get("", response_model=dict[str, list[dict[str, Any]]])
async def list_workflows(request: Request) -> dict[str, list[dict[str, Any]]]:
    """List all persisted workflow specifications."""
    store: StudioStore = request.app.state.store
    workflows = await store.list_workflows()
    return {"workflows": workflows}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_workflow(request: Request, payload: CreateWorkflowRequest) -> dict[str, Any]:
    """Create and persist a new workflow specification."""
    store: StudioStore = request.app.state.store
    registry: ModuleRegistry = request.app.state.registry

    try:
        spec = WorkflowSpec.from_dict(payload.spec)
        spec.validate(registry)
    except Exception as exc:
        raise WorkflowValidationError(f"Invalid workflow specification: {exc}") from exc

    workflow = await store.create_workflow(spec, workflow_id=payload.workflow_id)
    return workflow


@router.get("/{workflow_id}")
async def get_workflow(request: Request, workflow_id: str) -> dict[str, Any]:
    """Retrieve a specific workflow definition by ID."""
    store: StudioStore = request.app.state.store
    wf = await store.get_workflow(workflow_id)
    if wf is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "WORKFLOW_NOT_FOUND",
                "message": f"Workflow '{workflow_id}' not found.",
            },
        )
    return wf


@router.put("/{workflow_id}")
async def update_workflow(
    request: Request, workflow_id: str, payload: UpdateWorkflowRequest
) -> dict[str, Any]:
    """Update an existing workflow definition using expected revision checks."""
    store: StudioStore = request.app.state.store
    registry: ModuleRegistry = request.app.state.registry

    try:
        spec = WorkflowSpec.from_dict(payload.spec)
        spec.validate(registry)
    except Exception as exc:
        raise WorkflowValidationError(f"Invalid workflow specification: {exc}") from exc

    updated = await store.update_workflow(
        workflow_id=workflow_id,
        spec=spec,
        expected_revision=payload.expected_revision,
    )
    return updated


@router.post("/validate")
async def validate_workflow(request: Request, payload: ValidateWorkflowRequest) -> dict[str, Any]:
    """Validate graph topology, transitions, and module bindings against the registry."""
    registry: ModuleRegistry = request.app.state.registry
    try:
        spec = WorkflowSpec.from_dict(payload.spec)
        spec.validate(registry)
    except Exception as exc:
        raise WorkflowValidationError(str(exc)) from exc

    return {
        "valid": True,
        "name": spec.name,
        "entry_node": spec.entry_node,
        "node_count": len(spec.nodes),
        "spec_hash": spec.canonical_hash(),
    }
