# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Blob reference scan on Postgres, for ``nexura-runtime db blob-gc``.

Reads every column listed in ``nexura_runtime.blobs.retention.REFERENCE_COLUMNS`` that
exists in the current schema and returns the blob keys (and store ids) whose markers it finds,
plus the keys of unfinished runs, which payload expiry must keep, the database's binding
(``nexura_blob_store_binding``, migration component ``blob_binding``) and its random id
(``nexura_database_identity``), which the blob store records as its owner (two-way binding).

A reference column must be ``TEXT`` (or ``VARCHAR``) holding the codec's text unchanged. ``JSONB``
reorders object keys, so markers would no longer start with the marker prefix: the scan would
find nothing and every referenced blob would look unused. Any other column type is refused.
"""

from __future__ import annotations

import time
from typing import Any

from nexura_postgres.migrations import MIGRATION_TIMEOUT, Migration, migrate_all, register
from nexura_runtime.blobs.base import new_database_id, validate_database_id, validate_store_id
from nexura_runtime.blobs.binding import BINDING_TABLE, mismatch
from nexura_runtime.blobs.payloads import MARKER_PREFIX
from nexura_runtime.blobs.retention import (
    FINISHED_RUN_STATUSES,
    REFERENCE_COLUMNS,
    BlobRetentionRefused,
    ReferenceScan,
    scan_values,
)

_TEXT_TYPES = frozenset({"text", "character varying"})

#: The database's binding to its blob store (``nexura_runtime.blobs.binding``).
BINDING_COMPONENT = "blob_binding"
#: One row: this database's random id (``db_<32 hex>``), recorded by the blob store as its owner.
DATABASE_IDENTITY_TABLE = "nexura_database_identity"
BINDING_MIGRATIONS: tuple[Migration, ...] = (
    (
        1,
        "blob store binding",
        (
            f"""CREATE TABLE {BINDING_TABLE} (
                singleton INT PRIMARY KEY CHECK (singleton = 1),
                store_id TEXT NOT NULL,
                store TEXT NOT NULL,
                created_at DOUBLE PRECISION NOT NULL)""",
        ),
    ),
    (
        2,
        "database identity",
        (
            f"""CREATE TABLE {DATABASE_IDENTITY_TABLE} (
                singleton INT PRIMARY KEY CHECK (singleton = 1),
                database_id TEXT NOT NULL,
                created_at DOUBLE PRECISION NOT NULL)""",
        ),
    ),
)
register(BINDING_COMPONENT, BINDING_MIGRATIONS)


async def read_database_id(conn: Any) -> str | None:
    """This database's id (``None``: no table yet, or none created)."""
    if not await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", DATABASE_IDENTITY_TABLE):
        return None
    value = await conn.fetchval(
        f"SELECT database_id FROM {DATABASE_IDENTITY_TABLE} WHERE singleton = 1"
    )
    return validate_database_id(value) if value is not None else None


async def read_binding(conn: Any) -> str | None:
    """The bound store id (``None``: no table yet, or unbound)."""
    if not await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", BINDING_TABLE):
        return None
    value = await conn.fetchval(f"SELECT store_id FROM {BINDING_TABLE} WHERE singleton = 1")
    return validate_store_id(value) if value is not None else None


class PostgresBlobBinding:
    """The binding row in the shared Postgres database (migration component ``blob_binding``).

    ``database_id()`` makes the binding two-way: the codec has the store record that id as its
    owner before the binding row is written (``nexura_runtime.blobs.binding``).
    """

    ident = ("postgres", "service-database")

    def __init__(self, database: Any) -> None:
        self.database = database
        self._migrated = False
        self._database_id: str | None = None

    def database_id(self) -> str:
        """This database's id, created on first use (concurrent first uses agree on one)."""
        if self._database_id is not None:
            return self._database_id
        self._migrate()

        async def ensure(conn: Any) -> str | None:
            await conn.execute(
                f"INSERT INTO {DATABASE_IDENTITY_TABLE} (singleton, database_id, created_at) "
                "VALUES (1, $1, $2) ON CONFLICT (singleton) DO NOTHING",
                new_database_id(),
                time.time(),
            )
            return await read_database_id(conn)

        value = self.database.run(ensure)
        self._database_id = validate_database_id(value)
        return self._database_id

    def _migrate(self) -> None:
        if self._migrated:
            return

        async def apply(conn: Any) -> None:
            await migrate_all(conn, {BINDING_COMPONENT: BINDING_MIGRATIONS})

        self.database.run(apply, timeout=MIGRATION_TIMEOUT + 30)
        self._migrated = True

    def ensure(self, store_id: str, store: str) -> None:
        validate_store_id(store_id)
        self._migrate()

        async def bind(conn: Any) -> str | None:
            await conn.execute(
                f"INSERT INTO {BINDING_TABLE} (singleton, store_id, store, created_at) "
                "VALUES (1, $1, $2, $3) ON CONFLICT (singleton) DO NOTHING",
                store_id,
                store,
                time.time(),
            )
            return await read_binding(conn)

        bound = self.database.run(bind)
        if bound != store_id:
            raise mismatch(str(bound), store_id, "The Postgres service database")

    def read(self) -> str | None:
        return self.database.run(read_binding)  # type: ignore[no-any-return]

    def references(self, store_id: str) -> bool:
        """True when a reference column of this database holds a marker naming ``store_id``."""
        validate_store_id(store_id)
        needle = f'"store": "{store_id}"'

        async def check(conn: Any) -> bool:
            for table, column in REFERENCE_COLUMNS:
                if not await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", table):
                    continue
                found = await conn.fetchval(
                    f'SELECT EXISTS (SELECT 1 FROM "{table}" WHERE left("{column}", $1) = $2 '
                    f'AND strpos("{column}", $3) > 0)',
                    len(MARKER_PREFIX),
                    MARKER_PREFIX,
                    needle,
                )
                if found:
                    return True
            return False

        return bool(self.database.run(check, timeout=600))

    def reset_identity(self) -> str:
        """Give this database a new id and clear its binding (``db reset-identity``): a copy,
        branch or restore that becomes a separate deployment must not keep the original's."""
        self._migrate()
        new_id = new_database_id()

        async def reset(conn: Any) -> None:
            await conn.execute(f"DELETE FROM {DATABASE_IDENTITY_TABLE}")
            await conn.execute(
                f"INSERT INTO {DATABASE_IDENTITY_TABLE} (singleton, database_id, created_at) "
                "VALUES (1, $1, $2)",
                new_id,
                time.time(),
            )
            await conn.execute(f"DELETE FROM {BINDING_TABLE}")

        self.database.run(reset)
        self._database_id = new_id
        return new_id


