# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Typed error hierarchy for workspace and membership operations."""

from __future__ import annotations

from nexura_runtime.service.auth.errors import AuthError


class WorkspaceError(AuthError):
    """Base exception for workspace failures."""

    def __init__(self, message: str, code: str = "WORKSPACE_ERROR") -> None:
        super().__init__(message, code=code)


class WorkspaceNotFoundError(WorkspaceError):
    """Raised when a specified workspace ID does not exist."""

    def __init__(self, workspace_id: str) -> None:
        super().__init__(f"Workspace '{workspace_id}' not found.", code="WORKSPACE_NOT_FOUND")
        self.workspace_id = workspace_id


class MemberNotFoundError(WorkspaceError):
    """Raised when a principal is not a member of the workspace."""

    def __init__(self, workspace_id: str, principal_id: str) -> None:
        super().__init__(
            f"Principal '{principal_id}' is not a member of workspace '{workspace_id}'.",
            code="MEMBER_NOT_FOUND",
        )
        self.workspace_id = workspace_id
        self.principal_id = principal_id


class MemberAlreadyExistsError(WorkspaceError):
    """Raised when attempting to add a principal who is already a member."""

    def __init__(self, workspace_id: str, principal_id: str) -> None:
        super().__init__(
            f"Principal '{principal_id}' is already a member of workspace '{workspace_id}'.",
            code="MEMBER_ALREADY_EXISTS",
        )
        self.workspace_id = workspace_id
        self.principal_id = principal_id


class LastAdminError(WorkspaceError):
    """Raised when removing or demoting a member would leave the workspace with no admin."""

    def __init__(self, workspace_id: str, principal_id: str) -> None:
        super().__init__(
            f"Principal '{principal_id}' is the last admin of workspace '{workspace_id}'; "
            "make another member an admin first.",
            code="LAST_ADMIN",
        )
        self.workspace_id = workspace_id
        self.principal_id = principal_id
