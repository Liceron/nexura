# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Versioned skills on Postgres (MVP Postgres WP4, store #10).

``PostgresSkillStore`` has the API, errors and limits of ``nexura_runtime.skills_store.SkillStore``
(it is a subclass, and both apply the same rule functions of that module to the rows they read).
The SQLite store serialises every mutation with ``BEGIN IMMEDIATE``; here (design §2b-5):

- every mutation of one workspace (create, set label, remove) takes a transaction-level advisory
  lock on that workspace first, then reads the skill row, applies the rule and writes it back.
  So two instances publishing at once never lose a version or reuse a version number, and the
  per-workspace skill limit holds;
- the skill row is written with ``INSERT … ON CONFLICT DO UPDATE``, and the counter of a removed
  skill with ``ON CONFLICT DO UPDATE SET next_version = GREATEST(…)`` (SQLite's ``MAX``), so the
  number never goes down.

Tables keep the SQLite names (``nexura_service_skills``, ``nexura_service_skill_counters``);
``data`` is the same JSON text (``TEXT``). Lists are ordered by name in byte order
(``COLLATE "C"``), as SQLite orders them. Pooled-endpoint safety: one transaction per call,
transaction-scoped locks, no session state.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import Any

from nexura_agents.skills import Skill, SkillError, SkillLibrary

from nexura_postgres.migrations import MIGRATION_TIMEOUT, Migration, migrate_all, register
from nexura_runtime.skills_store import (
    SkillStore,
    add_for_run,
    apply_create,
    apply_remove,
    apply_set_label,
    library_from_data,
    pick_version,
    skill_summaries,
    skill_summary,
    stored_form,
    version_view,
    versions_view,
)

COMPONENT = "skills"
MIGRATIONS: tuple[Migration, ...] = (
    (
        1,
        "versioned skills and removed-skill counters",
        (
            """CREATE TABLE nexura_service_skills (
                workspace_id TEXT NOT NULL,
                name TEXT NOT NULL,
                data TEXT NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL,
                updated_by TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (workspace_id, name))""",
            """CREATE TABLE nexura_service_skill_counters (
                workspace_id TEXT NOT NULL,
                name TEXT NOT NULL,
                next_version BIGINT NOT NULL,
                PRIMARY KEY (workspace_id, name))""",
        ),
    ),
)
register(COMPONENT, MIGRATIONS)

#: Serialises every skill mutation of one workspace, on every instance (transaction-scoped).
WORKSPACE_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtextextended('nexura-skills/' || $1, 0))"


def _storable(*values: str) -> bool:
    """Postgres ``TEXT`` cannot hold NUL; such a workspace or name can never have been stored."""
    return not any("\x00" in value for value in values)


async def _load_data(conn: Any, workspace_id: str, name: str) -> str | None:
    if not _storable(workspace_id, name):
        return None
    data: str | None = await conn.fetchval(
        "SELECT data FROM nexura_service_skills WHERE workspace_id = $1 AND name = $2",
        workspace_id,
        name,
    )
    return data


async def _load(conn: Any, workspace_id: str, name: str) -> SkillLibrary | None:
    data = await _load_data(conn, workspace_id, name)
    return None if data is None else library_from_data(data)


def _parse(data: str | None) -> SkillLibrary | None:
    return None if data is None else library_from_data(data)


async def _store(
    conn: Any, workspace_id: str, name: str, library: SkillLibrary, actor: str
) -> None:
    data, next_version = stored_form(library, name)
    if data is None:
        await conn.execute(
            "DELETE FROM nexura_service_skills WHERE workspace_id = $1 AND name = $2",
            workspace_id,
            name,
        )
        await conn.execute(
            "INSERT INTO nexura_service_skill_counters (workspace_id, name, next_version) "
            "VALUES ($1, $2, $3) ON CONFLICT (workspace_id, name) DO UPDATE SET "
            "next_version = GREATEST(nexura_service_skill_counters.next_version, "
            "EXCLUDED.next_version)",
            workspace_id,
            name,
            next_version,
        )
        return
    await conn.execute(
        "INSERT INTO nexura_service_skills (workspace_id, name, data, updated_at, updated_by) "
        "VALUES ($1, $2, $3, $4, $5) ON CONFLICT (workspace_id, name) DO UPDATE SET "
        "data = EXCLUDED.data, updated_at = EXCLUDED.updated_at, "
        "updated_by = EXCLUDED.updated_by",
        workspace_id,
        name,
        data,
        time.time(),
        actor,
    )


