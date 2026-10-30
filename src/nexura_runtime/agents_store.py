# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Workspace-scoped saved agents persisted in the service database.

One row per (workspace, agent id) holds a validated :class:`~nexura_runtime.agents_spec.AgentSpec`
as JSON and a revision counter. Updates and deletes may name the revision the caller read
(``expected_revision``); a different stored revision is a conflict, so two editors cannot
silently overwrite each other. Names are unique per workspace. Every mutation runs in one
``BEGIN IMMEDIATE`` transaction.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

from nexura_runtime.agents_spec import AgentSpec

#: Most agents one workspace may store.
MAX_AGENTS_PER_WORKSPACE = 100

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nexura_service_agents (
    workspace_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    name TEXT NOT NULL,
    spec TEXT NOT NULL,
    revision INTEGER NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    updated_by TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (workspace_id, agent_id),
    UNIQUE (workspace_id, name)
)
"""


class AgentStoreError(Exception):
    """Base error of the agent store; ``code`` and ``status_code`` map to the API envelope."""

    code = "AGENT_INVALID"
    status_code = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class AgentNotFoundError(AgentStoreError):
    """The agent does not exist in the caller's workspace (other workspaces look the same)."""

    code = "AGENT_NOT_FOUND"
    status_code = 404


class AgentRevisionConflictError(AgentStoreError):
    """``expected_revision`` does not match the stored revision."""

    code = "REVISION_CONFLICT"
    status_code = 409

    def __init__(self, message: str, current_revision: int) -> None:
        super().__init__(message)
        self.current_revision = current_revision


class AgentNameTakenError(AgentStoreError):
    """Another agent of the workspace already has this name."""

    code = "AGENT_NAME_TAKEN"
    status_code = 409


class AgentLimitError(AgentStoreError):
    """The workspace already holds :data:`MAX_AGENTS_PER_WORKSPACE` agents."""

    code = "TOO_MANY_AGENTS"
    status_code = 413


def new_agent_id() -> str:
    """A fresh agent id: ``agt_`` and 16 hex digits."""
    return f"agt_{uuid.uuid4().hex[:16]}"


def agent_summary(record: dict[str, Any]) -> dict[str, Any]:
    """The list view of one stored record (shared by the SQLite and Postgres stores)."""
    spec = record["spec"]
    return {
        "agent_id": record["agent_id"],
        "name": spec.get("name", ""),
        "description": spec.get("description", ""),
        "model": spec.get("model", ""),
        "tool_count": len(spec.get("tools") or []),
        "tools": list(spec.get("tools") or []),
        "revision": record["revision"],
        "updated_at": record["updated_at"],
    }


