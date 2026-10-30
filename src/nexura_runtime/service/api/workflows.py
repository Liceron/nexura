# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
import base64
import io
import json
import os
import tempfile
import zipfile
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from nexura_agents.errors import WorkflowConflictError, WorkflowValidationError
from starlette.responses import StreamingResponse

from nexura_runtime.codegen import CodeImportError, python_to_workflow, workflow_to_python
from nexura_runtime.service.api.export import WorkflowExporter
from nexura_runtime.service.api.models import (
    CreateWorkflowRequest,
    ExportBundleResponse,
    ExportWorkflowRequest,
    ImportWorkflowRequest,
    PaginatedResponse,
    UpdateWorkflowRequest,
)
from nexura_runtime.service.api.policy import enforce_approval_policy
from nexura_runtime.service.api.validation import (
    WorkflowValidateRequest,
    WorkflowValidateResponse,
    validate_workflow_graph,
)
from nexura_runtime.service.auth.errors import (
    CrossWorkspaceAccessError,
    PermissionDeniedError,
)
from nexura_runtime.service.auth.models import Principal, WorkspaceRole
from nexura_runtime.service.auth.permissions import check_permission
from nexura_runtime.service.workspaces.repository import UNOWNED_WORKSPACE, WorkspaceRepository
from nexura_runtime.service.workspaces.scoping import ResourceScopeEnforcer
from nexura_runtime.studio.store import StudioStore
from nexura_runtime.workflow import ModuleRegistry, WorkflowSpec

router = APIRouter(prefix="/workflows", tags=["workflows"])


class ExportBundleResult(ExportBundleResponse):
    """Export response; the bundle path is omitted or root-relative in shared deployments."""

    bundle_path: str | None = None  # type: ignore[assignment]


def _export_root(request: Request) -> Path | None:
    """Optional directory confining server-side exports in either service profile."""
    configured = getattr(request.app.state, "export_root", None) or os.environ.get(
        "NEXURA_EXPORT_ROOT"
    )
    return Path(configured).resolve() if configured else None


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


def _assert_workflow_ownership(request: Request, workflow_id: str) -> None:
    """Verify that the workflow specification belongs to the caller's workspace."""
    caller_ws, principal, _ = _get_caller_context(request)
    ws_repo: WorkspaceRepository = request.app.state.workspace_repo
    owner_ws = ws_repo.get_resource_workspace("workflow", workflow_id)

    # In local-owner mode with unmapped legacy resources, default workspace owns it
    if (
        owner_ws is None
        and principal
        and principal.profile == "local-owner"
        and caller_ws == "default"
    ):
        owner_ws = "default"
        ws_repo.record_resource_ownership(
            "workflow", workflow_id, "default", principal.principal_id
        )

    if owner_ws == UNOWNED_WORKSPACE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "CROSS_WORKSPACE_ACCESS_DENIED",
                "message": "Resource has no workspace owner; access denied.",
            },
        )
    if owner_ws is not None and owner_ws != caller_ws:
        if principal and principal.profile == "local-owner":
            # One person owns every local workspace, so naming the workspace leaks nothing
            # and lets Studio explain why a link opened in another workspace is denied.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "CROSS_WORKSPACE_ACCESS_DENIED",
                    "message": (
                        f"Workflow '{workflow_id}' belongs to a different workspace "
                        f"('{owner_ws}'), not '{caller_ws}'."
                    ),
                },
            )
        # Shared service: identical to the missing-resource response so ids cannot be
        # probed across tenants.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "WORKFLOW_NOT_FOUND",
                "message": f"Workflow '{workflow_id}' not found.",
            },
        )


def _require_edit(request: Request) -> None:
    """Deny read-only principals (viewers) for state-changing or bundle-producing routes."""
    _, principal, role = _get_caller_context(request)
    if principal:
        try:
            check_permission(principal, role, "edit")
        except PermissionDeniedError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc


