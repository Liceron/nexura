# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Template gallery API: browse starter workflows and create one in the caller's workspace."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from nexura_runtime.credentials.store import ScopedCredentialStore
from nexura_runtime.service.api.workflows import (
    _create_or_conflict,
    _get_caller_context,
    _prepare_spec,
    _require_edit,
)
from nexura_runtime.service.workspaces.repository import WorkspaceRepository
from nexura_runtime.studio.store import StudioStore
from nexura_runtime.templates.catalog import Template, get_template, list_templates
from nexura_runtime.templates.handlers import register_template_handlers

router = APIRouter(prefix="/templates", tags=["templates"])


class InstantiateTemplateRequest(BaseModel):
    """Optional overrides when creating a workflow from a template."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    workflow_id: str | None = Field(default=None, min_length=1, max_length=200)


def _not_found(template_id: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"code": "TEMPLATE_NOT_FOUND", "message": f"Template '{template_id}' not found."},
    )


def _require(template_id: str) -> Template:
    template = get_template(template_id)
    if template is None:
        raise _not_found(template_id)
    return template


@router.get("")
async def list_template_catalog(
    category: Annotated[str | None, Query(description="Only this category")] = None,
    tag: Annotated[str | None, Query(description="Only templates with this tag")] = None,
) -> dict[str, Any]:
    """The catalog (metadata only), optionally filtered, with the facets for building filters."""
    everything = list_templates()
    return {
        "items": [t.summary() for t in list_templates(category, tag)],
        "total": len(list_templates(category, tag)),
        "categories": sorted({t.category for t in everything}),
        "tags": sorted({tag_name for t in everything for tag_name in t.tags}),
    }


@router.get("/{template_id}")
async def get_template_detail(template_id: str) -> dict[str, Any]:
    """One template with its spec, a Mermaid diagram and a sample input."""
    return _require(template_id).detail()


@router.post("/{template_id}/instantiate", status_code=status.HTTP_201_CREATED)
async def instantiate_template(
    request: Request, template_id: str, payload: InstantiateTemplateRequest | None = None
) -> dict[str, Any]:
    """Create a workflow from a template in the caller's workspace.

    Same rules as ``POST /workflows``: editors and admins only, the full create-time
    validation, and ownership recorded for the caller's workspace. Credentials the
    template names but the workspace lacks are reported by name; no secret is ever filled in.
    """
    _require_edit(request)
    template = _require(template_id)
    payload = payload or InstantiateTemplateRequest()
    caller_ws, principal, _ = _get_caller_context(request)

    register_template_handlers(request.app.state.registry)
    spec = _prepare_spec(request, template.new_spec(payload.name), payload.name, caller_ws)
    store: StudioStore = request.app.state.store
    workflow = await _create_or_conflict(request, store, spec, payload.workflow_id)

    ws_repo: WorkspaceRepository = request.app.state.workspace_repo
    ws_repo.record_resource_ownership(
        "workflow",
        workflow["workflow_id"],
        caller_ws,
        principal.principal_id if principal else "local-owner",
    )

    cred_store: ScopedCredentialStore | None = getattr(request.app.state, "credential_store", None)
    present = (
        {m.credential_id for m in cred_store.list_credentials(caller_ws) if m.status == "active"}
        if cred_store is not None
        else set()
    )
    return {
        "workflow": workflow,
        "workflow_id": workflow["workflow_id"],
        "template_id": template.id,
        "template_version": template.template_version,
        "missing_credentials": [c for c in template.required_credentials if c not in present],
        "runs_offline": True,
        "suggested_trigger": template.suggested_trigger,
    }
