# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Role-based access control and permission enforcement."""

from __future__ import annotations

from nexura_runtime.service.auth.errors import PermissionDeniedError
from nexura_runtime.service.auth.models import (
    ActionPermission,
    Principal,
    WorkspaceRole,
)

ROLE_PERMISSIONS: dict[WorkspaceRole, set[ActionPermission]] = {
    "admin": {"run", "edit", "approve", "admin", "export_credentials"},
    "editor": {"run", "edit"},
    "viewer": set(),
}


def get_role_permissions(role: WorkspaceRole) -> set[ActionPermission]:
    """Retrieve the set of allowed action permissions for a workspace role."""
    return set(ROLE_PERMISSIONS.get(role, set()))


def has_permission(role: WorkspaceRole, permission: ActionPermission) -> bool:
    """Verify whether a role grants a given action permission."""
    allowed = ROLE_PERMISSIONS.get(role, set())
    return permission in allowed


def check_permission(
    principal: Principal,
    role: WorkspaceRole,
    permission: ActionPermission,
) -> None:
    """Check permission for a principal and role, raising PermissionDeniedError on denial.

    Special case: local-owner profile has full permissions for local development.
    API key scopes (``principal.scopes``, when non-empty) are intersected with the role.
    """
    if principal.profile == "local-owner":
        return

    # API key scopes narrow (never widen) the role's permissions.
    if principal.scopes and permission not in principal.scopes:
        raise PermissionDeniedError(
            f"Principal '{principal.principal_id}' credential scopes do not grant "
            f"permission '{permission}'."
        )

    if not has_permission(role, permission):
        raise PermissionDeniedError(
            f"Principal '{principal.principal_id}' with role '{role}' "
            f"lacks required permission '{permission}'."
        )