def _prepare_spec(
    request: Request, raw_spec: dict[str, Any], name: str | None, caller_ws: str
) -> WorkflowSpec:
    """Run the full create-time checks: graph validation, approval policy, workspace references."""
    registry: ModuleRegistry = request.app.state.registry
    ws_repo: WorkspaceRepository = request.app.state.workspace_repo
    approval_handlers: frozenset[str] = getattr(
        request.app.state, "approval_required_handlers", frozenset()
    )
    val_report = validate_workflow_graph(
        raw_spec,
        registry=registry,
        approval_required_handlers=approval_handlers,
    )
    if not val_report.valid:
        first_err = val_report.errors[0]
        raise HTTPException(
            status_code=422,  # literal: the constant was renamed in newer Starlette
            detail={
                "code": "VALIDATION_ERROR",
                "message": f"Invalid spec: {first_err.message}",
                "errors": [e.model_dump() for e in val_report.errors],
                "warnings": [w.model_dump() for w in val_report.warnings],
            },
        )

    try:
        spec = WorkflowSpec.from_dict(raw_spec)
        if name:
            spec.name = name
        enforce_approval_policy(request, spec)
    except Exception as exc:
        raise HTTPException(
            status_code=422,  # literal: the constant was renamed in newer Starlette
            detail={"code": "VALIDATION_ERROR", "message": f"Invalid spec: {exc}"},
        ) from exc

    enforcer = ResourceScopeEnforcer(ws_repo)
    try:
        enforcer.scan_and_validate_references(caller_ws, spec.to_dict())
    except CrossWorkspaceAccessError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "CROSS_WORKSPACE_ACCESS_DENIED", "message": str(exc)},
        ) from exc
    return spec


async def _create_or_conflict(
    request: Request, store: StudioStore, spec: WorkflowSpec, workflow_id: str | None
) -> dict[str, Any]:
    """Create a workflow; never reveal whether another workspace owns a requested id.

    A requested id that already exists answers 409 only when the caller's own workspace
    (or the local owner) holds it. In shared mode an id held by another workspace is
    silently replaced by a server-generated id, so the response is indistinguishable from
    a free id and ids cannot be probed across tenants.
    """
    caller_ws, principal, _ = _get_caller_context(request)
    local_owner = bool(principal and principal.profile == "local-owner")
    try:
        try:
            return await store.create_workflow(spec, workflow_id=workflow_id)
        except WorkflowValidationError as exc:
            if workflow_id is None or "already exists" not in str(exc) or local_owner:
                raise
            ws_repo: WorkspaceRepository = request.app.state.workspace_repo
            if ws_repo.get_resource_workspace("workflow", workflow_id) == caller_ws:
                raise
            return await store.create_workflow(spec, workflow_id=None)
    except WorkflowValidationError as exc:
        if "already exists" in str(exc):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "WORKFLOW_ID_CONFLICT",
                    "message": "A workflow with this ID already exists.",
                },
            ) from exc
        raise HTTPException(
            status_code=422,  # literal: the constant was renamed in newer Starlette
            detail={"code": "VALIDATION_ERROR", "message": f"Invalid spec: {exc}"},
        ) from exc


