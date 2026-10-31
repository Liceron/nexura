# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Row deletion helpers for the stores' ``purge_workspace`` methods (workspace deletion, P46).

Each store names its own tables, in child-before-parent order; every table must have a
``workspace_id`` column. The helpers only delete: repeating them is harmless, so an
interrupted purge is simply run again.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import Any


def _check(tables: Sequence[str]) -> None:
    for table in tables:
        if not table.replace("_", "").isalnum():
            raise ValueError(f"Invalid table name {table!r}")


def sqlite_purge(conn: sqlite3.Connection, tables: Sequence[str], workspace_id: str) -> int:
    """Delete the rows of ``workspace_id`` from ``tables`` in one transaction on ``conn``;
    returns how many rows the last table lost (the parent rows)."""
    _check(tables)
    removed = 0
    with conn:
        for table in tables:
            removed = conn.execute(
                f"DELETE FROM {table} WHERE workspace_id = ?", (workspace_id,)
            ).rowcount
    return max(removed, 0)


async def postgres_purge_tx(conn: Any, tables: Sequence[str], workspace_id: str) -> int:
    """``sqlite_purge`` inside a caller's Postgres (asyncpg) transaction."""
    _check(tables)
    removed = 0
    for table in tables:
        status = await conn.execute(f"DELETE FROM {table} WHERE workspace_id = $1", workspace_id)
        removed = int(str(status).split()[-1]) if str(status).split()[-1].isdigit() else 0
    return removed


def postgres_purge(database: Any, tables: Sequence[str], workspace_id: str) -> int:
    """``sqlite_purge`` on a shared ``nexura_postgres.Database`` (one transaction)."""
    if "\x00" in workspace_id:
        return 0

    async def run(conn: Any) -> int:
        return await postgres_purge_tx(conn, tables, workspace_id)

    removed: int = database.run(run)
    return removed


__all__ = ["postgres_purge", "postgres_purge_tx", "sqlite_purge"]
