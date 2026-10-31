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
from nexura_runtime.service.workspaces.models import Workspace, WorkspaceDeletion
from nexura_runtime.service.workspaces.repository import (
    WorkspaceDeletedError,
    WorkspaceExistsError,
    deletion_from_row,
)

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
    (
        2,
        "workspace deletion: tombstones and deletion records",
        (
            # P46: a deleted workspace keeps its row (the id is retired); its content purge is
            # tracked in a deletion record until a final pass marks it done.
            "ALTER TABLE nexura_service_workspaces ADD COLUMN deleted_at DOUBLE PRECISION",
            """CREATE TABLE nexura_workspace_deletions (
                workspace_id TEXT PRIMARY KEY,
                requested_by TEXT NOT NULL,
                requested_at DOUBLE PRECISION NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                completed_at DOUBLE PRECISION)""",
            """CREATE INDEX idx_nexura_workspace_deletions_status
                ON nexura_workspace_deletions (status, requested_at)""",
        ),
    ),
)
register(COMPONENT, MIGRATIONS)

WORKSPACE_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtextextended('nexura-ws/' || $1, 0))"
_WORKSPACE_COLUMNS = "workspace_id, name, owner_id, created_at, metadata_json"
_DELETION_COLUMNS = (
    "workspace_id, requested_by, requested_at, status, attempts, last_error, completed_at"
)


def workspace_from_row(row: Any) -> Workspace:
    meta = row["metadata_json"]
    return Workspace(
        workspace_id=row["workspace_id"],
        name=row["name"],
        owner_id=row["owner_id"],
        created_at=row["created_at"],
        metadata=json.loads(meta) if meta else {},
    )


async def workspace_deleted(conn: Any, workspace_id: str) -> bool:
    """True when ``workspace_id`` has a tombstone (it was deleted; the id is retired)."""
    deleted = await conn.fetchval(
        "SELECT 1 FROM nexura_service_workspaces WHERE workspace_id = $1 "
        "AND deleted_at IS NOT NULL",
        workspace_id,
    )
    return deleted is not None


