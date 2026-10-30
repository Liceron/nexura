# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Workspaces and resource ownership on Postgres (MVP Postgres WP2, store #4 and #6).

``PostgresWorkspaceStore`` is the storage behind a ``WorkspaceRepository`` bound with
``bind_database``. It is read-through: nothing is cached in the process, so every instance
behind a load balancer sees the same workspaces and ownership on its next request.

Pooled-endpoint safety (design §2b-5): every call is one transaction on the shared
``Database``, except that one-statement calls run without ``BEGIN``/``COMMIT``
(``autocommit``, MVP WP11a: two round trips fewer); mutations of one workspace serialise on a
transaction-level advisory lock; no session state. Tables keep the SQLite names; ``seq``
identity columns keep the SQLite in-memory iteration order (insertion order, unchanged by an
upsert).
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping
from typing import Any

from nexura_postgres.migrations import Migration, migrate_all, register
from nexura_runtime.service.workspaces.models import Workspace
from nexura_runtime.service.workspaces.repository import WorkspaceExistsError

COMPONENT = "workspaces"
MIGRATIONS: tuple[Migration, ...] = (
    (
        1,
        "workspaces and resource ownership",
        (
            """CREATE TABLE nexura_service_workspaces (
                seq BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
                workspace_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                created_at DOUBLE PRECISION NOT NULL,
                metadata_json TEXT NOT NULL)""",
            """CREATE TABLE nexura_service_resource_ownership (
                seq BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
                resource_type TEXT NOT NULL,
                resource_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                created_at DOUBLE PRECISION NOT NULL,
                PRIMARY KEY (resource_type, resource_id))""",
            """CREATE INDEX idx_nexura_ownership_ws
                ON nexura_service_resource_ownership (workspace_id, resource_type, seq)""",
        ),
    ),
)
register(COMPONENT, MIGRATIONS)

WORKSPACE_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtextextended('nexura-ws/' || $1, 0))"
_WORKSPACE_COLUMNS = "workspace_id, name, owner_id, created_at, metadata_json"


def workspace_from_row(row: Any) -> Workspace:
    meta = row["metadata_json"]
    return Workspace(
        workspace_id=row["workspace_id"],
        name=row["name"],
        owner_id=row["owner_id"],
        created_at=row["created_at"],
        metadata=json.loads(meta) if meta else {},
    )


async def insert_workspace(conn: Any, ws: Workspace) -> bool:
    """Insert ``ws`` unless its id exists; True when inserted."""
    inserted = await conn.fetchval(
        f"INSERT INTO nexura_service_workspaces ({_WORKSPACE_COLUMNS}) VALUES ($1,$2,$3,$4,$5) "
        "ON CONFLICT (workspace_id) DO NOTHING RETURNING workspace_id",
        ws.workspace_id,
        ws.name,
        ws.owner_id,
        ws.created_at,
        json.dumps(ws.metadata),
    )
    return inserted is not None