@router.get("", response_model=PaginatedResponse[dict[str, Any]])
async def list_workflows(
    request: Request,
    query: Annotated[str | None, Query(description="Search filter for workflow name or ID")] = None,
    limit: Annotated[int, Query(ge=1, le=100, description="Items per page")] = 20,
    offset: Annotated[int, Query(ge=0, description="Page offset")] = 0,
) -> PaginatedResponse[dict[str, Any]]:
    """List workflow specifications scoped to caller workspace with pagination and search."""
    store: StudioStore = request.app.state.store
    ws_repo: WorkspaceRepository = request.app.state.workspace_repo
    caller_ws, principal, _ = _get_caller_context(request)

    all_workflows = await store.list_workflows()

    # Filter to workflows belonging to caller's workspace
    scoped_workflows = []
    for wf in all_workflows:
        wf_id = wf.get("workflow_id", "")
        owner_ws = ws_repo.get_resource_workspace("workflow", wf_id)
        if (
            owner_ws is None
            and principal
            and principal.profile == "local-owner"
            and caller_ws == "default"
        ):
            owner_ws = "default"
            ws_repo.record_resource_ownership("workflow", wf_id, "default", principal.principal_id)

        if owner_ws == caller_ws:
            scoped_workflows.append(wf)

    # Optional search filtering
    if isinstance(query, str) and query.strip():
        q_lower = query.strip().lower()
        scoped_workflows = [
            wf
            for wf in scoped_workflows
            if q_lower in wf.get("name", "").lower() or q_lower in wf.get("workflow_id", "").lower()
        ]

    total = len(scoped_workflows)
    page_items = scoped_workflows[offset : offset + limit]
    has_more = (offset + limit) < total

    return PaginatedResponse(
        items=page_items,
        total=total,
        limit=limit,
        offset=offset,
        has_more=has_more,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_workflow(request: Request, payload: CreateWorkflowRequest) -> dict[str, Any]:
    """Create and persist a new workflow specification with workspace ownership."""
    caller_ws, principal, role = _get_caller_context(request)
    if principal:
        try:
            check_permission(principal, role, "edit")
        except PermissionDeniedError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc

    store: StudioStore = request.app.state.store
    ws_repo: WorkspaceRepository = request.app.state.workspace_repo

    spec = _prepare_spec(request, payload.spec, payload.name, caller_ws)

    workflow = await _create_or_conflict(request, store, spec, payload.workflow_id)
    wf_id = workflow["workflow_id"]

    # Record durable workspace ownership
    principal_id = principal.principal_id if principal else "local-owner"
    ws_repo.record_resource_ownership("workflow", wf_id, caller_ws, principal_id)

    return workflow


@router.post("/validate", response_model=WorkflowValidateResponse)
async def validate_workflow(
    request: Request,
    payload: WorkflowValidateRequest,
) -> WorkflowValidateResponse:
    """Authoritatively validate workflow specification graph, mappings, and policies."""
    registry: ModuleRegistry = request.app.state.registry
    approval_handlers: frozenset[str] = getattr(
        request.app.state, "approval_required_handlers", frozenset()
    )
    return validate_workflow_graph(
        payload.spec,
        registry=registry,
        approval_required_handlers=approval_handlers,
    )


@router.get("/{workflow_id}")
async def get_workflow(request: Request, workflow_id: str) -> dict[str, Any]:
    """Retrieve a specific workflow definition by ID, enforcing workspace isolation."""
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

    _assert_workflow_ownership(request, workflow_id)
    _, principal, role = _get_caller_context(request)
    if principal:
        try:
            check_permission(principal, role, "edit")
        except PermissionDeniedError:
            # Read-only members get the same scrubbed view that export would produce
            # (export itself is denied to viewers); editors/admins see the full config.
            wf = {**wf, "spec": WorkflowExporter().sanitize_spec_dict(wf.get("spec"))}
    return wf


@router.put("/{workflow_id}")
async def update_workflow(
    request: Request, workflow_id: str, payload: UpdateWorkflowRequest
) -> dict[str, Any]:
    """Update an existing workflow definition with optimistic concurrency control."""
    caller_ws, principal, role = _get_caller_context(request)
    if principal:
        try:
            check_permission(principal, role, "edit")
        except PermissionDeniedError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc

    store: StudioStore = request.app.state.store
    registry: ModuleRegistry = request.app.state.registry
    ws_repo: WorkspaceRepository = request.app.state.workspace_repo

    # Check existence and ownership before updating
    wf = await store.get_workflow(workflow_id)
    if wf is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "WORKFLOW_NOT_FOUND",
                "message": f"Workflow '{workflow_id}' not found.",
            },
        )
    _assert_workflow_ownership(request, workflow_id)

    approval_handlers: frozenset[str] = getattr(
        request.app.state, "approval_required_handlers", frozenset()
    )
    val_report = validate_workflow_graph(
        payload.spec,
        registry=registry,
        approval_required_handlers=approval_handlers,
    )
    if not val_report.valid:
        first_err = val_report.errors[0]
        raise HTTPException(
            status_code=422,  # literal: the constant was renamed in newer Starlette
            detail={
                "code": "VALIDATION_ERROR",
                "message": f"Invalid spec: {first_err.message}",
                "errors": [e.model_dump() for e in val_report.errors],
                "warnings": [w.model_dump() for w in val_report.warnings],
            },
        )

    try:
        spec = WorkflowSpec.from_dict(payload.spec)
        enforce_approval_policy(request, spec)
    except Exception as exc:
        raise HTTPException(
            status_code=422,  # literal: the constant was renamed in newer Starlette
            detail={"code": "VALIDATION_ERROR", "message": f"Invalid spec: {exc}"},
        ) from exc

    enforcer = ResourceScopeEnforcer(ws_repo)
    try:
        enforcer.scan_and_validate_references(caller_ws, spec.to_dict())
    except CrossWorkspaceAccessError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "CROSS_WORKSPACE_ACCESS_DENIED", "message": str(exc)},
        ) from exc

    try:
        updated = await store.update_workflow(
            workflow_id=workflow_id,
            spec=spec,
            expected_revision=payload.expected_revision,
        )
        return updated
    except WorkflowConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "REVISION_CONFLICT", "message": str(exc)},
        ) from exc


