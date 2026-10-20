# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Single-host SQLite execution journal, using WAL and BEGIN IMMEDIATE transactions."""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from .models import ExecutionConflictError
from .sql import _Connection, _SQLStore


class _SQLiteConnection:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    async def execute(self, sql: str, params: Mapping[str, Any]) -> int:
        return self.db.execute(sql, params).rowcount

    async def fetch(self, sql: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.execute(sql, params).fetchall()]


class SQLiteExecutionStore(_SQLStore):
    """Durable single-host execution journal; never use a shared file across hosts.

    SQLite supplies lease time using julianday('now'), rather than caller clocks.
    WAL, FULL synchronous writes and BEGIN IMMEDIATE serialize competing writers.
    Methods use short synchronous sqlite3 transactions inside the async API; they
    can block the event loop up to busy_timeout under contention. Use Postgres for
    multi-host coordination. Call init_db() before using a new database.
    """

    _clock_sql = "SELECT (julianday('now') - 2440587.5) * 86400.0 AS now"

    def __init__(self, path: str | Path, *, busy_timeout: float = 10.0) -> None:
        filename = str(path)
        if filename != ":memory:":
            try:
                fd = os.open(filename, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
        self._db = sqlite3.connect(
            filename, timeout=busy_timeout, isolation_level=None, check_same_thread=False
        )
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._lock = threading.RLock()

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[_Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield _SQLiteConnection(self._db)
                self._db.commit()
            except BaseException as exc:
                self._db.rollback()
                if isinstance(exc, sqlite3.IntegrityError):
                    raise ExecutionConflictError("Execution journal constraint conflict") from exc
                raise

    async def close(self) -> None:
        """Close this store's SQLite connection; other store instances are unaffected."""
        with self._lock:
            self._db.close()
