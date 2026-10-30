# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Workspaces and resource ownership: the open-source half of workspace tenancy.

Workspace memberships, member removal tracking and the membership audit trail moved to the
closed edition in EDITIONS_PLAN step 7 (a subclass of ``WorkspaceRepository`` there keeps the
same tables). The open-source edition has
one principal, the local owner, so its membership answers are fixed: the local owner is the
``admin`` member of every workspace and nobody else is a member.

Storage: in memory, made durable with ``bind_db_path`` (SQLite, loaded into memory at bind),
or bound to a shared Postgres database with ``bind_database`` (MVP Postgres WP2). A
Postgres-bound repository keeps nothing in memory: every call reads or writes the database,
so several service instances never disagree about workspaces, ownership or membership.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections.abc import Iterable
from contextlib import closing
from pathlib import Path
from typing import Any

from nexura_runtime.service.workspaces.models import (
    Workspace,
    WorkspaceMember,
)

#: Pseudo workspace returned for unowned resources when the repository fails closed. It contains
#: a NUL byte, which can never be a valid (validated) workspace id or HTTP header value.
UNOWNED_WORKSPACE = "\x00unowned"

#: Resource types that fail closed (are owned by nobody) once the repository is in shared mode.
FAIL_CLOSED_RESOURCE_TYPES = frozenset({"workflow", "run", "session", "document"})


class WorkspaceExistsError(ValueError):
    """Raised when creating a workspace whose id is already registered."""


#: TEST ONLY. Set through ``nexura_runtime.testing.allow_sqlite_repository_import`` (never an
#: environment variable): lets tests that build a file-backed repository run on Postgres by
#: copying the file's records once. Ignored on Cloud Run.
_TEST_SQLITE_IMPORT = False


def refuse_sqlite_loaded_repository(repo: Any) -> None:
    """Refuse a repository loaded from a SQLite file before it can reach Postgres.

    Copying the file into the shared database on every start would undo removals made since
    the file was written (a removed member would regain access). Raised before any connection.
    """
    path = getattr(repo, "db_path", None)
    if path is None:
        return
    from nexura_runtime.service.cloud import on_cloud_run

    if _TEST_SQLITE_IMPORT and on_cloud_run() is None:
        return
    raise ValueError(
        f"database_url is set but workspace_repo was loaded from the SQLite file {path}. "
        "Postgres mode never copies a SQLite file implicitly: a stale copy would bring back "
        "removed members and deleted ownership on every start. Pass an unbound repository (or "
        "none). To move existing SQLite data, import it once, explicitly, before switching: "
        "the SQLite to Postgres import (`nexura-runtime db import-sqlite`, MVP WP12, planned)"
    )


def ensure_private_file(path: str | Path) -> None:
    """Create ``path`` with mode 0600 (POSIX) if it does not exist yet. Never touches existing."""
    target = Path(path)
    if str(target) == ":memory:" or target.exists():
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(target), os.O_WRONLY | os.O_CREAT, 0o600)
    except OSError:
        return
    os.close(fd)


