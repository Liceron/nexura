"""OSS identity contracts: one owner, pure role policy, isolated local projects."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from nexura_runtime.modules.registry import default_registry
from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.api.auth_providers import LocalOwnerAuthProvider
from nexura_runtime.service.api.knowledge import _assert_doc_ownership
from nexura_runtime.service.api.runs import _assert_run_ownership
from nexura_runtime.service.api.sessions import _assert_session_ownership
from nexura_runtime.service.api.workflows import _assert_workflow_ownership
from nexura_runtime.service.auth import ProfileManager
from nexura_runtime.service.auth.errors import CrossWorkspaceAccessError, PermissionDeniedError
from nexura_runtime.service.auth.models import ActionPermission, Principal
from nexura_runtime.service.auth.permissions import check_permission
from nexura_runtime.service.auth.resources import require_resource
from nexura_runtime.service.extensions import AuthProviderRequiredError
from nexura_runtime.service.workspaces.repository import WorkspaceRepository
from nexura_runtime.service.workspaces.scoping import ResourceScopeEnforcer


def test_profile_manager_holds_only_the_profile(tmp_path: Path) -> None:
    manager = ProfileManager()
    assert vars(manager) == {"profile": "local-owner"}
    assert manager.is_configured()
    manager.set_profile("authenticated-shared-service")
    assert not manager.is_configured()
    assert not manager.is_operator(None)
    with pytest.raises(AuthProviderRequiredError, match="enterprise"):
        create_api_app(db_path=tmp_path / "shared.db", profile_manager=manager)


@pytest.mark.parametrize(
    "kind",
    [
        "workflow",
        "artifact",
        "document",
        "run",
        "event",
        "index",
        "dataset",
        "credential",
        "approval",
    ],
)
def test_local_resource_scope_isolation(kind: str) -> None:
    repo = WorkspaceRepository()
    for wid in ("A", "B"):
        repo.create_workspace(wid, wid, "local-owner")
    enforcer = ResourceScopeEnforcer(repo)
    principal = Principal(principal_id="local-owner", profile="local-owner")
    assert enforcer.enforce_workspace_access(principal, "A") == "admin"
    assert enforcer.enforce_workspace_access(principal, "B") == "admin"
    enforcer.enforce_resource_ownership(
        caller_workspace_id="A", resource_workspace_id="A", resource_type=kind, resource_id="own"
    )
    with pytest.raises(CrossWorkspaceAccessError):
        enforcer.enforce_resource_ownership(
            caller_workspace_id="A",
            resource_workspace_id="B",
            resource_type=kind,
            resource_id="foreign",
        )


def test_closed_identity_modules_are_not_registered() -> None:
    assert not default_registry.has("nexura.service.auth")
    assert not default_registry.has("nexura.service.workspaces")


@pytest.mark.parametrize("role", ["admin", "editor", "viewer", "owner", ""])
@pytest.mark.parametrize("target", [None, "A", "B"])
def test_principal_headers_never_change_local_identity_or_role(
    role: str, target: str | None
) -> None:
    request = Request(
        {
            "type": "http",
            "headers": [
                (b"x-principal-id", b"foreign-user"),
                (b"x-principal-role", role.encode()),
                (b"x-workspace-id", b"forged-workspace"),
            ],
        }
    )
    identity = LocalOwnerAuthProvider().authenticate(request, target)
    assert identity.principal.principal_id == "local-owner"
    assert identity.principal.profile == "local-owner"
    assert identity.role == "admin"
    assert identity.workspace_id == (target or "default")


@pytest.mark.parametrize(
    "kind,guard,status_code,code",
    [
        # One person owns every local workspace: the guard denies and names the owner.
        ("run", _assert_run_ownership, 403, "CROSS_WORKSPACE_ACCESS_DENIED"),
        ("workflow", _assert_workflow_ownership, 403, "CROSS_WORKSPACE_ACCESS_DENIED"),
        ("session", _assert_session_ownership, 403, "CROSS_WORKSPACE_ACCESS_DENIED"),
        ("document", _assert_doc_ownership, 403, "CROSS_WORKSPACE_ACCESS_DENIED"),
    ],
)
def test_route_ownership_guards_deny_foreign_local_project(kind, guard, status_code, code) -> None:
    repo = WorkspaceRepository()
    repo.record_resource_ownership(kind, "foreign", "B", "local-owner")
    request = Request(
        {
            "type": "http",
            "headers": [],
            "app": SimpleNamespace(state=SimpleNamespace(workspace_repo=repo)),
            "state": {"workspace_id": "A", "principal": Principal(principal_id="local-owner")},
        }
    )
    with pytest.raises(HTTPException) as error:
        guard(request, "foreign")
    assert error.value.status_code == status_code
    assert error.value.detail["code"] == code
    if status_code == 403:
        assert "'B'" in error.value.detail["message"]
    else:
        assert "B" not in error.value.detail["message"]
    assert repo.get_resource_ownership(kind, "foreign") == ("B", "local-owner")
    repo.record_resource_ownership(kind, "own", "A", "local-owner")
    guard(request, "own")


def test_oss_has_no_auth_extra_or_auth_development_dependency() -> None:
    import tomllib

    project = tomllib.loads((Path(__file__).resolve().parents[3] / "pyproject.toml").read_text())
    assert "auth" not in project["project"]["optional-dependencies"]
    assert not any(
        "authlib" in dep or "joserfc" in dep for dep in project["dependency-groups"]["dev"]
    )


def test_resource_guard_still_rejects_foreign_or_unnamed_resources() -> None:
    owner = Principal(principal_id="local-owner", profile="local-owner")
    require_resource(owner, "workflow", "any-local-workflow")
    restricted = owner.model_copy(update={"resources": ["workflow:allowed"]})
    require_resource(restricted, "workflow", "allowed")
    for kind, rid in (("workflow", "foreign"), ("knowledge", "allowed"), ("workflow", None)):
        with pytest.raises(PermissionDeniedError, match="limited"):
            require_resource(restricted, kind, rid)


class TestRolePermissionsAndPrivilegeSeparation:
    """Acceptance: viewer cannot approve or export credentials, editor cannot admin."""

    def test_viewer_cannot_approve_or_export_credentials(self) -> None:
        viewer = Principal(principal_id="p_viewer", profile="authenticated-shared-service")

        # Viewer cannot approve workflow gates
        with pytest.raises(PermissionDeniedError):
            check_permission(viewer, "viewer", "approve")

        # Viewer cannot export credentials
        with pytest.raises(PermissionDeniedError):
            check_permission(viewer, "viewer", "export_credentials")

        # Viewer cannot run workflows
        with pytest.raises(PermissionDeniedError):
            check_permission(viewer, "viewer", "run")

        # Viewer cannot edit resources
        with pytest.raises(PermissionDeniedError):
            check_permission(viewer, "viewer", "edit")

    def test_editor_can_run_and_edit_but_cannot_approve_or_export(self) -> None:
        editor = Principal(principal_id="p_editor", profile="authenticated-shared-service")

        # Allowed: run and edit
        check_permission(editor, "editor", "run")
        check_permission(editor, "editor", "edit")

        # Disallowed: approve, export_credentials, admin
        with pytest.raises(PermissionDeniedError):
            check_permission(editor, "editor", "approve")

        with pytest.raises(PermissionDeniedError):
            check_permission(editor, "editor", "export_credentials")

        with pytest.raises(PermissionDeniedError):
            check_permission(editor, "editor", "admin")

    def test_admin_has_all_permissions(self) -> None:
        admin = Principal(principal_id="p_admin", profile="authenticated-shared-service")
        perms: list[ActionPermission] = ["run", "edit", "approve", "admin", "export_credentials"]
        for perm in perms:
            check_permission(admin, "admin", perm)