class AgentStore:
    """SQLite-backed agent store; every call names the workspace it acts for."""

    def __init__(self, db_path: str | Path) -> None:
        self._path = str(db_path)
        self._lock = threading.RLock()
        with closing(self._connect()) as conn, conn:
            conn.execute(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=10.0, isolation_level=None)
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    @staticmethod
    def _record(row: tuple[Any, ...]) -> dict[str, Any]:
        workspace_id, agent_id, _, spec, revision, created_at, updated_at, c_by, u_by = row
        return {
            "agent_id": agent_id,
            "workspace_id": workspace_id,
            "revision": revision,
            "spec": json.loads(spec),
            "created_at": created_at,
            "updated_at": updated_at,
            "created_by": c_by,
            "updated_by": u_by,
        }

    _COLUMNS = (
        "workspace_id, agent_id, name, spec, revision, created_at, updated_at, "
        "created_by, updated_by"
    )

    def _row(self, conn: sqlite3.Connection, workspace_id: str, agent_id: str) -> Any:
        return conn.execute(
            f"SELECT {self._COLUMNS} FROM nexura_service_agents "
            "WHERE workspace_id=? AND agent_id=?",
            (workspace_id, agent_id),
        ).fetchone()

    def list_agents(self, workspace_id: str) -> list[dict[str, Any]]:
        """Summaries of the workspace's agents, by name."""
        with self._lock, closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT {self._COLUMNS} FROM nexura_service_agents "
                "WHERE workspace_id=? ORDER BY name",
                (workspace_id,),
            ).fetchall()
        return [agent_summary(self._record(row)) for row in rows]

    def get(self, workspace_id: str, agent_id: str) -> dict[str, Any]:
        """The stored record (spec, revision, timestamps); raises :class:`AgentNotFoundError`."""
        with self._lock, closing(self._connect()) as conn:
            row = self._row(conn, workspace_id, agent_id)
        if row is None:
            raise AgentNotFoundError(f"Agent '{agent_id}' not found.")
        return self._record(row)

    def get_spec(self, workspace_id: str, agent_id: str) -> tuple[AgentSpec, int]:
        """The validated spec and its revision, for running the agent."""
        record = self.get(workspace_id, agent_id)
        return AgentSpec.model_validate(record["spec"]), int(record["revision"])

    def create(
        self, workspace_id: str, spec: AgentSpec, *, actor: str = "", agent_id: str | None = None
    ) -> dict[str, Any]:
        """Store a new agent at revision 1."""
        new_id = agent_id or new_agent_id()
        data = json.dumps(spec.model_dump(mode="json"), ensure_ascii=False)
        now = time.time()
        with self._lock, closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                count = conn.execute(
                    "SELECT COUNT(*) FROM nexura_service_agents WHERE workspace_id=?",
                    (workspace_id,),
                ).fetchone()[0]
                if count >= MAX_AGENTS_PER_WORKSPACE:
                    raise AgentLimitError(
                        f"A workspace may hold at most {MAX_AGENTS_PER_WORKSPACE} agents."
                    )
                self._check_name(conn, workspace_id, spec.name, None)
                conn.execute(
                    f"INSERT INTO nexura_service_agents ({self._COLUMNS}) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (workspace_id, new_id, spec.name, data, 1, now, now, actor, actor),
                )
                row = self._row(conn, workspace_id, new_id)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return self._record(row)

    @staticmethod
    def _check_name(
        conn: sqlite3.Connection, workspace_id: str, name: str, agent_id: str | None
    ) -> None:
        clash = conn.execute(
            "SELECT agent_id FROM nexura_service_agents WHERE workspace_id=? AND name=?",
            (workspace_id, name),
        ).fetchone()
        if clash is not None and clash[0] != agent_id:
            raise AgentNameTakenError(f"An agent named '{name}' already exists.")

    @staticmethod
    def _check_revision(agent_id: str, stored: int, expected: int | None) -> None:
        if expected is not None and expected != stored:
            raise AgentRevisionConflictError(
                f"Agent '{agent_id}' is at revision {stored}, not {expected}; reload before "
                "saving.",
                stored,
            )

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
        data = json.dumps(spec.model_dump(mode="json"), ensure_ascii=False)
        with self._lock, closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._row(conn, workspace_id, agent_id)
                if row is None:
                    raise AgentNotFoundError(f"Agent '{agent_id}' not found.")
                stored = int(row[4])
                self._check_revision(agent_id, stored, expected_revision)
                if json.loads(row[3]) != json.loads(data):
                    self._check_name(conn, workspace_id, spec.name, agent_id)
                    conn.execute(
                        "UPDATE nexura_service_agents SET name=?, spec=?, revision=?, "
                        "updated_at=?, updated_by=? WHERE workspace_id=? AND agent_id=?",
                        (spec.name, data, stored + 1, time.time(), actor, workspace_id, agent_id),
                    )
                    row = self._row(conn, workspace_id, agent_id)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return self._record(row)

    def delete(
        self, workspace_id: str, agent_id: str, *, expected_revision: int | None = None
    ) -> None:
        """Remove the agent; workflows naming it fail at run time from then on."""
        with self._lock, closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._row(conn, workspace_id, agent_id)
                if row is None:
                    raise AgentNotFoundError(f"Agent '{agent_id}' not found.")
                self._check_revision(agent_id, int(row[4]), expected_revision)
                conn.execute(
                    "DELETE FROM nexura_service_agents WHERE workspace_id=? AND agent_id=?",
                    (workspace_id, agent_id),
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise


__all__ = [
    "MAX_AGENTS_PER_WORKSPACE",
    "AgentLimitError",
    "AgentNameTakenError",
    "AgentNotFoundError",
    "AgentRevisionConflictError",
    "AgentStore",
    "AgentStoreError",
    "agent_summary",
    "new_agent_id",
]