async def _newest_activity(conn: Any) -> float | None:
    """The newest run ``updated_at`` or event ``timestamp`` (``None`` without runs)."""
    values: list[float] = []
    for table, column in (
        ("nexura_studio_runs", "updated_at"),
        ("nexura_studio_run_events", "timestamp"),
    ):
        if await _column_type(conn, table, column) is not None:
            value = await conn.fetchval(f'SELECT MAX("{column}") FROM "{table}"')
            if value is not None:
                values.append(float(value))
    return max(values) if values else None


async def _column_type(conn: Any, table: str, column: str) -> str | None:
    """The data type of ``table.column`` as resolved through the search path, or ``None``."""
    value = await conn.fetchval(
        "SELECT format_type(a.atttypid, a.atttypmod) FROM pg_attribute a "
        "WHERE a.attrelid = to_regclass($1) AND a.attname = $2 AND NOT a.attisdropped",
        table,
        column,
    )
    return str(value) if value is not None else None


async def referenced_blob_keys(conn: Any) -> ReferenceScan:
    """Referenced keys, read by one statement (one snapshot) over the tables that exist.

    ``Database.run`` already runs this inside a READ COMMITTED transaction; a single
    ``UNION ALL`` statement sees one snapshot of every table. Any snapshot taken after the
    retention start time is correct (see ``nexura_runtime.blobs.retention``).
    """
    bound = await read_binding(conn)
    bindings = frozenset({bound}) if bound else frozenset()
    database_id = await read_database_id(conn)
    database_ids = frozenset({database_id}) if database_id else frozenset()
    newest = await _newest_activity(conn)
    present: list[tuple[str, str]] = []
    for table, column in REFERENCE_COLUMNS:
        # Names come from the fixed list above, never from input.
        if not await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", table):
            continue
        kind = await _column_type(conn, table, column)
        base = (kind or "").split("(")[0]
        if base not in _TEXT_TYPES:
            raise BlobRetentionRefused(
                f"Refusing blob retention: {table}.{column} is {kind or 'missing'}, but blob "
                "references must be stored as TEXT (JSONB reorders keys and hides them)"
            )
        present.append((table, column))
    if not present:
        return ReferenceScan(
            frozenset(),
            frozenset(),
            bindings=bindings,
            database_ids=database_ids,
            newest_activity=newest,
        )
    query = " UNION ALL ".join(
        f'SELECT "{column}" AS value FROM "{table}" WHERE left("{column}", $1) = $2'
        for table, column in present
    )
    rows = await conn.fetch(query, len(MARKER_PREFIX), MARKER_PREFIX)
    keys, stores = scan_values(row["value"] for row in rows)
    protected: set[str] = set()
    if ("nexura_studio_run_events", "data_json") in present and await conn.fetchval(
        "SELECT to_regclass('nexura_studio_runs') IS NOT NULL"
    ):
        unfinished = await conn.fetch(
            "SELECT e.data_json AS value FROM nexura_studio_run_events e "
            "JOIN nexura_studio_runs r ON r.run_id = e.run_id "
            "WHERE NOT (r.status = ANY($3::text[])) AND left(e.data_json, $1) = $2",
            len(MARKER_PREFIX),
            MARKER_PREFIX,
            list(FINISHED_RUN_STATUSES),
        )
        protected = scan_values(row["value"] for row in unfinished)[0]
    return ReferenceScan(
        frozenset(keys),
        frozenset(table for table, _ in present),
        frozenset(stores),
        frozenset(protected),
        bindings,
        database_ids,
        newest,
    )
