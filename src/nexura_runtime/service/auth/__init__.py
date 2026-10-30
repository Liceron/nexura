# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Identity and authorization: principals, roles, permissions and the profile switch.

Token validation, API keys and the shared profile's principal resolution moved to the closed
edition in EDITIONS_PLAN step 7.
"""

from __future__ import annotations

from nexura_runtime.service.auth.errors import (
    ApiKeyNotFoundError,
    ApiKeyRevokedError,
    AuthenticationError,
    AuthError,
    CrossWorkspaceAccessError,
    InvalidAudienceError,
    InvalidIssuerError,
    InvalidKeyError,
    InvalidSignatureError,
    PermissionDeniedError,
    TokenExpiredError,
    UnauthenticatedNetworkError,
)
from nexura_runtime.service.auth.models import (
    ActionPermission,
    Principal,
    ProfileType,
    WorkspaceRole,
)
from nexura_runtime.service.auth.permissions import (
    ROLE_PERMISSIONS,
    check_permission,
    get_role_permissions,
    has_permission,
)
from nexura_runtime.service.auth.profile import ProfileManager

__all__ = [
    "ActionPermission",
    "ApiKeyNotFoundError",
    "ApiKeyRevokedError",
    "AuthenticationError",
    "AuthError",
    "CrossWorkspaceAccessError",
    "InvalidAudienceError",
    "InvalidIssuerError",
    "InvalidKeyError",
    "InvalidSignatureError",
    "PermissionDeniedError",
    "Principal",
    "ProfileManager",
    "ProfileType",
    "ROLE_PERMISSIONS",
    "TokenExpiredError",
    "UnauthenticatedNetworkError",
    "WorkspaceRole",
    "check_permission",
    "get_role_permissions",
    "has_permission",
]
