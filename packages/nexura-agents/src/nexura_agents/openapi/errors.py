# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Typed errors for HTTP and OpenAPI connectors."""

from __future__ import annotations

from nexura_agents.errors import NexuraError


class HttpIntegrationError(NexuraError):
    """Base class for HTTP and OpenAPI integration errors."""


class DuplicateOperationIdError(HttpIntegrationError):
    """Raised when an OpenAPI specification defines duplicate operation IDs."""

    def __init__(self, operation_id: str, path: str, method: str) -> None:
        self.operation_id = operation_id
        self.path = path
        self.method = method
        super().__init__(
            f"Duplicate OpenAPI operationId '{operation_id}' detected at {method.upper()} {path}. "
            "All operation IDs must be unique across the specification."
        )


class UnsupportedOpenAPIFeatureError(HttpIntegrationError):
    """Raised when an OpenAPI specification utilizes unsupported features (e.g. external refs)."""

    def __init__(self, feature: str, details: str | None = None) -> None:
        self.feature = feature
        self.details = details
        msg = f"Unsupported OpenAPI feature '{feature}'"
        if details:
            msg += f": {details}"
        super().__init__(msg)


class HttpMaxResponseSizeExceededError(HttpIntegrationError):
    """Raised when an HTTP response exceeds the configured maximum byte cap."""

    def __init__(self, size_bytes: int, limit_bytes: int) -> None:
        self.size_bytes = size_bytes
        self.limit_bytes = limit_bytes
        super().__init__(
            f"HTTP response size ({size_bytes} bytes) exceeds "
            f"configured limit of {limit_bytes} bytes."
        )


class NonIdempotentRetryForbiddenError(HttpIntegrationError):
    """Raised when a non-idempotent HTTP request (e.g. POST) is submitted for automated retries."""

    def __init__(self, method: str, url: str) -> None:
        self.method = method
        self.url = url
        super().__init__(
            f"Automated retry forbidden for non-idempotent HTTP method '{method.upper()}' "
            f"to '{url}' without explicit idempotency key or connector declaration."
        )
