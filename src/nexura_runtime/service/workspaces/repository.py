# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Workspaces and resource ownership: the open-source half of workspace tenancy.

Workspace memberships, member removal tracking and the membership audit trail moved to the
closed edition in EDITIONS_PLAN step 7 (a subclass of ``WorkspaceRepository`` there keeps the
same tables). The open-source edition has
one principal, the local owner, so its membership answers are fixed: the local owner is the
``admin`` member of every workspace and nobody else is a member.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
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

    def bind_db_path(self, db_path: str | Path) -> None:
        """Bind repository to SQLite storage and sync current memory state."""
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
        with self._lock:
            return self._ownership.get((resource_type, resource_id))

    def get_resource_workspace(
        self,
        resource_type: str,
        resource_id: str,
    ) -> str | None:
        """Retrieve workspace_id for a resource, or None."""
        with self._lock:
            res = self._ownership.get((resource_type, resource_id))
            if (
                res is None
                and self.fail_closed_unowned
                and resource_type in (FAIL_CLOSED_RESOURCE_TYPES)
            ):
                return UNOWNED_WORKSPACE
            return res[0] if res else None

    def list_workspace_resource_ids(
        self,
        resource_type: str,
        workspace_id: str,
    ) -> list[str]:
        """List all resource IDs of a given type owned by workspace."""
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

    def get_workspace(self, workspace_id: str) -> Workspace | None:
        """Retrieve workspace by ID."""
        with self._lock:
            return self._workspaces.get(workspace_id)

    def is_removed_member(self, workspace_id: str, principal_id: str) -> bool:
        """Always False: the open-source edition never removes the local owner."""
        return False

    def list_workspaces(self) -> list[Workspace]:
        """List all registered workspaces."""
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
        with self._lock:
            ws = self._workspaces.get(workspace_id)
        if ws is None:
            return None
        return WorkspaceMember(
            workspace_id=workspace_id,
            principal_id=self.LOCAL_OWNER,
            role="admin",
            joined_at=ws.created_at,
            updated_at=ws.created_at,
        )
