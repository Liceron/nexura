# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Workspace routes: list, create, read, the promotion-approval policy and scoped listings.

In the open-source edition workspaces are local project folders of the single local owner.
The workspace member routes (members and the caller's own membership) moved to the closed edition
in EDITIONS_PLAN step 7.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from nexura_runtime.service.api.auth_providers import caller_is_operator
from nexura_runtime.service.api.models import PaginatedResponse
from nexura_runtime.service.auth.errors import (
    CrossWorkspaceAccessError,
    PermissionDeniedError,
)
from nexura_runtime.service.auth.models import Principal, WorkspaceRole
from nexura_runtime.service.auth.permissions import check_permission
from nexura_runtime.service.workspaces.repository import WorkspaceExistsError, WorkspaceRepository
from nexura_runtime.service.workspaces.scoping import ResourceScopeEnforcer

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


class CreateWorkspaceRequest(BaseModel):
    """Payload to create a new workspace."""

    workspace_id: str = Field(
        description="Unique workspace slug or identifier",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:@-]*$",
    )
    name: str = Field(description="Display name for the workspace", max_length=256)
    description: str | None = Field(
        default=None, description="Optional description", max_length=4096
    )


def _enforce_perm(principal: Principal, role: WorkspaceRole, permission: Any) -> None:
    try:
        check_permission(principal, role, permission)
    except PermissionDeniedError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "PERMISSION_DENIED", "message": str(exc)},
        ) from exc


def _get_scope_enforcer(request: Request) -> ResourceScopeEnforcer:
    repo = getattr(request.app.state, "workspace_repo", None)
    if repo is None:
        repo = WorkspaceRepository()
        request.app.state.workspace_repo = repo
    return ResourceScopeEnforcer(repo)


def check_workspace_access(request: Request, workspace_id: str) -> None:
    """Validate that the requesting principal has permission to access the target workspace.

    The local owner (the only principal of the open-source edition) may use every workspace.
    A principal authenticated by a closed-edition provider must be a member of it. No header
    can name a different principal (``X-Principal-*`` trust was removed in EDITIONS_PLAN step 7).
    """
    principal: Principal | None = getattr(request.state, "principal", None)
    if principal is None or principal.profile != "authenticated-shared-service":
        return
    enforcer = _get_scope_enforcer(request)
    try:
        enforcer.enforce_workspace_access(principal, workspace_id)
    except (CrossWorkspaceAccessError, PermissionDeniedError) as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "PERMISSION_DENIED", "message": str(exc)},
        ) from exc


@router.get("", response_model=PaginatedResponse[dict[str, Any]])
async def list_workspaces(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> PaginatedResponse[dict[str, Any]]:
    """List workspaces visible to the caller (all in local mode; memberships in shared mode)."""
    repo: WorkspaceRepository = getattr(request.app.state, "workspace_repo", WorkspaceRepository())
    principal: Principal | None = getattr(request.state, "principal", None)
    visible = repo.list_workspaces()
    if principal is not None and principal.profile == "authenticated-shared-service":
        member_of = {
            w.workspace_id for w in repo.list_workspaces_for_principal(principal.principal_id)
        }
        # The caller's own (already authorised) workspace is always visible.
        member_of.add(getattr(request.state, "workspace_id", ""))
        visible = [ws for ws in visible if ws.workspace_id in member_of]
    all_ws = [
        {"workspace_id": ws.workspace_id, "name": ws.name, "created_at": ws.created_at}
        for ws in visible
    ]
    total = len(all_ws)
    page_items = all_ws[offset : offset + limit]
    return PaginatedResponse(
        items=page_items,
        total=total,
        limit=limit,
        offset=offset,
        has_more=(offset + limit) < total,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_workspace(request: Request, payload: CreateWorkspaceRequest) -> dict[str, Any]:
    """Create a new multi-tenant workspace."""
    repo: WorkspaceRepository = getattr(request.app.state, "workspace_repo", WorkspaceRepository())
    request.app.state.workspace_repo = repo
    meta = {"description": payload.description} if payload.description else {}
    principal = getattr(request.state, "principal", None)
    owner_id = principal.principal_id if principal else "admin_owner"

    # Creating a tenant is a platform-level action: require the admin permission (the
    # local-owner profile is always allowed) and never overwrite an existing workspace.
    if principal is not None:
        _enforce_perm(principal, getattr(request.state, "role", "viewer"), "admin")
        # The installed AuthProvider decides who is an operator. The open-source
        # LocalOwnerAuthProvider makes the local owner the operator; the operator allow-list
        # (operator_principal_ids / NEXURA_OPERATOR_PRINCIPALS) moved to the closed edition.
        if not caller_is_operator(request):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "PERMISSION_DENIED",
                    "message": "Creating workspaces requires a platform operator.",
                },
            )

    try:
        ws = repo.create_workspace(
            workspace_id=payload.workspace_id,
            name=payload.name,
            owner_id=owner_id,
            metadata=meta,
        )
    except WorkspaceExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "WORKSPACE_ALREADY_EXISTS",
                "message": "A workspace with this identifier already exists.",
            },
        ) from exc
    return {
        "workspace_id": ws.workspace_id,
        "name": ws.name,
        "description": ws.metadata.get("description"),
        "created_at": ws.created_at,
    }