class WorkspaceRepository:
    """Thread-safe workspace and resource-ownership store with SQLite durability.

    Membership is fixed in the open-source edition (see the module docstring): ``get_member``,
    ``is_removed_member`` and ``list_workspaces_for_principal`` satisfy the read-only
    ``MembershipStore`` protocol for the single local owner.
    """

    #: The single principal of the open-source edition.
    LOCAL_OWNER = "local-owner"

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path: Path | None = Path(db_path).resolve() if db_path else None
        self._lock = threading.RLock()
        self._workspaces: dict[str, Workspace] = {}
        # (resource_type, resource_id) -> (workspace_id, owner_id)
        self._ownership: dict[tuple[str, str], tuple[str, str]] = {}
        # When True (shared mode), unowned resources of FAIL_CLOSED_RESOURCE_TYPES belong to nobody.
        self.fail_closed_unowned: bool = False
        # Read-through Postgres storage once ``bind_database`` ran; ``None`` means memory/SQLite.
        self._pg: Any = None

        now = time.time()
        self._workspaces["default"] = Workspace(
            workspace_id="default",
            name="Default Workspace",
            owner_id="local-owner",
            created_at=now,
            metadata={"description": "System default workspace"},
        )

        if self.db_path is not None:
            self._init_sqlite()

    def _init_sqlite(self) -> None:
        """Create tables if not existing and load persisted records into memory."""
        assert self.db_path is not None
        ensure_private_file(self.db_path)
        with (
            self._lock,
            closing(
                sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=10.0)
            ) as conn,
            conn,
        ):
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS nexura_service_workspaces (
                    workspace_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    metadata_json TEXT NOT NULL
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS nexura_service_resource_ownership (
                    resource_type TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (resource_type, resource_id)
                );
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_nexura_ownership_ws
                ON nexura_service_resource_ownership(workspace_id, resource_type);
                """
            )

            # Load existing workspaces
            cur = conn.execute(
                "SELECT workspace_id, name, owner_id, created_at, metadata_json "
                "FROM nexura_service_workspaces"
            )
            for ws_id, name, owner, created_at, meta_json in cur.fetchall():
                self._workspaces[ws_id] = Workspace(
                    workspace_id=ws_id,
                    name=name,
                    owner_id=owner,
                    created_at=created_at,
                    metadata=json.loads(meta_json) if meta_json else {},
                )

            if "default" not in self._workspaces:
                now = time.time()
                default_ws = Workspace(
                    workspace_id="default",
                    name="Default Workspace",
                    owner_id="local-owner",
                    created_at=now,
                    metadata={"description": "System default workspace"},
                )
                self._workspaces["default"] = default_ws
                conn.execute(
                    """
                    INSERT OR REPLACE INTO nexura_service_workspaces
                    (workspace_id, name, owner_id, created_at, metadata_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        "default",
                        "Default Workspace",
                        "local-owner",
                        now,
                        json.dumps(default_ws.metadata),
                    ),
                )

            # Load ownership records
            cur = conn.execute(
                "SELECT resource_type, resource_id, workspace_id, owner_id "
                "FROM nexura_service_resource_ownership"
            )
            for r_type, r_id, ws_id, o_id in cur.fetchall():
                self._ownership[(r_type, r_id)] = (ws_id, o_id)

    @property
    def database_bound(self) -> bool:
        """True once ``bind_database`` bound this repository to Postgres."""
        return self._pg is not None

    def _postgres_store(self, database: Any) -> Any:
        """The Postgres storage class for this repository (the closed edition overrides it)."""
        from nexura_runtime.modules.optional_packages import POSTGRES_PACKAGE, require_package

        postgres = require_package(POSTGRES_PACKAGE, "Postgres workspaces")
        return postgres.PostgresWorkspaceStore(database)

    def _import_into(self, store: Any) -> None:
        """Copy in-memory records the database lacks (stored rows win, as ``bind_db_path``)."""
        store.import_state(list(self._workspaces.values()), dict(self._ownership))

    def bind_database(self, database: Any) -> None:
        """Bind to a shared Postgres ``Database`` (``nexura_postgres.Database``) read-through.

        Records created in memory before binding (``default`` and anything added since) are
        added unless the database already has them, mirroring ``bind_db_path``. A repository
        loaded from a SQLite file is refused (``refuse_sqlite_loaded_repository``): copying a
        possibly stale file on every start would bring back removed members and deleted
        ownership. Afterwards nothing is cached in this process.
        """
        refuse_sqlite_loaded_repository(self)
        with self._lock:
            if self._pg is not None:
                raise RuntimeError("WorkspaceRepository is already bound to Postgres")
            store = self._postgres_store(database)
            self._import_into(store)
            self._pg = store

    def bind_db_path(self, db_path: str | Path) -> None:
        """Bind repository to SQLite storage and sync current memory state."""
        if self._pg is not None:
            raise RuntimeError("WorkspaceRepository is already bound to Postgres")
        with self._lock:
            self.db_path = Path(db_path).resolve()
            self._init_sqlite()
            # Flush any unwritten workspaces/ownership that were in memory
            with (
                closing(
                    sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=10.0)
                ) as conn,
                conn,
            ):
                for ws in self._workspaces.values():
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO nexura_service_workspaces
                        (workspace_id, name, owner_id, created_at, metadata_json)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            ws.workspace_id,
                            ws.name,
                            ws.owner_id,
                            ws.created_at,
                            json.dumps(ws.metadata),
                        ),
                    )
                for (r_type, r_id), (ws_id, o_id) in self._ownership.items():
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO nexura_service_resource_ownership
                        (resource_type, resource_id, workspace_id, owner_id, created_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (r_type, r_id, ws_id, o_id, time.time()),
                    )
                conn.commit()

    def record_resource_ownership(
        self,
        resource_type: str,
        resource_id: str,
        workspace_id: str,
        owner_id: str,
    ) -> None:
        """Record durable tenant ownership mapping for a service resource."""
        if self._pg is not None:
            self._pg.record_resource_ownership(resource_type, resource_id, workspace_id, owner_id)
            return
        with self._lock:
            self._ownership[(resource_type, resource_id)] = (workspace_id, owner_id)
            if self.db_path is not None:
                with (
                    closing(
                        sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=10.0)
                    ) as conn,
                    conn,
                ):
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO nexura_service_resource_ownership
                        (resource_type, resource_id, workspace_id, owner_id, created_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (resource_type, resource_id, workspace_id, owner_id, time.time()),
                    )
                    conn.commit()

    def get_resource_ownership(
        self,
        resource_type: str,
        resource_id: str,
    ) -> tuple[str, str] | None:
        """Retrieve (workspace_id, owner_id) for a resource, or None."""
        if self._pg is not None:
            result: tuple[str, str] | None = self._pg.get_resource_ownership(
                resource_type, resource_id
            )
            return result
        with self._lock:
            return self._ownership.get((resource_type, resource_id))

    def get_resource_workspace(
        self,
        resource_type: str,
        resource_id: str,
    ) -> str | None:
        """Retrieve workspace_id for a resource, or None."""
        if self._pg is not None:
            res = self._pg.get_resource_ownership(resource_type, resource_id)
            if (
                res is None
                and self.fail_closed_unowned
                and resource_type in FAIL_CLOSED_RESOURCE_TYPES
            ):
                return UNOWNED_WORKSPACE
            return str(res[0]) if res else None
        with self._lock:
            res = self._ownership.get((resource_type, resource_id))
            if (
                res is None
                and self.fail_closed_unowned
                and resource_type in (FAIL_CLOSED_RESOURCE_TYPES)
            ):
                return UNOWNED_WORKSPACE
            return res[0] if res else None

    def get_resource_workspaces(
        self, keys: Iterable[tuple[str, str]]
    ) -> dict[tuple[str, str], str | None]:
        """``get_resource_workspace`` for many ``(resource_type, resource_id)`` keys.

        On Postgres the keys are read in one query (MVP WP11a) instead of one per key. Every
        key is in the result, with the same value ``get_resource_workspace`` would return
        (``None`` when unowned, or the fail-closed marker). Ids holding NUL (which TEXT cannot
        store) are left out, so a caller looks them up one by one as before.
        """
        wanted = [k for k in dict.fromkeys(keys) if "\x00" not in k[0] + k[1]]
        batch = getattr(self._pg, "get_resource_ownerships", None)
        overridden = (
            type(self).get_resource_workspace is not WorkspaceRepository.get_resource_workspace
        )
        if self._pg is None or batch is None or overridden:
            return {k: self.get_resource_workspace(*k) for k in wanted}
        found = batch(wanted)
        result: dict[tuple[str, str], str | None] = {}
        for key in wanted:
            res = found.get(key)
            if res is None:
                closed = self.fail_closed_unowned and key[0] in FAIL_CLOSED_RESOURCE_TYPES
                result[key] = UNOWNED_WORKSPACE if closed else None
            else:
                result[key] = str(res[0]) if res else None
        return result

    def list_workspace_resource_ids(
        self,
        resource_type: str,
        workspace_id: str,
    ) -> list[str]:
        """List all resource IDs of a given type owned by workspace."""
        if self._pg is not None:
            ids: list[str] = self._pg.list_workspace_resource_ids(resource_type, workspace_id)
            return ids
        with self._lock:
            return [
                r_id
                for (r_type, r_id), (ws_id, _) in self._ownership.items()
                if r_type == resource_type and ws_id == workspace_id
            ]

    def list_owned_resource_ids(
        self,
        resource_type: str,
        workspace_id: str,
        owner_id: str,
    ) -> list[str]:
        """List resource IDs of a type in ``workspace_id`` recorded as owned by ``owner_id``."""
        if self._pg is not None:
            owned: list[str] = self._pg.list_owned_resource_ids(
                resource_type, workspace_id, owner_id
            )
            return owned
        with self._lock:
            return [
                r_id
                for (r_type, r_id), (ws_id, o_id) in self._ownership.items()
                if r_type == resource_type and ws_id == workspace_id and o_id == owner_id
            ]

    def delete_resource_ownership(
        self,
        resource_type: str,
        resource_id: str,
    ) -> bool:
        """Remove tenant ownership mapping for a service resource."""
        if self._pg is not None:
            return bool(self._pg.delete_resource_ownership(resource_type, resource_id))
        with self._lock:
            removed = self._ownership.pop((resource_type, resource_id), None)
            if self.db_path is not None:
                with (
                    closing(
                        sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=10.0)
                    ) as conn,
                    conn,
                ):
                    conn.execute(
                        """
                        DELETE FROM nexura_service_resource_ownership
                        WHERE resource_type = ? AND resource_id = ?
                        """,
                        (resource_type, resource_id),
                    )
                    conn.commit()
            return removed is not None

    def create_workspace(
        self,
        workspace_id: str,
        name: str,
        owner_id: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> Workspace:
        """Create a new workspace owned by ``owner_id``."""
        if not workspace_id or any(ord(ch) < 32 or ord(ch) == 127 for ch in workspace_id):
            raise ValueError("Invalid workspace id.")
        if self._pg is not None:
            created: Workspace = self._pg.create_workspace(
                Workspace(
                    workspace_id=workspace_id,
                    name=name,
                    owner_id=owner_id,
                    metadata=metadata or {},
                )
            )
            return created
        with self._lock:
            if workspace_id in self._workspaces:
                raise WorkspaceExistsError(workspace_id)
            ws = Workspace(
                workspace_id=workspace_id,
                name=name,
                owner_id=owner_id,
                metadata=metadata or {},
            )
            self._workspaces[workspace_id] = ws

            if self.db_path is not None:
                with (
                    closing(
                        sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=10.0)
                    ) as conn,
                    conn,
                ):
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO nexura_service_workspaces
                        (workspace_id, name, owner_id, created_at, metadata_json)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            ws.workspace_id,
                            ws.name,
                            ws.owner_id,
                            ws.created_at,
                            json.dumps(ws.metadata),
                        ),
                    )
                    conn.commit()
            return ws

    def update_workspace_metadata(
        self,
        workspace_id: str,
        updates: dict[str, Any],
    ) -> Workspace:
        """Merge workspace policy metadata and persist it under the repository lock."""
        if self._pg is not None:
            updated: Workspace = self._pg.update_workspace_metadata(workspace_id, updates)
            return updated
        with self._lock:
            ws = self._workspaces.get(workspace_id)
            if ws is None:
                raise KeyError(workspace_id)
            metadata = {**ws.metadata, **updates}
            if self.db_path is not None:
                with closing(sqlite3.connect(str(self.db_path), timeout=10)) as conn, conn:
                    conn.execute(
                        "UPDATE nexura_service_workspaces SET metadata_json=? WHERE workspace_id=?",
                        (json.dumps(metadata), workspace_id),
                    )
            ws.metadata.update(updates)
            return ws

    def get_workspace(self, workspace_id: str) -> Workspace | None:
        """Retrieve workspace by ID."""
        if self._pg is not None:
            found: Workspace | None = self._pg.get_workspace(workspace_id)
            return found
        with self._lock:
            return self._workspaces.get(workspace_id)

    def is_removed_member(self, workspace_id: str, principal_id: str) -> bool:
        """Always False: the open-source edition never removes the local owner."""
        return False

    def list_workspaces(self) -> list[Workspace]:
        """List all registered workspaces."""
        if self._pg is not None:
            listed: list[Workspace] = self._pg.list_workspaces()
            return listed
        with self._lock:
            return list(self._workspaces.values())

    def list_workspaces_for_principal(self, principal_id: str) -> list[Workspace]:
        """Every workspace for the local owner; nothing for anyone else."""
        if principal_id != self.LOCAL_OWNER:
            return []
        return self.list_workspaces()

    def get_member(self, workspace_id: str, principal_id: str) -> WorkspaceMember | None:
        """The local owner's ``admin`` membership of an existing workspace, else ``None``."""
        if principal_id != self.LOCAL_OWNER:
            return None
        ws = self.get_workspace(workspace_id)
        if ws is None:
            return None
        return WorkspaceMember(
            workspace_id=workspace_id,
            principal_id=self.LOCAL_OWNER,
            role="admin",
            joined_at=ws.created_at,
            updated_at=ws.created_at,
        )