async def insert_workspace(conn: Any, ws: Workspace) -> bool:
    """Insert ``ws`` unless its id exists (a deleted workspace's tombstone included, so a
    retired id is never created again); True when inserted."""
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
                if await workspace_deleted(conn, ws.workspace_id):
                    raise WorkspaceDeletedError(ws.workspace_id)
                raise WorkspaceExistsError(ws.workspace_id)

        self.database.run(run)
        return ws

    def update_workspace_metadata(self, workspace_id: str, updates: dict[str, Any]) -> Workspace:
        async def run(conn: Any) -> Any:
            await conn.execute(WORKSPACE_LOCK_SQL, workspace_id)
            row = await conn.fetchrow(
                f"SELECT {_WORKSPACE_COLUMNS} FROM nexura_service_workspaces "
                "WHERE workspace_id = $1 AND deleted_at IS NULL FOR UPDATE",
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
                "WHERE workspace_id = $1 AND deleted_at IS NULL",
                workspace_id,
            )

        row = self.database.run(run, autocommit=True)
        return None if row is None else workspace_from_row(row)

    def list_workspaces(self) -> list[Workspace]:
        """``default`` first, then creation order (the SQLite repository's dict order)."""

        async def run(conn: Any) -> Any:
            return await conn.fetch(
                f"SELECT {_WORKSPACE_COLUMNS} FROM nexura_service_workspaces "
                "WHERE deleted_at IS NULL ORDER BY workspace_id <> 'default', seq"
            )

        return [workspace_from_row(row) for row in self.database.run(run, autocommit=True)]

    # ---- deletion (P46) ----------------------------------------------------------------
    def is_workspace_deleted(self, workspace_id: str) -> bool:
        async def run(conn: Any) -> bool:
            return await workspace_deleted(conn, workspace_id)

        return bool(self.database.run(run, autocommit=True))

    def retire_workspace(
        self, workspace_id: str, requested_by: str, at: float
    ) -> tuple[WorkspaceDeletion, bool]:
        """Tombstone ``workspace_id`` and write its deletion record in one transaction, under
        the workspace lock (see ``WorkspaceRepository.retire_workspace``)."""

        async def run(conn: Any) -> tuple[WorkspaceDeletion, bool]:
            await conn.execute(WORKSPACE_LOCK_SQL, workspace_id)
            row = await conn.fetchrow(
                "SELECT deleted_at FROM nexura_service_workspaces WHERE workspace_id = $1 "
                "FOR UPDATE",
                workspace_id,
            )
            if row is None:
                raise KeyError(workspace_id)
            if row["deleted_at"] is not None:
                existing = await conn.fetchrow(
                    f"SELECT {_DELETION_COLUMNS} FROM nexura_workspace_deletions "
                    "WHERE workspace_id = $1",
                    workspace_id,
                )
                if existing is not None:
                    return deletion_from_row(existing), False
                return (
                    WorkspaceDeletion(
                        workspace_id=workspace_id,
                        requested_by="",
                        requested_at=row["deleted_at"],
                    ),
                    False,
                )
            await conn.execute(
                "UPDATE nexura_service_workspaces SET deleted_at = $2 WHERE workspace_id = $1",
                workspace_id,
                at,
            )
            deletion = WorkspaceDeletion(
                workspace_id=workspace_id, requested_by=requested_by, requested_at=at
            )
            await conn.execute(
                f"INSERT INTO nexura_workspace_deletions ({_DELETION_COLUMNS}) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7) ON CONFLICT (workspace_id) DO NOTHING",
                deletion.workspace_id,
                deletion.requested_by,
                deletion.requested_at,
                deletion.status,
                deletion.attempts,
                deletion.last_error,
                deletion.completed_at,
            )
            return deletion, True

        result: tuple[WorkspaceDeletion, bool] = self.database.run(run)
        return result

    def get_workspace_deletion(self, workspace_id: str) -> WorkspaceDeletion | None:
        async def run(conn: Any) -> Any:
            return await conn.fetchrow(
                f"SELECT {_DELETION_COLUMNS} FROM nexura_workspace_deletions "
                "WHERE workspace_id = $1",
                workspace_id,
            )

        row = self.database.run(run, autocommit=True)
        return None if row is None else deletion_from_row(row)

    def list_workspace_deletions(self, status: str | None) -> list[WorkspaceDeletion]:
        async def run(conn: Any) -> Any:
            return await conn.fetch(
                f"SELECT {_DELETION_COLUMNS} FROM nexura_workspace_deletions "
                "WHERE $1::text IS NULL OR status = $1 ORDER BY requested_at, workspace_id",
                status,
            )

        return [deletion_from_row(row) for row in self.database.run(run, autocommit=True)]

    def record_purge_attempt(
        self, workspace_id: str, done: bool, error: str | None, at: float
    ) -> WorkspaceDeletion | None:
        async def run(conn: Any) -> Any:
            await conn.execute(
                "UPDATE nexura_workspace_deletions SET attempts = attempts + 1, "
                "last_error = $2, status = CASE WHEN $3 THEN 'done' ELSE status END, "
                "completed_at = CASE WHEN $3 THEN $4 ELSE completed_at END "
                "WHERE workspace_id = $1 AND status <> 'done'",
                workspace_id,
                error,
                done,
                at,
            )
            return await conn.fetchrow(
                f"SELECT {_DELETION_COLUMNS} FROM nexura_workspace_deletions "
                "WHERE workspace_id = $1",
                workspace_id,
            )

        row = self.database.run(run)
        return None if row is None else deletion_from_row(row)

    def delete_workspace_ownership(self, workspace_id: str) -> int:
        async def run(conn: Any) -> Any:
            return await conn.execute(
                "DELETE FROM nexura_service_resource_ownership WHERE workspace_id = $1",
                workspace_id,
            )

        status = self.database.run(run, autocommit=True)
        return int(str(status).split()[-1])
