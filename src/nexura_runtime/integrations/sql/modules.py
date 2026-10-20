# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""B00/B01 module implementations for SQL querying, schema inspection, and execution."""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from nexura_agents.contracts.base import BaseModule
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.manifest import ModuleManifest
from nexura_agents.contracts.result import ModuleResult

from nexura_runtime.integrations.sql.connection import (
    SqlConnectionManager,
    default_sql_connection_manager,
)
from nexura_runtime.integrations.sql.errors import (
    ArbitraryDsnProhibitedError,
    ByteLimitExceededError,
    QueryTimeoutError,
    SqlReadOnlyViolationError,
)

_PROHIBITED_INPUT_FIELDS = frozenset(
    {
        "dsn",
        "uri",
        "database_path",
        "db_path",
        "connection_string",
        "host",
        "database",
    }
)


def _validate_no_arbitrary_dsn(inputs: dict[str, Any]) -> None:
    """Ensure callers do not pass arbitrary connection strings or file paths."""
    for field in _PROHIBITED_INPUT_FIELDS:
        if field in inputs:
            raise ArbitraryDsnProhibitedError(field)


class SqlQueryModule(BaseModule):
    """B00/B01 read-only parameterized SQL query module."""

    manifest = ModuleManifest(
        schema_version=1,
        module_id="nexura.sql.query",
        version="1.0.0",
        kind="integration",
        title="SQL Query (Read-Only)",
        description=(
            "Execute policy-bounded, parameterized read-only SQL queries with timeout "
            "and result size limits."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "connection_id": {"type": "string"},
                "query": {"type": "string"},
                "parameters": {},
                "max_rows": {"type": "integer"},
            },
            "required": ["connection_id", "query"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "columns": {"type": "array"},
                "rows": {"type": "array"},
                "row_count": {"type": "integer"},
                "truncated": {"type": "boolean"},
                "execution_time_seconds": {"type": "number"},
            },
        },
        config_schema={
            "type": "object",
            "properties": {
                "timeout_seconds": {"type": "number"},
                "max_rows": {"type": "integer"},
                "max_bytes": {"type": "integer"},
            },
        },
        named_ports=[],
        capabilities={"streaming": False, "network": False},
        effect_policy="read",
        dependency_extra=None,
        min_sdk_version="0.3.0",
        timeout_seconds=30.0,
        output_limit_bytes=5_000_000,
        required_credential_types=[],
        supported_modalities=["text"],
        supported_locales=["all"],
        source_reference="src/nexura_runtime/integrations/sql/",
        verification_status="mock_verified",
    )

    def __init__(self, manager: SqlConnectionManager | None = None) -> None:
        self.manager = manager or default_sql_connection_manager

    async def invoke(
        self,
        context: ExecutionContext,
        inputs: dict[str, Any],
        config: dict[str, Any] | None = None,
    ) -> ModuleResult:
        _validate_no_arbitrary_dsn(inputs)

        cfg = config or {}
        connection_id = inputs["connection_id"]
        query = inputs["query"].strip()
        parameters = inputs.get("parameters")
        workspace_id = context.workspace_id

        pool = await self.manager.get_pool(connection_id, workspace_id)
        effective_timeout = float(
            cfg.get("timeout_seconds") or pool.config.statement_timeout_seconds
        )
        max_allowed_rows = int(
            inputs.get("max_rows") or cfg.get("max_rows") or pool.config.max_rows
        )
        max_bytes = int(cfg.get("max_bytes") or pool.config.max_bytes)

        # Execute query within checked-out pooled connection
        async with pool.connection() as conn:
            raw_conn = conn.raw_connection
            start_time = time.monotonic()

            def progress_checker() -> int:
                if time.monotonic() - start_time > effective_timeout:
                    return 1  # Abort query
                return 0

            raw_conn.set_progress_handler(progress_checker, 100)

            try:
                cursor = raw_conn.cursor()

                # Bind parameters deterministically
                if parameters is None:
                    cursor.execute(query)
                elif isinstance(parameters, (list, tuple)):
                    cursor.execute(query, tuple(parameters))
                elif isinstance(parameters, dict):
                    cursor.execute(query, parameters)
                else:
                    cursor.execute(query, (parameters,))

                cols = [col[0] for col in cursor.description] if cursor.description else []

                # Fetch up to max_allowed_rows + 1 to detect truncation
                raw_rows = cursor.fetchmany(max_allowed_rows + 1)
                is_truncated = len(raw_rows) > max_allowed_rows
                returned_rows = raw_rows[:max_allowed_rows]

                # Format rows as list of dicts
                rows_as_dicts: list[dict[str, Any]] = [
                    dict(zip(cols, row, strict=False)) for row in returned_rows
                ]

                # Check payload byte limits
                payload_json = json.dumps(rows_as_dicts, ensure_ascii=False)
                payload_bytes = len(payload_json.encode("utf-8"))
                if payload_bytes > max_bytes:
                    raise ByteLimitExceededError(payload_bytes, max_bytes)

            except sqlite3.OperationalError as exc:
                err_msg = str(exc).lower()
                if "interrupted" in err_msg or "query aborted" in err_msg:
                    raise QueryTimeoutError(effective_timeout) from exc
                if "readonly database" in err_msg or "query_only" in err_msg:
                    raise SqlReadOnlyViolationError(str(exc)) from exc
                raise
            finally:
                raw_conn.set_progress_handler(None, 0)

            elapsed = round(time.monotonic() - start_time, 4)

            return ModuleResult.success(
                outputs={
                    "columns": cols,
                    "rows": rows_as_dicts,
                    "row_count": len(rows_as_dicts),
                    "truncated": is_truncated,
                    "execution_time_seconds": elapsed,
                }
            )