@router.delete("/{workflow_id}", status_code=status.HTTP_200_OK)
async def delete_workflow(request: Request, workflow_id: str) -> dict[str, Any]:
    """Delete a workflow definition, enforcing tenant ownership and permissions."""
    caller_ws, principal, role = _get_caller_context(request)
    if principal:
        try:
            check_permission(principal, role, "edit")
        except PermissionDeniedError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc

    store: StudioStore = request.app.state.store
    ws_repo: WorkspaceRepository = request.app.state.workspace_repo

    wf = await store.get_workflow(workflow_id)
    if wf is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "WORKFLOW_NOT_FOUND",
                "message": f"Workflow '{workflow_id}' not found.",
            },
        )
    _assert_workflow_ownership(request, workflow_id)

    deleted = await store.delete_workflow(workflow_id)
    ws_repo.delete_resource_ownership("workflow", workflow_id)
    return {"deleted": deleted, "workflow_id": workflow_id}


@router.post("/{workflow_id}/export", response_model=ExportBundleResult)
async def export_workflow(
    request: Request,
    workflow_id: str,
    payload: ExportWorkflowRequest | None = None,
) -> ExportBundleResult:
    """Export a workflow to a portable standalone bundle, enforcing tenant ownership."""
    _require_edit(request)
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

    _assert_workflow_ownership(request, workflow_id)

    spec = WorkflowSpec.from_dict(wf["spec"])
    exporter = WorkflowExporter()
    registry = request.app.state.registry
    requested = payload.target_dir if payload else None
    _, principal, _ = _get_caller_context(request)
    local_owner = bool(principal and principal.profile == "local-owner")
    root = _export_root(request)

    if local_owner and root is None:
        # Without an export root, the machine owner may choose a local destination.
        # An explicitly configured root below confines exports in both profiles.
        if requested:
            try:
                target_dir = str(WorkflowExporter.validate_target_dir(requested))
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"code": "PATH_TRAVERSAL_DETECTED", "message": str(exc)},
                ) from exc
        else:
            target_dir = tempfile.mkdtemp(prefix=f"bundle_{workflow_id}_")
        bundle = exporter.export_bundle(
            spec, target_dir=target_dir, workflow_id=workflow_id, registry=registry
        )
        return ExportBundleResult(
            workflow_id=workflow_id,
            spec_hash=bundle.spec_hash,
            bundle_path=str(bundle.target_dir),
            files=bundle.files,
        )

    # Shared services and confined local exports do not reveal absolute server paths.
    if not requested:
        with tempfile.TemporaryDirectory(prefix="nexura_export_") as tmp:
            bundle = exporter.export_bundle(
                spec, target_dir=tmp, workflow_id=workflow_id, registry=registry
            )
        return ExportBundleResult(
            workflow_id=workflow_id,
            spec_hash=bundle.spec_hash,
            bundle_path=None,
            files=bundle.files,
        )

    if root is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "EXPORT_TARGET_NOT_PERMITTED",
                "message": "Server-side export directories are not enabled; use the download.",
            },
        )
    try:
        WorkflowExporter.validate_target_dir(requested)
        candidate = Path(requested)
        dest = (candidate if candidate.is_absolute() else root / candidate).resolve()
        dest.relative_to(root)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "PATH_TRAVERSAL_DETECTED",
                "message": "target_dir must be inside the configured export root.",
            },
        ) from exc
    if dest == root or (dest.exists() and any(dest.iterdir())):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "EXPORT_TARGET_EXISTS",
                "message": "Export target must be a new, empty directory under the export root.",
            },
        )
    bundle = exporter.export_bundle(
        spec, target_dir=dest, workflow_id=workflow_id, registry=registry
    )
    return ExportBundleResult(
        workflow_id=workflow_id,
        spec_hash=bundle.spec_hash,
        bundle_path=dest.relative_to(root).as_posix(),
        files=bundle.files,
    )