@router.get("/{workspace_id}")
async def get_workspace(
    request: Request,
    workspace_id: str,
) -> dict[str, Any]:
    """Retrieve workspace details verifying tenant boundary."""
    check_workspace_access(request, workspace_id)
    repo: WorkspaceRepository = getattr(request.app.state, "workspace_repo", WorkspaceRepository())
    ws = repo.get_workspace(workspace_id)
    if ws is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "WORKSPACE_NOT_FOUND",
                "message": f"Workspace '{workspace_id}' not found.",
            },
        )
    return {
        "workspace_id": ws.workspace_id,
        "name": ws.name,
        "description": ws.metadata.get("description"),
        "created_at": ws.created_at,
    }


class UpdateWorkspaceRequest(BaseModel):
    """Payload to change a workspace policy flag (only this one flag is editable here)."""

    require_promotion_approval: bool = Field(
        description="Prod promotions and prod credential-map changes need a second admin"
    )


@router.patch("/{workspace_id}")
async def update_workspace_policy(
    request: Request,
    workspace_id: str,
    payload: UpdateWorkspaceRequest,
) -> dict[str, Any]:
    """Set or clear ``require_promotion_approval``. Workspace admin only; audited."""
    check_workspace_access(request, workspace_id)
    principal: Principal = getattr(
        request.state, "principal", Principal(principal_id="local-owner", profile="local-owner")
    )
    caller_role: WorkspaceRole = getattr(request.state, "role", "admin")
    _enforce_perm(principal, caller_role, "admin")
    repo: WorkspaceRepository = getattr(request.app.state, "workspace_repo", WorkspaceRepository())
    ws = repo.get_workspace(workspace_id)
    if ws is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "WORKSPACE_NOT_FOUND",
                "message": f"Workspace '{workspace_id}' not found.",
            },
        )
    previous = ws.metadata.get("require_promotion_approval") is True
    ws.metadata["require_promotion_approval"] = payload.require_promotion_approval
    if repo.db_path is not None:
        with sqlite3.connect(str(repo.db_path), check_same_thread=False, timeout=10.0) as conn:
            conn.execute(
                "UPDATE nexura_service_workspaces SET metadata_json = ? WHERE workspace_id = ?",
                (json.dumps(ws.metadata), workspace_id),
            )
    pub_store = getattr(request.app.state, "publication_store", None)
    if pub_store is not None and previous != payload.require_promotion_approval:
        pub_store.add_audit(
            workspace_id,
            "",
            "require_promotion_approval_set",
            principal.principal_id,
            {"from": previous, "to": payload.require_promotion_approval},
        )
    return {
        "workspace_id": ws.workspace_id,
        "name": ws.name,
        "description": ws.metadata.get("description"),
        "created_at": ws.created_at,
        "require_promotion_approval": payload.require_promotion_approval,
    }


# Mount scoped sub-resources into the workspace route matrix
workspace_scoped_router = APIRouter(prefix="/workspaces/{workspace_id}")


@workspace_scoped_router.get("/workflows")
async def list_workspace_workflows(
    request: Request,
    workspace_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> Any:
    check_workspace_access(request, workspace_id)
    request.state.workspace_id = workspace_id
    from nexura_runtime.service.api.workflows import list_workflows

    return await list_workflows(request, limit=limit, offset=offset)


@workspace_scoped_router.get("/runs")
async def list_workspace_runs(
    request: Request,
    workspace_id: str,
    workflow_id: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> Any:
    check_workspace_access(request, workspace_id)
    request.state.workspace_id = workspace_id
    from nexura_runtime.service.api.runs import list_runs

    return await list_runs(
        request, workflow_id=workflow_id, status=None, limit=limit, offset=offset
    )