class SqlSchemaInspectModule(BaseModule):
    """B00/B01 module for inspecting SQL database schemas and table definitions."""

    manifest = ModuleManifest(
        schema_version=1,
        module_id="nexura.sql.schema",
        version="1.0.0",
        kind="integration",
        title="SQL Schema Inspection",
        description=(
            "Inspect tables, columns, data types, and constraints of a configured database."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "connection_id": {"type": "string"},
                "table_name": {"type": "string"},
            },
            "required": ["connection_id"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "tables": {"type": "array"},
            },
        },
        config_schema={"type": "object"},
        named_ports=[],
        capabilities={"streaming": False, "network": False},
        effect_policy="read",
        dependency_extra=None,
        min_sdk_version="0.3.0",
        timeout_seconds=15.0,
        output_limit_bytes=1_000_000,
        required_credential_types=[],
        supported_modalities=["text"],
        supported_locales=["all"],
        source_reference="src/nexura_runtime/integrations/sql/",
        verification_status="mock_verified",
    )

    def __init__(self, manager: SqlConnectionManager | None = None) -> None:
        self.manager = manager or default_sql_connection_manager

    async def invoke(
        self,
        context: ExecutionContext,
        inputs: dict[str, Any],
        config: dict[str, Any] | None = None,
    ) -> ModuleResult:
        _validate_no_arbitrary_dsn(inputs)

        connection_id = inputs["connection_id"]
        target_table = inputs.get("table_name")
        workspace_id = context.workspace_id

        pool = await self.manager.get_pool(connection_id, workspace_id)

        async with pool.connection() as conn:
            raw_conn = conn.raw_connection
            cursor = raw_conn.cursor()

            # Query list of user tables
            if target_table:
                cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
                    (target_table,),
                )
            else:
                cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )

            table_names = [row[0] for row in cursor.fetchall()]
            tables_metadata: list[dict[str, Any]] = []

            for tbl in table_names:
                # Retrieve column definitions via PRAGMA table_info
                # Sanitize table identifier by verifying it exists in table_names
                cursor.execute(f'PRAGMA table_info("{tbl}")')
                col_rows = cursor.fetchall()
                cols: list[dict[str, Any]] = []
                for cid, name, c_type, notnull, dflt_value, pk in col_rows:
                    cols.append(
                        {
                            "cid": cid,
                            "name": name,
                            "type": c_type,
                            "notnull": bool(notnull),
                            "default_value": dflt_value,
                            "primary_key": bool(pk),
                        }
                    )

                # Retrieve foreign keys
                cursor.execute(f'PRAGMA foreign_key_list("{tbl}")')
                fk_rows = cursor.fetchall()
                fks: list[dict[str, Any]] = []
                for _, _, to_tbl, from_col, to_col, on_update, on_delete, _ in fk_rows:
                    fks.append(
                        {
                            "to_table": to_tbl,
                            "from_column": from_col,
                            "to_column": to_col,
                            "on_update": on_update,
                            "on_delete": on_delete,
                        }
                    )

                tables_metadata.append(
                    {
                        "name": tbl,
                        "columns": cols,
                        "foreign_keys": fks,
                    }
                )

            return ModuleResult.success(outputs={"tables": tables_metadata})