@router.get("/{workflow_id}/export/download")
async def download_workflow_export(
    request: Request,
    workflow_id: str,
) -> StreamingResponse:
    """Download the standalone portable workflow bundle as a zip archive."""
    _require_edit(request)
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

    _assert_workflow_ownership(request, workflow_id)

    spec = WorkflowSpec.from_dict(wf["spec"])
    with tempfile.TemporaryDirectory(prefix=f"bundle_dl_{workflow_id}_") as temp_dir:
        exporter = WorkflowExporter()
        bundle = exporter.export_bundle(
            spec,
            target_dir=temp_dir,
            workflow_id=workflow_id,
            registry=request.app.state.registry,
        )

        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(temp_dir):
                for file in files:
                    full_path = Path(root) / file
                    rel_path = full_path.relative_to(temp_dir)
                    zf.write(full_path, arcname=str(rel_path))

        zip_buf.seek(0)
        return StreamingResponse(
            zip_buf,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="workflow_{workflow_id}.zip"',
                "X-Spec-Hash": bundle.spec_hash,
            },
        )


#: Largest Python source accepted by the import endpoint (it is parsed, never executed).
MAX_PYTHON_IMPORT_BYTES = 256 * 1024


@router.get("/{workflow_id}/export/python")
async def export_workflow_python(request: Request, workflow_id: str) -> dict[str, Any]:
    """Return the workflow as readable Python source (secrets scrubbed like the bundle export)."""
    _require_edit(request)
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
    _assert_workflow_ownership(request, workflow_id)
    spec = WorkflowSpec.from_dict(WorkflowExporter().sanitize_spec_dict(wf["spec"]))
    return {
        "workflow_id": workflow_id,
        "filename": f"workflow_{workflow_id}.py",
        "spec_hash": spec.canonical_hash(),
        "source": workflow_to_python(spec),
    }


@router.post("/import/python", status_code=status.HTTP_201_CREATED)
async def import_workflow_python(request: Request) -> dict[str, Any]:
    """Create a workflow from generated Python source.

    The body is JSON ``{"source": "...", "name"?: ..., "workflow_id"?: ...}`` or the raw source
    as ``text/plain``. The source is parsed with ``ast`` and never executed; only the restricted
    shape ``workflow_to_python`` emits is accepted, and the result goes through the same
    validation, approval policy and workspace checks as the JSON import.
    """
    caller_ws, principal, _ = _get_caller_context(request)
    _require_edit(request)

    raw = await request.body()
    if len(raw) > MAX_PYTHON_IMPORT_BYTES + 4096:
        raise HTTPException(
            status_code=413,  # literal: the constant was renamed in newer Starlette
            detail={
                "code": "SOURCE_TOO_LARGE",
                "message": f"Python source exceeds {MAX_PYTHON_IMPORT_BYTES} bytes.",
            },
        )
    name: str | None = None
    workflow_id: str | None = None
    try:
        if request.headers.get("content-type", "").startswith("text/plain"):
            source: Any = raw.decode("utf-8")
        else:
            body = json.loads(raw.decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("body must be a JSON object")
            source = body.get("source")
            name = body.get("name")
            workflow_id = body.get("workflow_id")
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_BODY", "message": f"Malformed request body: {exc}"},
        ) from exc
    if not isinstance(source, str) or not source.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_BODY", "message": "'source' must be non-empty Python text."},
        )
    if len(source.encode("utf-8")) > MAX_PYTHON_IMPORT_BYTES:
        raise HTTPException(
            status_code=413,  # literal: the constant was renamed in newer Starlette
            detail={
                "code": "SOURCE_TOO_LARGE",
                "message": f"Python source exceeds {MAX_PYTHON_IMPORT_BYTES} bytes.",
            },
        )
    if not (name is None or isinstance(name, str)) or not (
        workflow_id is None or isinstance(workflow_id, str)
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_BODY", "message": "'name' and 'workflow_id' must be text."},
        )

    try:
        spec = python_to_workflow(source, max_bytes=MAX_PYTHON_IMPORT_BYTES)
    except CodeImportError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "INVALID_PYTHON_SOURCE",
                "message": "Python import failed",
                "errors": [{"line": exc.line, "column": exc.column, "message": str(exc)}],
            },
        ) from exc

    store: StudioStore = request.app.state.store
    ws_repo: WorkspaceRepository = request.app.state.workspace_repo
    spec = _prepare_spec(request, spec.to_dict(), name, caller_ws)
    record = await _create_or_conflict(request, store, spec, workflow_id)
    creator_id = principal.principal_id if principal else "local-owner"
    ws_repo.record_resource_ownership("workflow", record["workflow_id"], caller_ws, creator_id)
    return record


