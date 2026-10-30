# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Chat sessions on Postgres (MVP Postgres WP4, store #31).

``PostgresSessionStore`` implements the ``SessionStore`` protocol of
``nexura_agents.session`` (plus the service's optional ``delete``, ``list_sessions`` and
``updated_at``) with the semantics of ``SQLiteSessionStore``:

- ``save`` is compare-and-swap on the revision. A later save is one
  ``UPDATE … WHERE session_id = $1 AND revision = $2 RETURNING``, so of two writers that read the
  same revision exactly one wins and the other gets ``SessionConflictError``, on any instance.
- A first save (``expected_revision=0``) is ``INSERT … ON CONFLICT DO NOTHING RETURNING``; a
  second concurrent first save gets the same ``SessionConflictError`` as a later CAS miss.
- A deleted session leaves a tombstone with its last revision, so a re-created session
  continues the count and a snapshot taken before the delete can never match it. First saves
  and deletes of one session id serialise on a transaction-level advisory lock, so a delete
  racing a re-create can never hand out a revision twice.

Tables keep the SQLite names (``nexura_sessions``, ``nexura_session_tombstones``); ``data`` is
the same versioned JSON text (``TEXT``, never ``JSONB``). ``nexura-agents`` stays stdlib-only:
this class lives here. Pooled-endpoint safety (design §2b-5): every call is one transaction on
the shared ``Database``; locks are transaction-scoped; no session state.
"""

from __future__ import annotations

import copy
import time
from typing import Any

from nexura_agents.errors import SessionConflictError, StorageError
from nexura_agents.session import (
    SessionSnapshot,
    deserialize_session,
    serialize_session,
    session_state,
)
from nexura_agents.types import Message

from nexura_postgres.migrations import MIGRATION_TIMEOUT, Migration, migrate_all, register

COMPONENT = "sessions"
MIGRATIONS: tuple[Migration, ...] = (
    (
        1,
        "chat sessions and tombstones",
        (
            """CREATE TABLE nexura_sessions (
                session_id TEXT PRIMARY KEY,
                revision BIGINT NOT NULL,
                data TEXT NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL)""",
            """CREATE TABLE nexura_session_tombstones (
                session_id TEXT PRIMARY KEY,
                revision BIGINT NOT NULL)""",
        ),
    ),
)
register(COMPONENT, MIGRATIONS)

#: Serialises first saves and deletes of one session id, on every instance.
SESSION_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtextextended('nexura-session/' || $1, 0))"
_BIGINT_MAX = 2**63 - 1


def _storable(value: str) -> bool:
    """Postgres ``TEXT`` cannot hold NUL; such an id can never have been stored."""
    return "\x00" not in value


class PostgresSessionStore:
    """Durable chat sessions in the shared Postgres ``Database`` (compare-and-swap saves).

    ``database``: a ``nexura_postgres.Database`` (the service's shared pool); the store never
    closes it. Opening the store applies its migrations (component ``sessions``). After
    ``close()`` every call raises ``StorageError``, as ``SQLiteSessionStore`` does.

    A session id containing NUL (which Postgres text cannot hold) reads as missing and is
    refused on save with ``StorageError``.
    """

    def __init__(self, database: Any, *, migrate: bool = True) -> None:
        self.database = database
        self._closed = False
        if migrate:
            components = {COMPONENT: MIGRATIONS}

            async def apply(conn: Any) -> None:
                await migrate_all(conn, components)

            database.run(apply, timeout=MIGRATION_TIMEOUT + 30)

    def _check_open(self) -> None:
        if self._closed:
            raise StorageError("PostgresSessionStore is closed.")

    async def _arun(self, fn: Any) -> Any:
        self._check_open()
        return await self.database.arun(fn)

    # ---- SessionStore protocol ------------------------------------------------------------

    async def get(self, session_id: str) -> SessionSnapshot | None:
        """The stored snapshot, or ``None`` when the session does not exist."""
        self._check_open()
        if not _storable(session_id):
            return None

        async def run(conn: Any) -> Any:
            return await conn.fetchval(
                "SELECT data FROM nexura_sessions WHERE session_id = $1", session_id
            )

        data_str = await self._arun(run)
        if data_str is None:
            return None
        _, rev, messages = deserialize_session(data_str)
        return SessionSnapshot(
            session_id=session_id,
            messages=copy.deepcopy(messages),
            revision=rev,
            state=session_state(data_str),
        )

    async def save(
        self,
        session_id: str,
        messages: list[Message],
        expected_revision: int,
        state: dict[str, Any] | None = None,
    ) -> SessionSnapshot:
        """Store ``messages`` if the stored revision is ``expected_revision`` (0: a new session),
        else raise ``SessionConflictError``; returns the snapshot at the new revision."""
        self._check_open()
        if not _storable(session_id):
            raise StorageError("A session id cannot contain NUL characters.")
        expected = int(expected_revision)
        now = time.time()

        if expected == 0:

            async def create(conn: Any) -> tuple[int, str]:
                await conn.execute(SESSION_LOCK_SQL, session_id)
                actual = await conn.fetchval(
                    "SELECT revision FROM nexura_sessions WHERE session_id = $1", session_id
                )
                if actual is not None:
                    raise SessionConflictError(
                        session_id=session_id, expected_revision=0, actual_revision=int(actual)
                    )
                tomb = await conn.fetchval(
                    "DELETE FROM nexura_session_tombstones WHERE session_id = $1 "
                    "RETURNING revision",
                    session_id,
                )
                new_revision = (int(tomb) if tomb is not None else 0) + 1
                data_str = serialize_session(session_id, new_revision, messages, state)
                inserted = await conn.fetchval(
                    "INSERT INTO nexura_sessions (session_id, revision, data, updated_at) "
                    "VALUES ($1, $2, $3, $4) ON CONFLICT (session_id) DO NOTHING "
                    "RETURNING revision",
                    session_id,
                    new_revision,
                    data_str,
                    now,
                )
                if inserted is None:  # backstop: a writer that skipped the lock got there first
                    actual = await conn.fetchval(
                        "SELECT revision FROM nexura_sessions WHERE session_id = $1", session_id
                    )
                    raise SessionConflictError(
                        session_id=session_id,
                        expected_revision=0,
                        actual_revision=int(actual or 0),
                    )
                return new_revision, data_str

            new_revision, _ = await self._arun(create)
        else:
            new_revision = expected + 1
            data_str = serialize_session(session_id, new_revision, messages, state)

            async def update(conn: Any) -> int | None:
                if not 0 < expected < _BIGINT_MAX:
                    updated = None  # no stored revision can match
                else:
                    updated = await conn.fetchval(
                        "UPDATE nexura_sessions SET revision = $3, data = $4, updated_at = $5 "
                        "WHERE session_id = $1 AND revision = $2 RETURNING revision",
                        session_id,
                        expected,
                        new_revision,
                        data_str,
                        now,
                    )
                if updated is not None:
                    return None
                actual = await conn.fetchval(
                    "SELECT revision FROM nexura_sessions WHERE session_id = $1", session_id
                )
                return int(actual or 0)

            actual_revision = await self._arun(update)
            if actual_revision is not None:
                raise SessionConflictError(
                    session_id=session_id,
                    expected_revision=expected_revision,
                    actual_revision=actual_revision,
                )
        return SessionSnapshot(
            session_id=session_id,
            messages=copy.deepcopy(messages),
            revision=new_revision,
            state=copy.deepcopy(state or {}),
        )

    async def close(self) -> None:
        """Close the store (the shared ``Database`` stays open: it belongs to the app)."""
        self._closed = True

    def close_sync(self) -> None:
        self._closed = True

    # ---- optional methods the service and helpers use --------------------------------------

    async def delete(self, session_id: str) -> bool:
        """Delete a session, keeping its last revision as a tombstone. ``True`` if it existed."""
        self._check_open()
        if not _storable(session_id):
            return False

        async def run(conn: Any) -> bool:
            await conn.execute(SESSION_LOCK_SQL, session_id)
            revision = await conn.fetchval(
                "DELETE FROM nexura_sessions WHERE session_id = $1 RETURNING revision", session_id
            )
            if revision is None:
                return False
            await conn.execute(
                "INSERT INTO nexura_session_tombstones (session_id, revision) VALUES ($1, $2) "
                "ON CONFLICT (session_id) DO UPDATE SET revision = EXCLUDED.revision",
                session_id,
                int(revision),
            )
            return True

        return bool(await self._arun(run))

    async def list_sessions(self) -> list[str]:
        """All stored session ids, sorted (Python string order, as the SQLite store)."""

        async def run(conn: Any) -> Any:
            return await conn.fetch("SELECT session_id FROM nexura_sessions")

        return sorted(str(row["session_id"]) for row in await self._arun(run))

    async def updated_at(self, session_ids: list[str]) -> dict[str, float]:
        """Last-save time (Unix seconds) of each stored session among ``session_ids``."""
        self._check_open()
        wanted = [sid for sid in dict.fromkeys(session_ids) if _storable(sid)]
        if not wanted:
            return {}

        async def run(conn: Any) -> Any:
            return await conn.fetch(
                "SELECT session_id, updated_at FROM nexura_sessions "
                "WHERE session_id = ANY($1::text[])",
                wanted,
            )

        return {row["session_id"]: float(row["updated_at"]) for row in await self._arun(run)}

    async def __aenter__(self) -> PostgresSessionStore:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()


__all__ = ["COMPONENT", "MIGRATIONS", "PostgresSessionStore"]