class SqlExecuteModule(BaseModule):
    """B00/B01 module for policy-gated explicit SQL write operations."""

    manifest = ModuleManifest(
        schema_version=1,
        module_id="nexura.sql.execute",
        version="1.0.0",
        kind="integration",
        title="SQL Execute (Write)",
        description="Execute explicit policy-gated SQL statements (INSERT, UPDATE, DELETE).",
        input_schema={
            "type": "object",
            "properties": {
                "connection_id": {"type": "string"},
                "statement": {"type": "string"},
                "parameters": {},
            },
            "required": ["connection_id", "statement"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "rows_affected": {"type": "integer"},
                "last_row_id": {},
                "execution_time_seconds": {"type": "number"},
            },
        },
        config_schema={"type": "object"},
        named_ports=[],
        capabilities={"streaming": False, "network": False},
        effect_policy="non_idempotent_write",
        always_write=True,
        dependency_extra=None,
        min_sdk_version="0.3.0",
        timeout_seconds=30.0,
        output_limit_bytes=100_000,
        required_credential_types=[],
        supported_modalities=["text"],
        supported_locales=["all"],
        source_reference="src/nexura_runtime/integrations/sql/",
        verification_status="mock_verified",
    )

    def __init__(self, manager: SqlConnectionManager | None = None) -> None:
        self.manager = manager or default_sql_connection_manager

    async def invoke(
        self,
        context: ExecutionContext,
        inputs: dict[str, Any],
        config: dict[str, Any] | None = None,
    ) -> ModuleResult:
        _validate_no_arbitrary_dsn(inputs)
        context.require_write_approval("SQL execute")

        connection_id = inputs["connection_id"]
        statement = inputs["statement"].strip()
        parameters = inputs.get("parameters")
        workspace_id = context.workspace_id

        pool = await self.manager.get_pool(connection_id, workspace_id)

        # Enforce that the connection configuration permits writes
        if pool.config.read_only:
            raise SqlReadOnlyViolationError(
                f"SQL connection '{connection_id}' is configured as read-only. "
                "Mutating statements are prohibited."
            )

        async with pool.connection() as conn:
            raw_conn = conn.raw_connection
            start_time = time.monotonic()
            cursor = raw_conn.cursor()

            try:
                if parameters is None:
                    cursor.execute(statement)
                elif isinstance(parameters, (list, tuple)):
                    cursor.execute(statement, tuple(parameters))
                elif isinstance(parameters, dict):
                    cursor.execute(statement, parameters)
                else:
                    cursor.execute(statement, (parameters,))

                raw_conn.commit()
            except sqlite3.OperationalError as exc:
                raw_conn.rollback()
                if "readonly database" in str(exc).lower():
                    raise SqlReadOnlyViolationError(str(exc)) from exc
                raise

            elapsed = round(time.monotonic() - start_time, 4)

            return ModuleResult.success(
                outputs={
                    "rows_affected": cursor.rowcount,
                    "last_row_id": cursor.lastrowid,
                    "execution_time_seconds": elapsed,
                }
            )
