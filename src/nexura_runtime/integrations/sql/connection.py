# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""SQL connection configuration, pooling, and lifecycle management."""

from __future__ import annotations

import asyncio
import contextlib
import os
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal
from urllib.parse import quote

from pydantic import BaseModel, Field

from nexura_runtime.integrations.sql.errors import (
    ConnectionNotFoundError,
    CrossWorkspaceConnectionError,
    PoolExhaustedError,
)


class SqlConnectionConfig(BaseModel):
    """Operator configuration for a vetted SQL connection endpoint."""

    connection_id: str
    workspace_id: str = "default"
    engine: Literal["sqlite", "postgres", "mysql"] = "sqlite"
    database_path: str = ":memory:"
    read_only: bool = True
    statement_timeout_seconds: float = 5.0
    max_rows: int = 1000
    max_bytes: int = 5_000_000
    pool_size: int = Field(default=5, ge=1, le=50)


class SqlPooledConnection:
    """Wrapper holding a native database connection and its pool state."""

    def __init__(self, raw_connection: sqlite3.Connection, config: SqlConnectionConfig) -> None:
        self.raw_connection = raw_connection
        self.config = config
        self.is_closed = False

    def close(self) -> None:
        """Close the underlying connection."""
        if not self.is_closed:
            with contextlib.suppress(Exception):
                self.raw_connection.close()
            self.is_closed = True


class SqlConnectionPool:
    """Asynchronous, thread-safe connection pool for SQLite and embedded databases."""

    def __init__(self, config: SqlConnectionConfig) -> None:
        self.config = config
        self._pool: asyncio.Queue[SqlPooledConnection] = asyncio.Queue()
        self._total_created = 0
        self._checked_out = 0
        self._lock = asyncio.Lock()
        self._is_closed = False

    @property
    def checked_out_count(self) -> int:
        return self._checked_out

    @property
    def available_count(self) -> int:
        return self._pool.qsize()

    @property
    def total_count(self) -> int:
        return self._total_created

    def _create_raw_connection(self) -> sqlite3.Connection:
        """Create a new native sqlite3 connection with strict security pragmas."""
        db_path = self.config.database_path
        is_uri = db_path.startswith("file:")

        if self.config.read_only:
            if not is_uri and db_path != ":memory:" and os.path.exists(db_path):
                # Open with OS/VFS read-only mode flag
                abs_path = os.path.abspath(db_path)
                uri = f"file:{quote(abs_path)}?mode=ro"
                conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
            elif is_uri:
                conn = sqlite3.connect(db_path, uri=True, check_same_thread=False)
            else:
                conn = sqlite3.connect(db_path, check_same_thread=False)
            # Enforce read-only constraint inside SQLite engine
            conn.execute("PRAGMA query_only = ON;")
        else:
            if is_uri:
                conn = sqlite3.connect(db_path, uri=True, check_same_thread=False)
            else:
                conn = sqlite3.connect(db_path, check_same_thread=False)

        # Standard safety pragmas
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    async def acquire(self, timeout: float = 5.0) -> SqlPooledConnection:
        """Acquire an active connection from the pool, creating one if capacity permits."""
        if self._is_closed:
            raise PoolExhaustedError(self.config.connection_id, self.config.pool_size)

        async with self._lock:
            # 1. Reuse existing idle connection if available
            if not self._pool.empty():
                pooled_conn = self._pool.get_nowait()
                if not pooled_conn.is_closed:
                    self._checked_out += 1
                    return pooled_conn
                self._total_created -= 1

            # 2. Spawn a new connection if under max pool size
            if self._total_created < self.config.pool_size:
                raw_conn = await asyncio.to_thread(self._create_raw_connection)
                pooled_conn = SqlPooledConnection(raw_conn, self.config)
                self._total_created += 1
                self._checked_out += 1
                return pooled_conn

        # 3. Wait for a returned connection if pool is fully utilized
        try:
            pooled_conn = await asyncio.wait_for(self._pool.get(), timeout=timeout)
            async with self._lock:
                self._checked_out += 1
            return pooled_conn
        except TimeoutError as exc:
            raise PoolExhaustedError(self.config.connection_id, self.config.pool_size) from exc

    async def release(self, conn: SqlPooledConnection) -> None:
        """Return a checked-out connection to the pool, cleaning up active transactions."""
        async with self._lock:
            self._checked_out = max(0, self._checked_out - 1)

            if self._is_closed or conn.is_closed:
                conn.close()
                self._total_created = max(0, self._total_created - 1)
                return

            try:
                # Rollback uncommitted work and reset progress handler
                await asyncio.to_thread(conn.raw_connection.rollback)
                conn.raw_connection.set_progress_handler(None, 0)
                await self._pool.put(conn)
            except Exception:
                conn.close()
                self._total_created = max(0, self._total_created - 1)

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[SqlPooledConnection]:
        """Context manager guaranteeing connection return even on query exceptions."""
        conn = await self.acquire()
        try:
            yield conn
        finally:
            await self.release(conn)

    async def close_all(self) -> None:
        """Close all connections and terminate the pool."""
        async with self._lock:
            self._is_closed = True
            while not self._pool.empty():
                conn = self._pool.get_nowait()
                conn.close()
            self._total_created = 0
            self._checked_out = 0


class SqlConnectionManager:
    """Registry and manager for operator-configured SQL connection pools."""

    def __init__(self) -> None:
        self._configs: dict[str, SqlConnectionConfig] = {}
        self._pools: dict[str, SqlConnectionPool] = {}
        self._keepers: dict[str, sqlite3.Connection] = {}
        self._lock = asyncio.Lock()

    def register_connection(
        self,
        config: SqlConnectionConfig,
        *,
        initial_sql: str | None = None,
    ) -> None:
        """Register an operator-vetted SQL connection configuration."""
        self._configs[config.connection_id] = config
        if initial_sql:
            is_uri = config.database_path.startswith("file:")
            setup_conn = sqlite3.connect(config.database_path, uri=is_uri, check_same_thread=False)
            try:
                setup_conn.executescript(initial_sql)
                setup_conn.commit()
                if "mode=memory" in config.database_path or config.database_path == ":memory:":
                    self._keepers[config.connection_id] = setup_conn
                else:
                    setup_conn.close()
            except Exception:
                setup_conn.close()
                raise

    def unregister_connection(self, connection_id: str) -> None:
        """Remove a connection configuration."""
        self._configs.pop(connection_id, None)
        keeper = self._keepers.pop(connection_id, None)
        if keeper:
            with contextlib.suppress(Exception):
                keeper.close()
        pool = self._pools.pop(connection_id, None)
        if pool:
            asyncio.create_task(pool.close_all())

    def get_config(self, connection_id: str, workspace_id: str) -> SqlConnectionConfig:
        """Retrieve connection config with strict workspace isolation enforcement."""
        if connection_id not in self._configs:
            raise ConnectionNotFoundError(connection_id, workspace_id)

        cfg = self._configs[connection_id]
        if cfg.workspace_id not in ("*", workspace_id):
            raise CrossWorkspaceConnectionError(connection_id, cfg.workspace_id, workspace_id)

        return cfg

    async def get_pool(self, connection_id: str, workspace_id: str) -> SqlConnectionPool:
        """Get or initialize the connection pool for the specified connection ID."""
        cfg = self.get_config(connection_id, workspace_id)

        async with self._lock:
            if connection_id not in self._pools:
                self._pools[connection_id] = SqlConnectionPool(cfg)
            return self._pools[connection_id]


# Global singleton instance for application use
default_sql_connection_manager = SqlConnectionManager()
