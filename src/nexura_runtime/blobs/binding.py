# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""The binding between a database and the blob store its references point into.

A database that holds blob references records, once, the identity of the store those references
use: the one-row table ``nexura_blob_store_binding``. It is written the first time a payload of
that database is stored as a blob (or at startup in Postgres or ``gs://`` mode) and never
changes afterwards. ``db blob-gc`` checks this row, so it still recognises the right database
after run retention has deleted every row that held a reference, and it refuses a database bound
to another store.

On SQLite the table is created on first use; on Postgres it is created by the numbered migration
component ``blob_binding`` (``nexura_postgres.blobs``). Both have the same columns.

**Two-way binding (MVP WP3, review R3-1).** A binding that has a ``database_id()`` (the Postgres
service database: a random ``db_<32 hex>`` kept in ``nexura_database_identity``) also makes the
store record that id as its *owner* (``_nexura/store-owner``, or ``<prefix>.nexura-store-owner``
on GCS, create-only). Another database is then refused before it is bound (``BlobOwnerError``),
so a staging or dev service started with prod's ``NEXURA_BLOB_URL`` stops at startup, and
``db blob-gc`` refuses unless the owning database is among the scanned ones. A database restored
from a dump keeps its id. SQLite bindings stay one-way: each Cloud Run instance would start with
its own file, and a plain SQLite service is a single host.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Protocol

from nexura_runtime.blobs.base import BlobStoreError, validate_store_id

BINDING_TABLE = "nexura_blob_store_binding"
SQLITE_BINDING_DDL = f"""CREATE TABLE IF NOT EXISTS {BINDING_TABLE} (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    store_id TEXT NOT NULL,
    store TEXT NOT NULL,
    created_at REAL NOT NULL)"""


class BlobBindingError(BlobStoreError):
    """The database is bound to a different blob store."""


class BlobOwnerError(BlobBindingError):
    """The blob store belongs to a different database (two-way binding, R3-1)."""


class BlobClaimRefused(BlobOwnerError):
    """A database may not claim an unowned store that already holds blobs it does not use."""


#: The operator command for the deliberate first claim of a store that already holds blobs.
CLAIM_COMMAND = "nexura-runtime db blob-claim --i-own-this-store"


def claim_refused(store: str, database_id: str) -> BlobClaimRefused:
    return BlobClaimRefused(
        f"Blob store {store} already holds blobs and has no owner, and this database "
        f"({database_id}) neither is bound to it nor references it, so the blobs may belong to "
        "another deployment (a staging or dev service started with prod's NEXURA_BLOB_URL). "
        "Use a new bucket prefix for this deployment. If this database really owns these "
        "blobs (for example a store written before owners existed), claim it once with "
        f"`{CLAIM_COMMAND}`"
    )


def owner_without_database(owner: str, store: str) -> BlobOwnerError:
    return BlobOwnerError(
        f"Blob store {store} belongs to database {owner}, and this service's database cannot "
        "own a store (SQLite): it holds another deployment's payloads. Use one bucket prefix per "
        "deployment"
    )


def owner_mismatch(owner: str, database_id: str, store: str) -> BlobOwnerError:
    return BlobOwnerError(
        f"Blob store {store} belongs to database {owner}, not to this database "
        f"({database_id}): it holds another deployment's payloads. Use one bucket prefix per "
        "deployment (a new NEXURA_BLOB_URL prefix for this database). A database restored with "
        "pg_dump/pg_restore keeps its id and is accepted"
    )


def mismatch(bound: str, store_id: str, where: str) -> BlobBindingError:
    return BlobBindingError(
        f"{where} is bound to blob store {bound}, not to {store_id}: its references point into "
        "another store. Use that store's NEXURA_BLOB_URL"
    )


class BlobBinding(Protocol):
    """Where a database records the store id; ``ident`` tells two bindings apart."""

    ident: tuple[str, str]

    def ensure(self, store_id: str, store: str) -> None:
        """Record ``store_id`` if the database is unbound; raise ``BlobBindingError`` if it is
        bound to another store."""
        ...

    def read(self) -> str | None: ...


def read_sqlite_binding(conn: sqlite3.Connection) -> str | None:
    """The bound store id on an open connection (``None``: no table, or no row)."""
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (BINDING_TABLE,)
    ).fetchone():
        return None
    row = conn.execute(f"SELECT store_id FROM {BINDING_TABLE} WHERE singleton = 1").fetchone()
    return validate_store_id(row[0]) if row else None


class SQLiteBlobBinding:
    """The binding row in a service SQLite file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self.ident = ("sqlite", str(self.path))

    def ensure(self, store_id: str, store: str) -> None:
        validate_store_id(store_id)
        with closing(sqlite3.connect(self.path, timeout=30, isolation_level=None)) as conn:
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.execute(SQLITE_BINDING_DDL)
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    f"INSERT OR IGNORE INTO {BINDING_TABLE} (singleton, store_id, store, "
                    "created_at) VALUES (1, ?, ?, ?)",
                    (store_id, store, time.time()),
                )
                bound = read_sqlite_binding(conn)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        if bound != store_id:
            raise mismatch(str(bound), store_id, f"Database {self.path}")

    def read(self) -> str | None:
        if not self.path.is_file():
            return None
        uri = self.path.as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=30)) as conn:
            return read_sqlite_binding(conn)


def binding_ident(binding: Any) -> tuple[str, str]:
    ident = getattr(binding, "ident", None)
    return ident if isinstance(ident, tuple) else ("object", str(id(binding)))
