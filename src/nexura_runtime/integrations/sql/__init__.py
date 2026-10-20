# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""SQL data connector integration package conforming to B00/B01 contracts."""

from __future__ import annotations

from nexura_runtime.integrations.sql.connection import (
    SqlConnectionConfig,
    SqlConnectionManager,
    SqlConnectionPool,
    SqlPooledConnection,
    default_sql_connection_manager,
)
from nexura_runtime.integrations.sql.errors import (
    ArbitraryDsnProhibitedError,
    ByteLimitExceededError,
    ConnectionNotFoundError,
    CrossWorkspaceConnectionError,
    PoolExhaustedError,
    QueryTimeoutError,
    RowLimitExceededError,
    SqlIntegrationError,
    SqlReadOnlyViolationError,
)
from nexura_runtime.integrations.sql.modules import (
    SqlExecuteModule,
    SqlQueryModule,
    SqlSchemaInspectModule,
)
from nexura_runtime.modules.registry import default_registry

# Register modules with the default module registry
default_registry.register(SqlQueryModule())
default_registry.register(SqlSchemaInspectModule())
default_registry.register(SqlExecuteModule())

# Pre-register a default SQLite sample database for immediate CLI and demo availability
default_sql_connection_manager.register_connection(
    SqlConnectionConfig(
        connection_id="default_sqlite",
        workspace_id="*",
        engine="sqlite",
        database_path="file:nexura_default_db?mode=memory&cache=shared",
        read_only=True,
    ),
    initial_sql="""
    CREATE TABLE IF NOT EXISTS sample_items (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        price REAL NOT NULL
    );
    INSERT OR IGNORE INTO sample_items (id, name, price) VALUES
        (1, 'Laptop', 1200.0),
        (2, 'Mouse', 25.0);
    """,
)

__all__ = [
    "SqlConnectionConfig",
    "SqlPooledConnection",
    "SqlConnectionPool",
    "SqlConnectionManager",
    "default_sql_connection_manager",
    "SqlQueryModule",
    "SqlSchemaInspectModule",
    "SqlExecuteModule",
    "SqlIntegrationError",
    "SqlReadOnlyViolationError",
    "CrossWorkspaceConnectionError",
    "ConnectionNotFoundError",
    "ArbitraryDsnProhibitedError",
    "QueryTimeoutError",
    "RowLimitExceededError",
    "ByteLimitExceededError",
    "PoolExhaustedError",
]
