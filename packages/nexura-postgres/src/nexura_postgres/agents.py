# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Saved agents on Postgres (MVP Postgres WP4, store #9).

``PostgresAgentStore`` has the API, errors and limits of ``nexura_runtime.agents_store.AgentStore``
(it is a subclass and shares its row converter, revision check and list view). The SQLite store
gets its guarantees from ``BEGIN IMMEDIATE``, a lock on the whole file; here (design §2b-5):

- every mutation of one workspace (create, update, delete) takes a transaction-level advisory
  lock on that workspace first, so the per-workspace limit (``MAX_AGENTS_PER_WORKSPACE``) and
  the duplicate-name rule hold when several instances create agents at once. A plain
  read-then-write check would not: under READ COMMITTED two instances could both count 99;
- ``UNIQUE (workspace_id, name)`` is the backstop for the name rule (a violation is reported as
  ``AgentNameTakenError``);
- update and delete are compare-and-set on the stored revision (``WHERE revision = $n``).

The table keeps the SQLite name (``nexura_service_agents``) and columns; ``spec`` is the same JSON
text (``TEXT``, never ``JSONB``). The list is ordered by name in byte order (``COLLATE "C"``), as
SQLite orders it. Pooled-endpoint safety: one transaction per call, transaction-scoped locks, no
session state.
"""

from __future__ import annotations

import json
import time
from typing import Any

from nexura_postgres.migrations import MIGRATION_TIMEOUT, Migration, migrate_all, register
from nexura_runtime import agents_store
from nexura_runtime.agents_spec import AgentSpec
from nexura_runtime.agents_store import (
    AgentLimitError,
    AgentNameTakenError,
    AgentNotFoundError,
    AgentStore,
    AgentStoreError,
    agent_summary,
    new_agent_id,
)

COMPONENT = "agents"
MIGRATIONS: tuple[Migration, ...] = (
    (
        1,
        "saved agents",
        (
            """CREATE TABLE nexura_service_agents (
                workspace_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                name TEXT NOT NULL,
                spec TEXT NOT NULL,
                revision BIGINT NOT NULL,
                created_at DOUBLE PRECISION NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL,
                created_by TEXT NOT NULL DEFAULT '',
                updated_by TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (workspace_id, agent_id),
                CONSTRAINT uq_service_agents_name UNIQUE (workspace_id, name))""",
        ),
    ),
)
register(COMPONENT, MIGRATIONS)

#: Serialises every agent mutation of one workspace, on every instance (transaction-scoped).
WORKSPACE_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtextextended('nexura-agents/' || $1, 0))"
_NAME_CONSTRAINT = "uq_service_agents_name"


def _storable(*values: str) -> bool:
    """Postgres ``TEXT`` cannot hold NUL; such an id or name can never have been stored."""
    return not any("\x00" in value for value in values)


def _name_taken(name: str) -> AgentNameTakenError:
    return AgentNameTakenError(f"An agent named '{name}' already exists.")


class PostgresAgentStore(AgentStore):
    """Workspace-scoped saved agents in the shared Postgres ``Database``.

    ``database``: a ``nexura_postgres.Database`` (the service's shared pool); the store never
    closes it. Opening the store applies its migrations (component ``agents``).
    """

    def __init__(self, database: Any, *, migrate: bool = True) -> None:
        # AgentStore.__init__ is not called: it opens the SQLite file.
        self.database = database
        if migrate:
            components = {COMPONENT: MIGRATIONS}

            async def apply(conn: Any) -> None:
                await migrate_all(conn, components)

            database.run(apply, timeout=MIGRATION_TIMEOUT + 30)

    @staticmethod
    async def _fetch(conn: Any, workspace_id: str, agent_id: str, *, lock: bool = False) -> Any:
        row = await conn.fetchrow(
            f"SELECT {AgentStore._COLUMNS} FROM nexura_service_agents "
            "WHERE workspace_id = $1 AND agent_id = $2" + (" FOR UPDATE" if lock else ""),
            workspace_id,
            agent_id,
        )
        return None if row is None else tuple(row)

    def list_agents(self, workspace_id: str) -> list[dict[str, Any]]:
        """Summaries of the workspace's agents, by name (byte order)."""
        if not _storable(workspace_id):
            return []

        async def run(conn: Any) -> Any:
            return await conn.fetch(
                f"SELECT {self._COLUMNS} FROM nexura_service_agents "
                'WHERE workspace_id = $1 ORDER BY name COLLATE "C"',
                workspace_id,
            )

        rows = self.database.run(run, autocommit=True)  # one statement (MVP WP11a)
        return [agent_summary(self._record(tuple(row))) for row in rows]

    def get(self, workspace_id: str, agent_id: str) -> dict[str, Any]:
        """The stored record (spec, revision, timestamps); raises :class:`AgentNotFoundError`."""
        row = None
        if _storable(workspace_id, agent_id):

            async def run(conn: Any) -> Any:
                return await self._fetch(conn, workspace_id, agent_id)

            row = self.database.run(run)
        if row is None:
            raise AgentNotFoundError(f"Agent '{agent_id}' not found.")
        return self._record(row)

    def create(
        self, workspace_id: str, spec: AgentSpec, *, actor: str = "", agent_id: str | None = None
    ) -> dict[str, Any]:
        """Store a new agent at revision 1 (limit and unique name hold across instances)."""
        new_id = agent_id or new_agent_id()
        if not _storable(workspace_id, new_id, actor):
            raise AgentStoreError("Workspace, agent id and actor cannot contain NUL characters.")
        data = json.dumps(spec.model_dump(mode="json"), ensure_ascii=False)
        now = time.time()

        async def run(conn: Any) -> Any:
            await conn.execute(WORKSPACE_LOCK_SQL, workspace_id)
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM nexura_service_agents WHERE workspace_id = $1",
                workspace_id,
            )
            # Read at call time, as the SQLite store reads its module constant.
            limit = agents_store.MAX_AGENTS_PER_WORKSPACE
            if count >= limit:
                raise AgentLimitError(f"A workspace may hold at most {limit} agents.")
            await self._check_name_pg(conn, workspace_id, spec.name, None)
            existing = await self._fetch(conn, workspace_id, new_id)
            if existing is not None:
                raise AgentStoreError(f"Agent '{new_id}' already exists.")
            await self._write(
                conn,
                f"INSERT INTO nexura_service_agents ({self._COLUMNS}) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
                spec.name,
                workspace_id,
                new_id,
                spec.name,
                data,
                1,
                now,
                now,
                actor,
                actor,
            )
            return await self._fetch(conn, workspace_id, new_id)

        return self._record(self.database.run(run))

    @staticmethod
    async def _check_name_pg(conn: Any, workspace_id: str, name: str, agent_id: str | None) -> None:
        clash = await conn.fetchval(
            "SELECT agent_id FROM nexura_service_agents WHERE workspace_id = $1 AND name = $2",
            workspace_id,
            name,
        )
        if clash is not None and clash != agent_id:
            raise _name_taken(name)

    @staticmethod
    async def _write(conn: Any, sql: str, name: str, *args: Any) -> str:
        """Run one write; a violation of the unique name constraint is ``AgentNameTakenError``."""
        import asyncpg  # type: ignore[import-untyped]

        try:
            result: str = await conn.execute(sql, *args)
        except asyncpg.UniqueViolationError as exc:
            if getattr(exc, "constraint_name", None) == _NAME_CONSTRAINT:
                raise _name_taken(name) from exc
            raise
        return result

    def update(
        self,
        workspace_id: str,
        agent_id: str,
        spec: AgentSpec,
        *,
        expected_revision: int | None,
        actor: str = "",
    ) -> dict[str, Any]:
        """Replace the spec; the revision goes up by one. Same spec is a no-op."""
        if not _storable(workspace_id, agent_id):
            raise AgentNotFoundError(f"Agent '{agent_id}' not found.")
        if not _storable(actor):
            raise AgentStoreError("The actor cannot contain NUL characters.")
        data = json.dumps(spec.model_dump(mode="json"), ensure_ascii=False)

        async def run(conn: Any) -> Any:
            await conn.execute(WORKSPACE_LOCK_SQL, workspace_id)
            row = await self._fetch(conn, workspace_id, agent_id, lock=True)
            if row is None:
                raise AgentNotFoundError(f"Agent '{agent_id}' not found.")
            stored = int(row[4])
            self._check_revision(agent_id, stored, expected_revision)
            if json.loads(row[3]) == json.loads(data):
                return row
            await self._check_name_pg(conn, workspace_id, spec.name, agent_id)
            status = await self._write(
                conn,
                "UPDATE nexura_service_agents SET name = $1, spec = $2, revision = $3, "
                "updated_at = $4, updated_by = $5 "
                "WHERE workspace_id = $6 AND agent_id = $7 AND revision = $8",
                spec.name,
                spec.name,
                data,
                stored + 1,
                time.time(),
                actor,
                workspace_id,
                agent_id,
                stored,
            )
            if status != "UPDATE 1":  # pragma: no cover - the row lock makes this unreachable
                current = await self._fetch(conn, workspace_id, agent_id)
                if current is None:
                    raise AgentNotFoundError(f"Agent '{agent_id}' not found.")
                self._check_revision(agent_id, int(current[4]), stored)
            return await self._fetch(conn, workspace_id, agent_id)

        return self._record(self.database.run(run))

    def delete(
        self, workspace_id: str, agent_id: str, *, expected_revision: int | None = None
    ) -> None:
        """Remove the agent; workflows naming it fail at run time from then on."""
        if not _storable(workspace_id, agent_id):
            raise AgentNotFoundError(f"Agent '{agent_id}' not found.")

        async def run(conn: Any) -> None:
            await conn.execute(WORKSPACE_LOCK_SQL, workspace_id)
            row = await self._fetch(conn, workspace_id, agent_id, lock=True)
            if row is None:
                raise AgentNotFoundError(f"Agent '{agent_id}' not found.")
            stored = int(row[4])
            self._check_revision(agent_id, stored, expected_revision)
            deleted = await conn.fetchval(
                "DELETE FROM nexura_service_agents "
                "WHERE workspace_id = $1 AND agent_id = $2 AND revision = $3 RETURNING 1",
                workspace_id,
                agent_id,
                stored,
            )
            if deleted is None:  # pragma: no cover - the row lock makes this unreachable
                raise AgentNotFoundError(f"Agent '{agent_id}' not found.")

        self.database.run(run)

    def close(self) -> None:
        """Nothing to release: the shared ``Database`` belongs to the app."""


__all__ = ["COMPONENT", "MIGRATIONS", "PostgresAgentStore"]
