# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Workspace-scoped, versioned skills persisted in the service database.

Each (workspace, skill name) is one row holding a ``SkillLibrary.export()`` of that single skill,
so versions, labels and the never-reused version counter follow the library's own rules. Every
mutation runs in one ``BEGIN IMMEDIATE`` transaction. Skills are text only: everything ``Skill``
refuses (scripts, non-text resources, oversize, bad names) is refused here with ``SkillError``.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Mapping, Sequence
from contextlib import closing
from pathlib import Path
from typing import Any

from nexura_agents.skills import Skill, SkillError, SkillLibrary

#: Most distinct skills one workspace may store.
MAX_SKILLS_PER_WORKSPACE = 200
#: Most versions kept per skill (remove old ones to add more).
MAX_VERSIONS_PER_SKILL = 100
#: Most distinct labels one skill may carry (``latest`` included).
MAX_LABELS_PER_SKILL = 20

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nexura_service_skills (
    workspace_id TEXT NOT NULL,
    name TEXT NOT NULL,
    data TEXT NOT NULL,
    updated_at REAL NOT NULL,
    updated_by TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (workspace_id, name)
)
"""
# Next version number of skills removed entirely, so a re-created skill does not reuse them.
_COUNTERS = """
CREATE TABLE IF NOT EXISTS nexura_service_skill_counters (
    workspace_id TEXT NOT NULL,
    name TEXT NOT NULL,
    next_version INTEGER NOT NULL,
    PRIMARY KEY (workspace_id, name)
)
"""


class SkillNotFoundError(SkillError):
    """The skill (or version) does not exist in the caller's workspace."""