class PostgresWorkspaceStore:
    """Read-through workspaces and resource ownership on one shared ``Database``."""

    #: Migration components this store needs; subclasses add theirs.
    COMPONENTS: Mapping[str, tuple[Migration, ...]] = {COMPONENT: MIGRATIONS}

    def __init__(self, database: Any, *, migrate: bool = True) -> None:
        self.database = database
        if migrate:
            components = dict(self.COMPONENTS)

            async def apply(conn: Any) -> None:
                await migrate_all(conn, components)

            from nexura_postgres.migrations import MIGRATION_TIMEOUT

            database.run(apply, timeout=MIGRATION_TIMEOUT + 30)

    # ---- binding -----------------------------------------------------------------------
    def import_state(
        self,
        workspaces: Iterable[Workspace],
        ownership: Mapping[tuple[str, str], tuple[str, str]],
    ) -> None:
        """Add in-memory records that the database lacks; stored rows win (as SQLite binding)."""
        rows = list(workspaces)
        owned = list(ownership.items())

        async def run(conn: Any) -> None:
            for ws in rows:
                await insert_workspace(conn, ws)
            now = time.time()
            for (r_type, r_id), (ws_id, o_id) in owned:
                await conn.execute(
                    "INSERT INTO nexura_service_resource_ownership "
                    "(resource_type, resource_id, workspace_id, owner_id, created_at) "
                    "VALUES ($1,$2,$3,$4,$5) ON CONFLICT (resource_type, resource_id) DO NOTHING",
                    r_type,
                    r_id,
                    ws_id,
                    o_id,
                    now,
                )

        self.database.run(run)

    # ---- resource ownership ------------------------------------------------------------
    def record_resource_ownership(
        self, resource_type: str, resource_id: str, workspace_id: str, owner_id: str
    ) -> None:
        async def run(conn: Any) -> None:
            await conn.execute(
                "INSERT INTO nexura_service_resource_ownership "
                "(resource_type, resource_id, workspace_id, owner_id, created_at) "
                "VALUES ($1,$2,$3,$4,$5) ON CONFLICT (resource_type, resource_id) DO UPDATE SET "
                "workspace_id = EXCLUDED.workspace_id, owner_id = EXCLUDED.owner_id, "
                "created_at = EXCLUDED.created_at",
                resource_type,
                resource_id,
                workspace_id,
                owner_id,
                time.time(),
            )

        self.database.run(run, autocommit=True)

    def get_resource_ownership(
        self, resource_type: str, resource_id: str
    ) -> tuple[str, str] | None:
        async def run(conn: Any) -> Any:
            return await conn.fetchrow(
                "SELECT workspace_id, owner_id FROM nexura_service_resource_ownership "
                "WHERE resource_type = $1 AND resource_id = $2",
                resource_type,
                resource_id,
            )

        row = self.database.run(run, autocommit=True)
        return None if row is None else (row["workspace_id"], row["owner_id"])

    def get_resource_ownerships(
        self, keys: Iterable[tuple[str, str]]
    ) -> dict[tuple[str, str], tuple[str, str]]:
        """``get_resource_ownership`` for many ``(resource_type, resource_id)`` keys in one
        query (MVP WP11a); keys without a row are left out."""
        wanted = list(dict.fromkeys(keys))
        if not wanted:
            return {}

        async def run(conn: Any) -> Any:
            return await conn.fetch(
                "SELECT o.resource_type, o.resource_id, o.workspace_id, o.owner_id "
                "FROM unnest($1::text[], $2::text[]) AS k(resource_type, resource_id) "
                "JOIN nexura_service_resource_ownership o "
                "ON o.resource_type = k.resource_type AND o.resource_id = k.resource_id",
                [t for t, _ in wanted],
                [i for _, i in wanted],
            )

        return {
            (row["resource_type"], row["resource_id"]): (row["workspace_id"], row["owner_id"])
            for row in self.database.run(run, autocommit=True)
        }

    def list_workspace_resource_ids(self, resource_type: str, workspace_id: str) -> list[str]:
        async def run(conn: Any) -> Any:
            return await conn.fetch(
                "SELECT resource_id FROM nexura_service_resource_ownership "
                "WHERE workspace_id = $1 AND resource_type = $2 ORDER BY seq",
                workspace_id,
                resource_type,
            )

        return [row["resource_id"] for row in self.database.run(run, autocommit=True)]

    def list_owned_resource_ids(
        self, resource_type: str, workspace_id: str, owner_id: str
    ) -> list[str]:
        async def run(conn: Any) -> Any:
            return await conn.fetch(
                "SELECT resource_id FROM nexura_service_resource_ownership "
                "WHERE workspace_id = $1 AND resource_type = $2 AND owner_id = $3 ORDER BY seq",
                workspace_id,
                resource_type,
                owner_id,
            )

        return [row["resource_id"] for row in self.database.run(run, autocommit=True)]

    def delete_resource_ownership(self, resource_type: str, resource_id: str) -> bool:
        async def run(conn: Any) -> Any:
            return await conn.fetchval(
                "DELETE FROM nexura_service_resource_ownership "
                "WHERE resource_type = $1 AND resource_id = $2 RETURNING 1",
                resource_type,
                resource_id,
            )

        return self.database.run(run) is not None

    # ---- workspaces --------------------------------------------------------------------
    def create_workspace(self, ws: Workspace) -> Workspace:
        """Insert ``ws``; ``WorkspaceExistsError`` when the id is taken (on any instance)."""

        async def run(conn: Any) -> None:
            await conn.execute(WORKSPACE_LOCK_SQL, ws.workspace_id)
            if not await insert_workspace(conn, ws):
                raise WorkspaceExistsError(ws.workspace_id)

        self.database.run(run)
        return ws

    def update_workspace_metadata(self, workspace_id: str, updates: dict[str, Any]) -> Workspace:
        async def run(conn: Any) -> Any:
            await conn.execute(WORKSPACE_LOCK_SQL, workspace_id)
            row = await conn.fetchrow(
                f"SELECT {_WORKSPACE_COLUMNS} FROM nexura_service_workspaces "
                "WHERE workspace_id = $1 FOR UPDATE",
                workspace_id,
            )
            if row is None:
                raise KeyError(workspace_id)
            ws = workspace_from_row(row)
            ws.metadata.update(updates)
            await conn.execute(
                "UPDATE nexura_service_workspaces SET metadata_json = $1 WHERE workspace_id = $2",
                json.dumps(ws.metadata),
                workspace_id,
            )
            return ws

        result: Workspace = self.database.run(run)
        return result

    def get_workspace(self, workspace_id: str) -> Workspace | None:
        async def run(conn: Any) -> Any:
            return await conn.fetchrow(
                f"SELECT {_WORKSPACE_COLUMNS} FROM nexura_service_workspaces "
                "WHERE workspace_id = $1",
                workspace_id,
            )

        row = self.database.run(run, autocommit=True)
        return None if row is None else workspace_from_row(row)

    def list_workspaces(self) -> list[Workspace]:
        """``default`` first, then creation order (the SQLite repository's dict order)."""

        async def run(conn: Any) -> Any:
            return await conn.fetch(
                f"SELECT {_WORKSPACE_COLUMNS} FROM nexura_service_workspaces "
                "ORDER BY workspace_id <> 'default', seq"
            )

        return [workspace_from_row(row) for row in self.database.run(run, autocommit=True)]
