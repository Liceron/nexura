# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Typed exceptions for SQL data connector integration."""

from __future__ import annotations

from typing import Any


class SqlIntegrationError(Exception):
    """Base exception for all SQL connector operations."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class SqlReadOnlyViolationError(SqlIntegrationError):
    """Raised when a data-modifying operation is attempted on a read-only database connection."""

    def __init__(
        self,
        message: str = "Attempt to write to a read-only database connection.",
    ) -> None:
        super().__init__(message, details={"error_code": "READ_ONLY_VIOLATION"})


class CrossWorkspaceConnectionError(SqlIntegrationError):
    """Raised when an execution context attempts to access a connection from another workspace."""

    def __init__(self, connection_id: str, conn_workspace: str, req_workspace: str) -> None:
        super().__init__(
            f"Access to SQL connection '{connection_id}' belonging to workspace "
            f"'{conn_workspace}' is denied for execution workspace '{req_workspace}'.",
            details={
                "connection_id": connection_id,
                "connection_workspace": conn_workspace,
                "requested_workspace": req_workspace,
                "error_code": "CROSS_WORKSPACE_DENIED",
            },
        )


class ConnectionNotFoundError(SqlIntegrationError):
    """Raised when the specified connection ID is not registered."""

    def __init__(self, connection_id: str, workspace_id: str) -> None:
        super().__init__(
            f"SQL connection '{connection_id}' not found for workspace '{workspace_id}'.",
            details={
                "connection_id": connection_id,
                "workspace_id": workspace_id,
                "error_code": "CONNECTION_NOT_FOUND",
            },
        )


class ArbitraryDsnProhibitedError(SqlIntegrationError):
    """Raised when arbitrary DSN or database paths are supplied directly in module inputs."""

    def __init__(self, field_name: str) -> None:
        super().__init__(
            f"Arbitrary database connection parameter '{field_name}' in inputs is prohibited. "
            "All connections must be pre-configured by an operator and referenced by "
            "connection_id.",
            details={"prohibited_field": field_name, "error_code": "ARBITRARY_DSN_PROHIBITED"},
        )


class QueryTimeoutError(SqlIntegrationError):
    """Raised when a query exceeds the configured statement execution timeout."""

    def __init__(self, timeout_seconds: float) -> None:
        super().__init__(
            f"SQL query cancelled: execution time exceeded statement timeout of "
            f"{timeout_seconds} seconds.",
            details={"timeout_seconds": timeout_seconds, "error_code": "STATEMENT_TIMEOUT"},
        )


class RowLimitExceededError(SqlIntegrationError):
    """Raised when a query result set exceeds the allowed row count limit."""

    def __init__(self, row_count: int, max_rows: int) -> None:
        super().__init__(
            f"SQL query result ({row_count} rows) exceeded row limit of {max_rows} rows.",
            details={
                "row_count": row_count,
                "max_rows": max_rows,
                "error_code": "ROW_LIMIT_EXCEEDED",
            },
        )


class ByteLimitExceededError(SqlIntegrationError):
    """Raised when a query result payload exceeds the allowed byte limit."""

    def __init__(self, size_bytes: int, max_bytes: int) -> None:
        super().__init__(
            f"SQL query result payload ({size_bytes} bytes) exceeded byte limit of "
            f"{max_bytes} bytes.",
            details={
                "size_bytes": size_bytes,
                "max_bytes": max_bytes,
                "error_code": "BYTE_LIMIT_EXCEEDED",
            },
        )


class PoolExhaustedError(SqlIntegrationError):
    """Raised when no connections are available in the connection pool."""

    def __init__(self, connection_id: str, pool_size: int) -> None:
        super().__init__(
            f"SQL connection pool '{connection_id}' (size: {pool_size}) exhausted. "
            "All connections are currently checked out.",
            details={
                "connection_id": connection_id,
                "pool_size": pool_size,
                "error_code": "POOL_EXHAUSTED",
            },
        )