class SkillStore:
    """SQLite-backed skills store; every call names the workspace it acts for."""

    def __init__(self, db_path: str | Path) -> None:
        self._path = str(db_path)
        self._lock = threading.RLock()
        with closing(self._connect()) as conn, conn:
            conn.execute(_SCHEMA)
            conn.execute(_COUNTERS)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=10.0, isolation_level=None)
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _load(self, conn: sqlite3.Connection, workspace_id: str, name: str) -> SkillLibrary | None:
        row = conn.execute(
            "SELECT data FROM nexura_service_skills WHERE workspace_id=? AND name=?",
            (workspace_id, name),
        ).fetchone()
        if row is None:
            return None
        library = SkillLibrary()
        library.import_versions(json.loads(row[0]))
        return library

    def _save(
        self,
        conn: sqlite3.Connection,
        workspace_id: str,
        name: str,
        library: SkillLibrary,
        actor: str,
    ) -> None:
        if name not in library.names():
            conn.execute(
                "DELETE FROM nexura_service_skills WHERE workspace_id=? AND name=?",
                (workspace_id, name),
            )
            conn.execute(
                "INSERT INTO nexura_service_skill_counters (workspace_id, name, next_version) "
                "VALUES (?,?,?) ON CONFLICT(workspace_id, name) DO UPDATE SET "
                "next_version=MAX(next_version, excluded.next_version)",
                (workspace_id, name, library.next_version(name)),
            )
            return
        data = json.dumps(library.export(), ensure_ascii=False)
        conn.execute(
            "INSERT INTO nexura_service_skills (workspace_id, name, data, updated_at, updated_by) "
            "VALUES (?,?,?,?,?) ON CONFLICT(workspace_id, name) DO UPDATE SET "
            "data=excluded.data, updated_at=excluded.updated_at, updated_by=excluded.updated_by",
            (workspace_id, name, data, time.time(), actor),
        )

    @staticmethod
    def _summary(library: SkillLibrary, name: str) -> dict[str, Any]:
        versions = library.versions(name)
        newest = versions[-1]
        return {
            "name": name,
            "description": newest.skill.description,
            "latest_version": newest.version,
            "version_count": len(versions),
            "labels": {label: v.version for v in versions for label in v.labels},
        }

    @staticmethod
    def _version_view(library: SkillLibrary, name: str, version: int) -> dict[str, Any]:
        for item in library.versions(name):
            if item.version == version:
                skill = item.skill
                return {
                    "name": name,
                    "version": item.version,
                    "labels": list(item.labels),
                    "description": skill.description,
                    "instructions": skill.instructions,
                    "tools": list(skill.tools),
                    "resources": dict(skill.resources),
                    "sha256": skill.sha256,
                }
        raise SkillNotFoundError(f"Skill {name!r} has no version {version}.")

    def list_skills(self, workspace_id: str, label: str | None = None) -> list[dict[str, Any]]:
        """Summaries of the workspace's skills, optionally only those carrying ``label``.

        Read from the stored export without rebuilding every version: rows are validated when
        written, and a summary needs only the newest version and the label pointers.
        """
        with self._lock, closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT name, data FROM nexura_service_skills WHERE workspace_id=? ORDER BY name",
                (workspace_id,),
            ).fetchall()
        out = []
        for name, data in rows:
            versions = json.loads(data).get("skills", {}).get(name, {}).get("versions") or []
            if not versions:
                continue
            labels = {lab: v["version"] for v in versions for lab in v.get("labels", ())}
            if label is not None and label not in labels:
                continue
            out.append(
                {
                    "name": name,
                    "description": versions[-1]["skill"].get("description", ""),
                    "latest_version": versions[-1]["version"],
                    "version_count": len(versions),
                    "labels": labels,
                }
            )
        return out

    def get(
        self,
        workspace_id: str,
        name: str,
        *,
        version: int | None = None,
        label: str | None = None,
    ) -> dict[str, Any]:
        """One version (by number, else by label, else the newest) with its full text."""
        with self._lock, closing(self._connect()) as conn:
            library = self._load(conn, workspace_id, name)
            if library is None:
                raise SkillNotFoundError(f"Unknown skill {name!r}.")
            if version is None:
                found = library.versions(name)
                if label is None:
                    version = found[-1].version
                else:
                    carrying = [v.version for v in found if label in v.labels]
                    if not carrying:
                        raise SkillNotFoundError(f"Skill {name!r} has no label {label!r}.")
                    version = carrying[0]
            return self._version_view(library, name, version)

    def versions(self, workspace_id: str, name: str) -> dict[str, Any]:
        """Summary plus every version (with full text) of one skill."""
        with self._lock, closing(self._connect()) as conn:
            library = self._load(conn, workspace_id, name)
            if library is None:
                raise SkillNotFoundError(f"Unknown skill {name!r}.")
            return {
                **self._summary(library, name),
                "versions": [
                    self._version_view(library, name, v.version) for v in library.versions(name)
                ],
            }

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
        with self._lock, closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                library = self._load(conn, workspace_id, skill.name)
                if library is None:
                    count = conn.execute(
                        "SELECT COUNT(*) FROM nexura_service_skills WHERE workspace_id=?",
                        (workspace_id,),
                    ).fetchone()[0]
                    if count >= MAX_SKILLS_PER_WORKSPACE:
                        raise SkillError(
                            f"A workspace may hold at most {MAX_SKILLS_PER_WORKSPACE} skills."
                        )
                    library = SkillLibrary()
                    counter = conn.execute(
                        "SELECT next_version FROM nexura_service_skill_counters "
                        "WHERE workspace_id=? AND name=?",
                        (workspace_id, skill.name),
                    ).fetchone()
                    if counter is not None:
                        library.continue_numbering(skill.name, int(counter[0]))
                elif len(library.versions(skill.name)) >= MAX_VERSIONS_PER_SKILL and (
                    library.versions(skill.name)[-1].skill.sha256 != skill.sha256
                ):
                    raise SkillError(
                        f"Skill {skill.name!r} already has {MAX_VERSIONS_PER_SKILL} versions; "
                        "remove old versions first."
                    )
                if len(set(labels)) >= MAX_LABELS_PER_SKILL:
                    raise SkillError(f"At most {MAX_LABELS_PER_SKILL - 1} labels per request.")
                held = (
                    {lab for v in library.versions(skill.name) for lab in v.labels}
                    if skill.name in library.names()
                    else set()
                )
                if len(held | set(labels) | {"latest"}) > MAX_LABELS_PER_SKILL:
                    raise SkillError(
                        f"Skill {skill.name!r} may carry at most {MAX_LABELS_PER_SKILL} labels; "
                        "reuse one (labels move between versions) instead of adding more."
                    )
                added = library.add(skill, labels=labels)
                self._save(conn, workspace_id, skill.name, library, actor)
                result = self._version_view(library, skill.name, added.version)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            return result

    def set_label(
        self, workspace_id: str, name: str, label: str, version: int, *, actor: str = ""
    ) -> dict[str, Any]:
        """Point ``label`` at ``version`` (``latest`` only at the newest version)."""
        with self._lock, closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                library = self._load(conn, workspace_id, name)
                if library is None:
                    raise SkillNotFoundError(f"Unknown skill {name!r}.")
                if not any(v.version == version for v in library.versions(name)):
                    raise SkillNotFoundError(f"Skill {name!r} has no version {version}.")
                labels = {lab for v in library.versions(name) for lab in v.labels}
                if label not in labels and len(labels) >= MAX_LABELS_PER_SKILL:
                    raise SkillError(
                        f"Skill {name!r} already has {MAX_LABELS_PER_SKILL} labels; "
                        "reuse one (labels move between versions) instead of adding more."
                    )
                library.set_label(name, label, version)
                self._save(conn, workspace_id, name, library, actor)
                result = self._summary(library, name)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            return result

    def remove(
        self, workspace_id: str, name: str, version: int | None = None, *, actor: str = ""
    ) -> int:
        """Delete a skill (all versions) or one version; returns how many were removed."""
        with self._lock, closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                library = self._load(conn, workspace_id, name)
                if library is None:
                    raise SkillNotFoundError(f"Unknown skill {name!r}.")
                if version is not None and not any(
                    v.version == version for v in library.versions(name)
                ):
                    raise SkillNotFoundError(f"Skill {name!r} has no version {version}.")
                removed = library.remove(name, version)
                self._save(conn, workspace_id, name, library, actor)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            return removed

    def library_for(self, workspace_id: str, names: Sequence[str], label: str) -> SkillLibrary:
        """A library of just ``names`` for one run; refuses a name or label that does not exist."""
        library = SkillLibrary()
        with self._lock, closing(self._connect()) as conn:
            for name in names:
                stored = self._load(conn, workspace_id, name)
                if stored is None:
                    raise SkillNotFoundError(f"Unknown skill {name!r} in this workspace.")
                if name not in stored.names_with_label(label):
                    raise SkillNotFoundError(f"Skill {name!r} has no label {label!r}.")
                data = stored.export()
                library.import_versions(
                    {"format": data["format"], "skills": {name: data["skills"][name]}}
                )
        return library


__all__ = [
    "MAX_SKILLS_PER_WORKSPACE",
    "MAX_LABELS_PER_SKILL",
    "MAX_VERSIONS_PER_SKILL",
    "SkillNotFoundError",
    "SkillStore",
]
