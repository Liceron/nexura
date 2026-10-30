# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Typed error hierarchy for authentication, authorization, and workspace security."""

from __future__ import annotations

from nexura_agents.errors import NexuraError


class AuthError(NexuraError):
    """Base exception for authentication, identity, and authorization failures."""

    def __init__(self, message: str, code: str = "AUTH_ERROR") -> None:
        super().__init__(message)
        self.code = code


class AuthenticationError(AuthError):
    """Raised when authentication credentials or tokens cannot be verified."""

    def __init__(
        self,
        message: str = "Authentication failed.",
        code: str = "AUTHENTICATION_FAILED",
    ) -> None:
        super().__init__(message, code=code)


class TokenExpiredError(AuthenticationError):
    """Raised when an authentication bearer token has expired."""

    def __init__(self, message: str = "Authentication token has expired.") -> None:
        super().__init__(message, code="TOKEN_EXPIRED")


class InvalidAudienceError(AuthenticationError):
    """Raised when token audience does not match the expected service identifier."""

    def __init__(self, message: str = "Token audience claim does not match service.") -> None:
        super().__init__(message, code="INVALID_AUDIENCE")


class InvalidIssuerError(AuthenticationError):
    """Raised when token issuer does not match the trusted authority."""

    def __init__(
        self,
        message: str = "Token issuer claim does not match trusted authority.",
    ) -> None:
        super().__init__(message, code="INVALID_ISSUER")


class InvalidSignatureError(AuthenticationError):
    """Raised when token cryptographic signature or key verification fails."""

    def __init__(self, message: str = "Token cryptographic signature verification failed.") -> None:
        super().__init__(message, code="INVALID_SIGNATURE")


class InvalidKeyError(AuthenticationError):
    """Raised when the specified key ID or key set is invalid or missing."""

    def __init__(self, message: str = "Key identifier not found in key set.") -> None:
        super().__init__(message, code="INVALID_KEY")


class ApiKeyNotFoundError(AuthenticationError):
    """Raised when an API key does not exist or has an invalid prefix."""

    def __init__(self, message: str = "API key not found.") -> None:
        super().__init__(message, code="API_KEY_NOT_FOUND")


class ApiKeyRevokedError(AuthenticationError):
    """Raised when attempting to authenticate with a revoked API key."""

    def __init__(self, message: str = "API key has been revoked.") -> None:
        super().__init__(message, code="API_KEY_REVOKED")


class UnauthenticatedNetworkError(AuthenticationError):
    """Raised when an unauthenticated request is received in shared network service mode."""

    def __init__(
        self,
        message: str = "Unauthenticated network access denied. Service requires authentication.",
    ) -> None:
        super().__init__(message, code="UNAUTHENTICATED_NETWORK_MODE")


class PermissionDeniedError(AuthError):
    """Raised when an authenticated principal lacks required action permissions."""

    def __init__(
        self,
        message: str = "Permission denied for requested action.",
        code: str = "PERMISSION_DENIED",
    ) -> None:
        super().__init__(message, code=code)


class CrossWorkspaceAccessError(PermissionDeniedError):
    """Raised when attempting to access or manipulate resources across workspace boundaries."""

    def __init__(self, message: str = "Cross-workspace access denied.") -> None:
        super().__init__(message, code="CROSS_WORKSPACE_ACCESS_DENIED")