class PostgresSkillStore(SkillStore):
    """Workspace-scoped, versioned skills in the shared Postgres ``Database``.

    ``database``: a ``nexura_postgres.Database`` (the service's shared pool); the store never
    closes it. Opening the store applies its migrations (component ``skills``).
    """

    def __init__(self, database: Any, *, migrate: bool = True) -> None:
        # SkillStore.__init__ is not called: it opens the SQLite file.
        self.database = database
        if migrate:
            components = {COMPONENT: MIGRATIONS}

            async def apply(conn: Any) -> None:
                await migrate_all(conn, components)

            database.run(apply, timeout=MIGRATION_TIMEOUT + 30)

    @staticmethod
    def _writable(workspace_id: str, actor: str) -> None:
        if not _storable(workspace_id, actor):
            raise SkillError("The workspace and actor cannot contain NUL characters.")

    def list_skills(self, workspace_id: str, label: str | None = None) -> list[dict[str, Any]]:
        """Summaries of the workspace's skills, optionally only those carrying ``label``."""
        if not _storable(workspace_id):
            return []

        async def run(conn: Any) -> Any:
            return await conn.fetch(
                "SELECT name, data FROM nexura_service_skills WHERE workspace_id = $1 "
                'ORDER BY name COLLATE "C"',
                workspace_id,
            )

        rows = self.database.run(run)
        return skill_summaries(((row["name"], row["data"]) for row in rows), label)

    def _read(self, workspace_id: str, name: str) -> SkillLibrary | None:
        """A read: the row is fetched in the transaction and parsed after it (off the database
        thread)."""

        async def run(conn: Any) -> str | None:
            return await _load_data(conn, workspace_id, name)

        return _parse(self.database.run(run))

    def get(
        self,
        workspace_id: str,
        name: str,
        *,
        version: int | None = None,
        label: str | None = None,
    ) -> dict[str, Any]:
        """One version (by number, else by label, else the newest) with its full text."""
        return pick_version(self._read(workspace_id, name), name, version, label)

    def versions(self, workspace_id: str, name: str) -> dict[str, Any]:
        """Summary plus every version (with full text) of one skill."""
        return versions_view(self._read(workspace_id, name), name)

    def create(
        self,
        workspace_id: str,
        markdown: str,
        *,
        resources: Mapping[str, str] | None = None,
        labels: Sequence[str] = (),
        actor: str = "",
    ) -> dict[str, Any]:
        """Store ``SKILL.md`` text (and text resources) as a new version; same text is a no-op."""
        skill = Skill.from_markdown(markdown, resources=dict(resources or {}))
        self._writable(workspace_id, actor)

        async def run(conn: Any) -> dict[str, Any]:
            await conn.execute(WORKSPACE_LOCK_SQL, workspace_id)
            stored = await _load(conn, workspace_id, skill.name)
            count = counter = None
            if stored is None:
                count = int(
                    await conn.fetchval(
                        "SELECT COUNT(*) FROM nexura_service_skills WHERE workspace_id = $1",
                        workspace_id,
                    )
                )
                counter = await conn.fetchval(
                    "SELECT next_version FROM nexura_service_skill_counters "
                    "WHERE workspace_id = $1 AND name = $2",
                    workspace_id,
                    skill.name,
                )
            library, added = apply_create(
                stored,
                skill,
                labels,
                count=lambda: int(count or 0),
                counter=lambda: None if counter is None else int(counter),
            )
            await _store(conn, workspace_id, skill.name, library, actor)
            return version_view(library, skill.name, added)

        result: dict[str, Any] = self.database.run(run)
        return result

    def set_label(
        self, workspace_id: str, name: str, label: str, version: int, *, actor: str = ""
    ) -> dict[str, Any]:
        """Point ``label`` at ``version`` (``latest`` only at the newest version)."""
        self._writable(workspace_id, actor)

        async def run(conn: Any) -> dict[str, Any]:
            await conn.execute(WORKSPACE_LOCK_SQL, workspace_id)
            library = apply_set_label(await _load(conn, workspace_id, name), name, label, version)
            await _store(conn, workspace_id, name, library, actor)
            return skill_summary(library, name)

        result: dict[str, Any] = self.database.run(run)
        return result

    def remove(
        self, workspace_id: str, name: str, version: int | None = None, *, actor: str = ""
    ) -> int:
        """Delete a skill (all versions) or one version; returns how many were removed."""
        self._writable(workspace_id, actor)

        async def run(conn: Any) -> int:
            await conn.execute(WORKSPACE_LOCK_SQL, workspace_id)
            library = await _load(conn, workspace_id, name)
            removed = apply_remove(library, name, version)
            assert library is not None  # apply_remove refuses a missing skill
            await _store(conn, workspace_id, name, library, actor)
            return removed

        result: int = self.database.run(run)
        return result

    def library_for(self, workspace_id: str, names: Sequence[str], label: str) -> SkillLibrary:
        """A library of just ``names`` for one run; refuses a name or label that does not exist.

        All names are read in one transaction."""
        wanted = list(names)

        async def run(conn: Any) -> list[str | None]:
            return [await _load_data(conn, workspace_id, name) for name in wanted]

        stored = self.database.run(run)
        library = SkillLibrary()
        for name, data in zip(wanted, stored, strict=True):
            add_for_run(library, _parse(data), name, label)
        return library

    def close(self) -> None:
        """Nothing to release: the shared ``Database`` belongs to the app."""


__all__ = ["COMPONENT", "MIGRATIONS", "PostgresSkillStore"]