@router.post("/import", status_code=status.HTTP_201_CREATED)
async def import_workflow(
    request: Request,
    payload: ImportWorkflowRequest,
) -> dict[str, Any]:
    """Import a standalone workflow bundle or specification into caller's workspace."""
    caller_ws, principal, role = _get_caller_context(request)
    if principal:
        try:
            check_permission(principal, role, "edit")
        except PermissionDeniedError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc

    store: StudioStore = request.app.state.store
    ws_repo: WorkspaceRepository = request.app.state.workspace_repo

    spec_data: dict[str, Any] | None = None

    # 1. From zip archive (base64)
    if payload.archive_base64:
        try:
            archive_bytes = base64.b64decode(payload.archive_base64)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "INVALID_ARCHIVE", "message": f"Malformed base64 archive: {exc}"},
            ) from exc

        try:
            zf = zipfile.ZipFile(io.BytesIO(archive_bytes), "r")
        except zipfile.BadZipFile as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "INVALID_ARCHIVE", "message": f"Malformed ZIP archive: {exc}"},
            ) from exc

        with zf:
            total_uncompressed = 0
            wf_content: bytes | None = None
            for info in zf.infolist():
                # Reject path traversal / zip slip
                if ".." in info.filename or info.filename.startswith(("/", "\\")):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail={
                            "code": "UNSAFE_ARCHIVE_CONTENT",
                            "message": (
                                f"Archive contains unsafe path traversal member: '{info.filename}'"
                            ),
                        },
                    )
                # Reject executable / script extensions
                if info.filename.lower().endswith((".exe", ".sh", ".bat", ".cmd", ".vbs")):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail={
                            "code": "UNSAFE_ARCHIVE_CONTENT",
                            "message": (
                                f"Archive contains disallowed executable file: '{info.filename}'"
                            ),
                        },
                    )
                total_uncompressed += info.file_size
                if total_uncompressed > 25_000_000:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail={
                            "code": "UNSAFE_ARCHIVE_CONTENT",
                            "message": (
                                "Archive exceeds maximum permitted decompression limit (25MB)."
                            ),
                        },
                    )
                if info.filename == "workflow.json" or info.filename.endswith("/workflow.json"):
                    if wf_content is not None:
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail={
                                "code": "DUPLICATE_WORKFLOW_ENTRY",
                                "message": (
                                    "Archive contains ambiguous duplicate workflow.json entries."
                                ),
                            },
                        )
                    wf_content = zf.read(info)

            if wf_content is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "code": "INVALID_ARCHIVE",
                        "message": "Archive does not contain workflow.json.",
                    },
                )
            try:
                spec_data = json.loads(wf_content.decode("utf-8"))
            except Exception as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "code": "INVALID_SPEC",
                        "message": f"workflow.json is not valid JSON: {exc}",
                    },
                ) from exc

    # 2. From spec_json string
    elif payload.spec_json:
        try:
            spec_data = json.loads(payload.spec_json)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "INVALID_SPEC", "message": f"Malformed spec JSON: {exc}"},
            ) from exc

    # 3. From spec dict
    elif payload.spec is not None:
        spec_data = payload.spec

    if not spec_data or not isinstance(spec_data, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_SPEC", "message": "No valid workflow specification provided."},
        )

    try:
        spec = WorkflowSpec.from_dict(spec_data)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "SPEC_VALIDATION_FAILED",
                "message": f"WorkflowSpec validation failed: {exc}",
            },
        ) from exc

    creator_id = principal.principal_id if principal else "local-owner"

    spec = _prepare_spec(request, spec_data, payload.name, caller_ws)
    record = await _create_or_conflict(request, store, spec, payload.workflow_id)
    wf_id = record["workflow_id"]
    ws_repo.record_resource_ownership("workflow", wf_id, caller_ws, creator_id)

    return record
