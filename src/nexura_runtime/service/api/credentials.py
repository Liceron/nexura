# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Workspace-scoped credential references, availability, and verification API.

Guarantees:
- Secrets are NEVER returned in response payloads, error details, or metadata.
- Cross-workspace isolation is strictly enforced; credential ids are scoped per workspace.
- Revoked credentials fail closed on resolution.
- Role-based permissions enforced server-side.
"""

from __future__ import annotations

import re
import time
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from nexura_runtime.credentials.errors import (
    CredentialAccessDeniedError,
    CredentialAlreadyExistsError,
    CredentialNotFoundError,
)
from nexura_runtime.credentials.store import ScopedCredentialStore
from nexura_runtime.service.api.models import PaginatedResponse
from nexura_runtime.service.auth.errors import PermissionDeniedError
from nexura_runtime.service.auth.models import Principal, WorkspaceRole
from nexura_runtime.service.auth.permissions import check_permission
from nexura_runtime.service.workspaces.repository import WorkspaceRepository

router = APIRouter(prefix="/credentials", tags=["credentials"])


class CreateCredentialRequest(BaseModel):
    """Payload to register a new credential reference."""

    credential_id: str = Field(description="Unique credential reference identifier")
    credential_type: str = Field(
        description="Type kind e.g. api_key, oauth, bearer_token, database_url"
    )
    provider: str = Field(
        default="custom", description="Service provider e.g. openai, anthropic, whatsapp, custom"
    )
    description: str = Field(default="", description="Descriptive label or scope")
    secret: str = Field(
        description="Transient secret value; immediately secured in memory/vault, never returned"
    )
    environment_variable: str | None = Field(
        default=None, description="Optional environment variable fallback"
    )


class VerifyCredentialResponse(BaseModel):
    """Result of a credential check made on this server (never with the provider).

    ``stored``: the credential exists, is not revoked, and its secret can be read and is not
    empty. Nobody asked the provider, so a wrong or expired key also passes
    (``checked_with_provider`` is always false). ``invalid``: the secret cannot be read or is
    empty. ``revoked``: it was revoked and fails closed.
    """

    credential_id: str
    status: Literal["stored", "invalid", "revoked"]
    message: str
    checked_at: float
    checked_with_provider: bool = False


def _enforce_perm(principal: Principal, role: WorkspaceRole, permission: Any) -> None:
    try:
        check_permission(principal, role, permission)
    except PermissionDeniedError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "PERMISSION_DENIED", "message": str(exc)},
        ) from exc


def _not_found(credential_id: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={
            "code": "CREDENTIAL_NOT_FOUND",
            "message": f"Credential '{credential_id}' not found.",
        },
    )


def _assert_visible(repo: WorkspaceRepository, caller_ws: str, credential_id: str) -> None:
    """Reject ids that a legacy ownership record assigns to another workspace (read-only)."""
    owner_ws = repo.get_resource_workspace("credential", credential_id)
    if owner_ws and owner_ws != caller_ws:
        raise _not_found(credential_id)


def _get_caller_context(request: Request) -> tuple[str, Principal, WorkspaceRole]:
    """Extract authoritative workspace, principal, and role from request state."""
    caller_ws: str = getattr(request.state, "workspace_id", "default")
    principal: Principal = getattr(
        request.state,
        "principal",
        Principal(principal_id="local-owner", profile="local-owner"),
    )
    # Unrecognised roles narrow to "viewer", which grants the same empty permission set.
    raw_role = getattr(request.state, "role", "admin")
    role: WorkspaceRole = (
        "admin" if raw_role == "admin" else ("editor" if raw_role == "editor" else "viewer")
    )
    return caller_ws, principal, role


def _get_credential_store(request: Request) -> ScopedCredentialStore:
    store = getattr(request.app.state, "credential_store", None)
    if store is None:
        store = ScopedCredentialStore()
        request.app.state.credential_store = store
    return store


@router.get("", response_model=PaginatedResponse[dict[str, Any]])
async def list_credentials(
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> PaginatedResponse[dict[str, Any]]:
    """List metadata for all credentials belonging to the caller's active workspace.

    Guarantees: Zero secret values are exposed in any field.
    """
    caller_ws, principal, role = _get_caller_context(request)
    _enforce_perm(principal, role, "edit")
    store = _get_credential_store(request)

    all_metas = store.list_credentials(caller_ws)
    total = len(all_metas)
    page_metas = all_metas[offset : offset + limit]

    items = [
        {
            "credential_id": m.credential_id,
            "workspace_id": m.workspace_id,
            "credential_type": m.credential_type,
            "provider": getattr(m, "provider", "custom"),
            "description": m.description,
            "environment_variable": m.environment_variable,
            "status": getattr(m, "status", "active"),
            "created_at": m.created_at,
            "last_verified_at": getattr(m, "last_verified_at", None),
            "masked_hint": getattr(m, "masked_hint", ""),
        }
        for m in page_metas
    ]

    return PaginatedResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        has_more=(offset + limit) < total,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_credential(
    request: Request,
    payload: CreateCredentialRequest,
) -> dict[str, Any]:
    """Register a new credential reference in caller's workspace.

    Viewer role is denied with 403.
    """
    caller_ws, principal, role = _get_caller_context(request)
    _enforce_perm(principal, role, "edit")

    # Validate ID format
    cid = payload.credential_id.strip()
    if not cid or not re.match(r"^[a-zA-Z0-9_\-\.]+$", cid):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_CREDENTIAL_ID", "message": "Invalid credential_id format."},
        )

    if not payload.secret or not payload.secret.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "EMPTY_SECRET", "message": "Secret cannot be empty."},
        )

    store = _get_credential_store(request)

    # Check if credential already exists in workspace
    try:
        existing = store.get_metadata(caller_ws, cid)
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "CREDENTIAL_ALREADY_EXISTS",
                    "message": f"Credential '{cid}' already exists.",
                },
            )
    except CredentialNotFoundError:
        pass

    # Store credential in isolated scoped store. Create-only, atomically in the backend: of two
    # instances creating one name at once, one gets 201 and the other 409 (MVP WP5 review M2).
    try:
        meta = store.store(
            workspace_id=caller_ws,
            credential_id=cid,
            credential_type=payload.credential_type,
            secret=payload.secret,
            description=payload.description,
            environment_variable=payload.environment_variable,
            provider=payload.provider,
            status="active",
            created_by=principal.principal_id,
            mode="create",
        )
    except CredentialAlreadyExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "CREDENTIAL_ALREADY_EXISTS",
                "message": f"Credential '{cid}' already exists.",
            },
        ) from exc

    # Return metadata ONLY — NEVER return secret
    return {
        "credential_id": meta.credential_id,
        "workspace_id": meta.workspace_id,
        "credential_type": meta.credential_type,
        "provider": getattr(meta, "provider", "custom"),
        "description": meta.description,
        "environment_variable": meta.environment_variable,
        "status": getattr(meta, "status", "active"),
        "created_at": meta.created_at,
        "last_verified_at": getattr(meta, "last_verified_at", None),
        "masked_hint": getattr(meta, "masked_hint", ""),
    }


@router.get("/{credential_id}")
async def get_credential(
    request: Request,
    credential_id: str,
) -> dict[str, Any]:
    """Retrieve metadata for a specific credential in caller's workspace.

    Guarantees: Zero secret values are exposed.
    """
    caller_ws, principal, role = _get_caller_context(request)
    _enforce_perm(principal, role, "edit")
    store = _get_credential_store(request)

    # Verify workspace resource ownership
    try:
        meta = store.get_metadata(caller_ws, credential_id)
    except CredentialNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "CREDENTIAL_NOT_FOUND",
                "message": f"Credential '{credential_id}' not found.",
            },
        ) from exc

    return {
        "credential_id": meta.credential_id,
        "workspace_id": meta.workspace_id,
        "credential_type": meta.credential_type,
        "provider": getattr(meta, "provider", "custom"),
        "description": meta.description,
        "environment_variable": meta.environment_variable,
        "status": getattr(meta, "status", "active"),
        "created_at": meta.created_at,
        "last_verified_at": getattr(meta, "last_verified_at", None),
        "masked_hint": getattr(meta, "masked_hint", ""),
    }


@router.delete("/{credential_id}")
async def delete_credential(
    request: Request,
    credential_id: str,
) -> dict[str, Any]:
    """Delete a credential reference from caller's workspace.

    Viewer role is denied with 403.
    """
    caller_ws, principal, role = _get_caller_context(request)
    _enforce_perm(principal, role, "edit")

    store = _get_credential_store(request)

    deleted = store.delete(caller_ws, credential_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "CREDENTIAL_NOT_FOUND",
                "message": f"Credential '{credential_id}' not found.",
            },
        )

    return {"deleted": True, "credential_id": credential_id}


@router.post("/{credential_id}/verify", response_model=VerifyCredentialResponse)
async def verify_credential(
    request: Request,
    credential_id: str,
) -> VerifyCredentialResponse:
    """Check that the stored credential can be used: it exists in this workspace, is not
    revoked, and its secret can be read and is not empty.

    It does not call the provider, so it cannot tell a working key from a wrong one: the
    answer is ``stored`` ("stored and readable; not checked with the provider"), never
    "valid". Writes ``last_verified_at``, so it needs the edit permission.
    """
    caller_ws, principal, role = _get_caller_context(request)
    _enforce_perm(principal, role, "edit")
    store = _get_credential_store(request)

    now = time.time()
    try:
        meta = store.get_metadata(caller_ws, credential_id)
    except CredentialNotFoundError as exc:
        raise _not_found(credential_id) from exc

    if getattr(meta, "status", "active") == "revoked":
        return VerifyCredentialResponse(
            credential_id=credential_id,
            status="revoked",
            message="Credential has been revoked and fails closed.",
            checked_at=now,
        )

    try:
        secret_val = store.resolve(caller_ws, credential_id)
        if not secret_val.reveal():
            return VerifyCredentialResponse(
                credential_id=credential_id,
                status="invalid",
                message="Credential secret is empty.",
                checked_at=now,
            )
    except (CredentialAccessDeniedError, Exception) as exc:
        return VerifyCredentialResponse(
            credential_id=credential_id,
            status="invalid",
            message=str(exc),
            checked_at=now,
        )

    store.mark_verified(caller_ws, credential_id, now)
    return VerifyCredentialResponse(
        credential_id=credential_id,
        status="stored",
        message=(
            "Stored and readable (not empty, not revoked). Not checked with the provider: "
            "a wrong or expired key also passes this check."
        ),
        checked_at=now,
    )


@router.post("/{credential_id}/revoke")
async def revoke_credential(
    request: Request,
    credential_id: str,
) -> dict[str, Any]:
    """Revoke a credential reference, failing all subsequent resolutions closed.

    Admin role required.
    """
    caller_ws, principal, role = _get_caller_context(request)
    _enforce_perm(principal, role, "admin")

    store = _get_credential_store(request)

    try:
        meta = store.revoke(caller_ws, credential_id)
    except CredentialNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "CREDENTIAL_NOT_FOUND",
                "message": f"Credential '{credential_id}' not found.",
            },
        ) from exc

    return {
        "credential_id": meta.credential_id,
        "workspace_id": meta.workspace_id,
        "status": meta.status,
        "revoked": True,
        "message": "Credential revoked successfully.",
    }
