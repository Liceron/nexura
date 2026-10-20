# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Typed errors for the connectors pack; messages are always redacted before they surface."""

from __future__ import annotations


class ConnectorError(Exception):
    """Base class for connector failures, carrying a stable machine-readable code."""

    code = "CONNECTOR_ERROR"
    retryable = False

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class InvalidParamsError(ConnectorError):
    """Operation parameters failed validation."""

    code = "INVALID_PARAMS"


class InvalidConfigError(ConnectorError):
    """Module configuration is missing or unusable."""

    code = "INVALID_CONFIG"


class ApprovalRequiredError(ConnectorError):
    """A write operation was requested without explicit approval."""

    code = "APPROVAL_REQUIRED"


class MissingCredentialError(ConnectorError):
    """The credential reference is absent, literal, or does not resolve."""

    code = "MISSING_CREDENTIAL"


class SsrfBlockedError(ConnectorError):
    """The destination violates the outbound network policy."""

    code = "SSRF_BLOCKED"


class ResponseTooLargeError(ConnectorError):
    """The upstream response exceeded the configured size cap."""

    code = "RESPONSE_TOO_LARGE"


class AuthFailedError(ConnectorError):
    """Upstream rejected the credential (401/403)."""

    code = "AUTH_FAILED"


class RateLimitedError(ConnectorError):
    """Upstream throttled the request (429)."""

    code = "RATE_LIMITED"
    retryable = True


class UpstreamError(ConnectorError):
    """Upstream returned an error status or an unusable body."""

    code = "UPSTREAM_ERROR"


class ConnectorTimeoutError(UpstreamError):
    """The upstream did not answer within the timeout."""

    code = "TIMEOUT"
    retryable = True


class ConnectorTransportError(UpstreamError):
    """The connection to the upstream failed."""

    code = "UPSTREAM_UNAVAILABLE"
    retryable = True


class IndeterminateEffectError(ConnectorError):
    """A write may or may not have been applied (no response received)."""

    code = "INDETERMINATE_EFFECT"


class ConnectorDependencyError(ConnectorError):
    """A real driver is not installed; inject a driver or install the optional extra."""

    code = "MISSING_DEPENDENCY"
