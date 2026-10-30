# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Persisted MCP/OpenAPI fixture tool registrations on Postgres (MVP WP8, stores #17, #18).

``PostgresIntegrationRegistrations`` has the API of
``nexura_runtime.service.storage_backend.SQLiteIntegrationRegistrations`` (migration component
``integrations``; the SQLite table name ``nexura_service_integration_registrations``). The table
is created by the migration, never by the register route. Every instance restores the rows
into its module registry when it starts (``create_api_app``).

**One workspace per tool id, across instances.** ``save`` takes a transaction-level advisory lock
per module id (in sorted order, so two saves never wait on each other in a cycle), refuses with
``IntegrationToolTakenError`` when another workspace already holds one of the ids, then upserts
the rows, all in one transaction. A unique index on ``module_id`` is the backstop. So two
instances registering one id for two workspaces at once give one success and one refusal, and
nothing of the refused call is stored.

NUL: ``TEXT`` cannot hold NUL; a NUL in a stored value is kept as U+FFFD. Every call is a network
round trip (``blocking_io = True``); the register route runs ``save`` in a worker thread.
"""

from __future__ import annotations

import builtins
import time
from typing import Any

from nexura_postgres.migrations import MIGRATION_TIMEOUT, Migration, migrate_all, register
from nexura_runtime.service.storage_backend import IntegrationToolTakenError

COMPONENT = "integrations"
TABLE = "nexura_service_integration_registrations"
MIGRATIONS: tuple[Migration, ...] = (
    (
        1,
        "integration tool registrations",
        (
            f"""CREATE TABLE {TABLE} (
                workspace_id TEXT NOT NULL,
                module_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                fixture_id TEXT,
                server_name_or_prefix TEXT NOT NULL,
                created_at DOUBLE PRECISION NOT NULL,
                seq BIGINT GENERATED ALWAYS AS IDENTITY,
                PRIMARY KEY (workspace_id, module_id))""",
            # A tool id belongs to one workspace (the save lock enforces it; this is the backstop).
            f"CREATE UNIQUE INDEX {TABLE}_module ON {TABLE} (module_id)",
        ),
    ),
)
register(COMPONENT, MIGRATIONS)

#: Serialises every save of one module id, on every instance (transaction-scoped).
TOOL_LOCK_SQL = (
    "SELECT pg_advisory_xact_lock(hashtextextended('nexura-integration-tool/' || $1, 0))"
)


def _t(value: str) -> str:
    return value.replace("\x00", "\ufffd")


class PostgresIntegrationRegistrations:
    """Registrations in the shared Postgres ``Database``; the store never closes it."""

    blocking_io = True

    def __init__(self, database: Any, *, migrate: bool = True) -> None:
        self.database = database
        if migrate:

            async def apply(conn: Any) -> None:
                await migrate_all(conn, {COMPONENT: MIGRATIONS})

            database.run(apply, timeout=MIGRATION_TIMEOUT + 30)

    def list(self) -> list[tuple[str, str, str, str | None, str]]:
        """Every registration, in the order it was first saved."""

        async def run(conn: Any) -> Any:
            return await conn.fetch(
                "SELECT workspace_id, module_id, kind, fixture_id, server_name_or_prefix "
                f"FROM {TABLE} ORDER BY seq"
            )

        return [tuple(row) for row in self.database.run(run)]

    def save(
        self,
        workspace_id: str,
        module_ids: builtins.list[str],
        kind: str,
        fixture_id: str | None,
        server_name_or_prefix: str,
    ) -> None:
        """Store (or replace) the workspace's registrations of ``module_ids`` atomically.

        Raises ``IntegrationToolTakenError`` (nothing stored) when another workspace holds one
        of the ids.
        """
        ws = _t(workspace_id)
        ids = sorted({_t(mid) for mid in module_ids})
        if not ids:
            return
        now = time.time()

        async def run(conn: Any) -> None:
            import asyncpg  # type: ignore[import-untyped]

            for mid in ids:
                await conn.execute(TOOL_LOCK_SQL, mid)
            taken = await conn.fetchval(
                f"SELECT module_id FROM {TABLE} WHERE module_id = ANY($1::text[]) "
                'AND workspace_id <> $2 ORDER BY module_id COLLATE "C" LIMIT 1',
                ids,
                ws,
            )
            if taken is not None:
                raise IntegrationToolTakenError(taken)
            try:
                await conn.execute(
                    f"""INSERT INTO {TABLE} (workspace_id, module_id, kind, fixture_id,
                        server_name_or_prefix, created_at)
                    SELECT $1, m, $3, $4, $5, $6 FROM unnest($2::text[]) AS m
                    ON CONFLICT (workspace_id, module_id) DO UPDATE SET
                        kind = EXCLUDED.kind, fixture_id = EXCLUDED.fixture_id,
                        server_name_or_prefix = EXCLUDED.server_name_or_prefix,
                        created_at = EXCLUDED.created_at""",
                    ws,
                    ids,
                    _t(kind),
                    None if fixture_id is None else _t(fixture_id),
                    _t(server_name_or_prefix),
                    now,
                )
            except asyncpg.UniqueViolationError as exc:  # pragma: no cover - the lock wins first
                raise IntegrationToolTakenError(ids[0]) from exc

        self.database.run(run)

    def close(self) -> None:
        """Nothing to release: the shared ``Database`` belongs to the app."""

    def delete(self, workspace_id: str, module_ids: builtins.list[str]) -> builtins.list[str]:
        """Delete the workspace's registrations of ``module_ids``; returns the ids deleted."""
        ws = _t(workspace_id)
        ids = sorted({_t(mid) for mid in module_ids})
        if not ids:
            return []

        async def run(conn: Any) -> Any:
            return await conn.fetch(
                f"DELETE FROM {TABLE} WHERE workspace_id = $1 AND module_id = ANY($2::text[]) "
                "RETURNING module_id",
                ws,
                ids,
            )

        return sorted(str(row[0]) for row in self.database.run(run))


__all__ = ["COMPONENT", "MIGRATIONS", "TABLE", "PostgresIntegrationRegistrations"]
