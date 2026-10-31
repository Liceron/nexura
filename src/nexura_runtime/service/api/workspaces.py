# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Workspace routes: list, create, read, the promotion-approval policy and scoped listings.

In the open-source edition workspaces are local project folders of the single local owner.
The workspace member routes (members and the caller's own membership) moved to the closed edition
in EDITIONS_PLAN step 7.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from nexura_runtime.service.api.auth_providers import caller_is_operator
from nexura_runtime.service.api.models import PaginatedResponse
from nexura_runtime.service.auth.errors import (
    CrossWorkspaceAccessError,
    PermissionDeniedError,
)
from nexura_runtime.service.auth.models import Principal, WorkspaceRole
from nexura_runtime.service.auth.permissions import check_permission
from nexura_runtime.service.workspaces.membership import remembered_member
from nexura_runtime.service.workspaces.models import WorkspaceMember
from nexura_runtime.service.workspaces.repository import (
    WorkspaceDeletedError,
    WorkspaceExistsError,
    WorkspaceRepository,
)
from nexura_runtime.service.workspaces.scoping import ResourceScopeEnforcer

router = APIRouter(prefix="/workspaces", tags=["workspaces"])
logger = logging.getLogger("nexura.service.workspaces")


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


def checked_workspace_member(request: Request, workspace_id: str) -> WorkspaceMember | None:
    """``check_workspace_access`` and the caller's membership record from one membership read
    (MVP WP11a; a route that needs both used to read it twice, after the authentication read).
    When this request's authentication already read that record (``remember_member``), it is
    used: the same read the request was authorised with.

    Returns the record (``None``: none, which only the local owner may have) or raises the
    403 that ``check_workspace_access`` raises.
    """
    principal: Principal | None = getattr(request.state, "principal", None)
    repo = _get_scope_enforcer(request).workspace_repo
    principal_id = principal.principal_id if principal is not None else "local-owner"
    # The read the authentication of this request made, when it was for this workspace.
    known, member = remembered_member(request.state, workspace_id, principal_id)
    if not known:
        member = repo.get_member(workspace_id, principal_id)
    if (
        member is None
        and principal is not None
        and principal.profile == "authenticated-shared-service"
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "PERMISSION_DENIED",
                "message": (
                    f"Principal '{principal_id}' does not belong to workspace '{workspace_id}'."
                ),
            },
        )
    return member


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
    except WorkspaceDeletedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "WORKSPACE_ID_RETIRED",
                "message": "This identifier belonged to a deleted workspace; choose another.",
            },
        ) from exc
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
    repo.update_workspace_metadata(
        workspace_id, {"require_promotion_approval": payload.require_promotion_approval}
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


class DeleteWorkspaceRequest(BaseModel):
    """Confirmation of a workspace deletion: the workspace id, repeated."""

    confirm: str = Field(default="", max_length=256, description="The workspace id, repeated")


def _not_found(workspace_id: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"code": "WORKSPACE_NOT_FOUND", "message": f"Workspace '{workspace_id}' not found."},
    )


def _deletion_operator(request: Request) -> bool:
    """True for a platform operator (``caller_is_operator``) or a caller an edition's
    ``operators`` hook accepts (the hosted edition: its operator admin API key)."""
    from nexura_runtime.service.workspaces.deletion import deletion_hooks

    if caller_is_operator(request):
        return True
    for check in deletion_hooks(request.app.state).operators:
        try:
            if check(request) is True:
                return True
        except Exception:
            logger.exception("workspace deletion operator check failed")
    return False


@router.delete("/{workspace_id}", status_code=status.HTTP_202_ACCEPTED)
async def delete_workspace(
    request: Request,
    workspace_id: str,
    payload: DeleteWorkspaceRequest | None = Body(default=None),  # noqa: B008
) -> JSONResponse:
    """Delete a workspace for good (P46, ``nexura_runtime.service.workspaces.deletion``).

    Allowed for an admin member of the workspace (a person, not an API key) and for a platform
    operator, who need not be a member. The request authenticates against the caller's own
    workspace (``X-Workspace-ID`` or the credential's), never the one being deleted. The body
    must repeat the id: ``{"confirm": "<workspace_id>"}`` (else 400 ``CONFIRMATION_REQUIRED``).
    ``default``, operator workspaces and workspaces an edition protects answer 409
    ``WORKSPACE_PROTECTED``.

    Access ends in the request: the id is retired (it can never be created again), members,
    API keys, publications, webhooks, schedules and trigger jobs go and every live run is
    cancelled. The content is purged right after, in the background, and again by the trigger
    tick until a pass after the workflow lease TTL marks it done. Answers 202
    ``{"workspace_id", "status": "purging"}``. An already deleted workspace answers 404, except
    to an operator, who gets 202 with its current status (``purging`` or ``deleted``) and, while
    it is still purging, starts another purge pass.
    """
    from nexura_runtime.service.workspaces.deletion import (
        deletion_hooks,
        end_workspace_access,
        protection_reason,
        start_background_purge,
    )

    state = request.app.state
    repo: WorkspaceRepository = _get_scope_enforcer(request).workspace_repo
    principal: Principal = getattr(
        request.state, "principal", Principal(principal_id="local-owner", profile="local-owner")
    )
    operator = _deletion_operator(request)
    if await asyncio.to_thread(repo.is_workspace_deleted, workspace_id):
        if not operator:
            raise _not_found(workspace_id)
        deletion = await asyncio.to_thread(repo.get_workspace_deletion, workspace_id)
        purging = deletion is not None and deletion.status == "purging"
        if purging:
            start_background_purge(state, workspace_id)
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={"workspace_id": workspace_id, "status": "purging" if purging else "deleted"},
        )
    if not operator:
        member = (
            None
            if principal.metadata.get("auth_kind") == "api_key"
            else await asyncio.to_thread(repo.get_member, workspace_id, principal.principal_id)
        )
        if member is None or member.role != "admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "PERMISSION_DENIED",
                    "message": "Deleting a workspace needs an admin of it or a platform operator.",
                },
            )
    if await asyncio.to_thread(repo.get_workspace, workspace_id) is None:
        raise _not_found(workspace_id)
    reason = protection_reason(state, workspace_id)
    if reason is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "WORKSPACE_PROTECTED", "message": reason},
        )
    if payload is None or payload.confirm != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "CONFIRMATION_REQUIRED",
                "message": 'Repeat the workspace id to confirm: {"confirm": "<workspace_id>"}.',
            },
        )
    actor = principal.principal_id
    try:
        deletion, created = await asyncio.to_thread(repo.retire_workspace, workspace_id, actor)
    except KeyError as exc:
        raise _not_found(workspace_id) from exc
    if not created:  # deleted by a concurrent request
        raise _not_found(workspace_id)
    summary = await end_workspace_access(state, workspace_id, actor)
    for hook in deletion_hooks(state).audit:
        await hook(state, workspace_id, actor, deletion)
    logger.warning(
        "workspace %s deleted by %s (operator=%s): %s",
        workspace_id,
        actor,
        operator,
        {k: v for k, v in summary.items() if k != "cancelled_run_ids"}
        | {"cancelled_runs": len(summary.get("cancelled_run_ids", []))},
    )
    start_background_purge(state, workspace_id)
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"workspace_id": workspace_id, "status": "purging"},
    )


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
