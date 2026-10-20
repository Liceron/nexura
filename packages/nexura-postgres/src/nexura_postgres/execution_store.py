# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Postgres execution journal using asyncpg transactions and FOR UPDATE row locking.

Follows the repository.py transaction/CAS patterns without importing the runtime
or changing its older repository tables. Only the optional Postgres package needs
asyncpg; the shared journal logic and DDL remain in the stdlib-only SDK store.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

from nexura_agents.execution_store.models import ExecutionConflictError
from nexura_agents.execution_store.sql import _Connection, _SQLStore


class _PostgresConnection:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    @staticmethod
    def _bind(sql: str, params: Mapping[str, Any]) -> tuple[str, list[Any]]:
        names: list[str] = []

        def bind(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in names:
                names.append(name)
            return f"${names.index(name) + 1}"

        return re.sub(r":([a-z_][a-z_0-9]*)", bind, sql), [params[name] for name in names]

    async def execute(self, sql: str, params: Mapping[str, Any]) -> int:
        query, values = self._bind(sql, params)
        result: str = await self.connection.execute(query, *values)
        last = result.rsplit(" ", 1)[-1]
        return int(last) if last.isdigit() else 0

    async def fetch(self, sql: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        query, values = self._bind(sql, params)
        return [dict(row) for row in await self.connection.fetch(query, *values)]


class PostgresExecutionStore(_SQLStore):
    """Optional durable multi-host journal, with database-clock leases and fenced writes.

    Uses FOR UPDATE plus a token/owner/revision guarded UPDATE, following
    PostgresRepository's CAS pattern. Expiry uses the server's clock_timestamp()
    after locking; transaction-start now() could be stale after a row-lock wait.
    Call init_db() to apply versioned migrations before first use. Supplied pools
    remain caller-owned. DSNs must name an explicitly configured database.
    """

    _lock_suffix = " FOR UPDATE"
    _clock_sql = "SELECT EXTRACT(EPOCH FROM clock_timestamp()) AS now"

    def __init__(self, dsn: str | None = None, *, pool: Any = None) -> None:
        if dsn is None and pool is None:
            raise ValueError("Provide a Postgres DSN or pool")
        self._dsn = dsn
        self._pool = pool
        self._owns_pool = pool is None
        self._pool_lock = asyncio.Lock()

    async def _get_pool(self) -> Any:
        async with self._pool_lock:
            if self._pool is None:
                import asyncpg  # type: ignore[import-untyped]

                self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=5)
        return self._pool

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[_Connection]:
        pool = await self._get_pool()
        try:
            async with pool.acquire() as connection, connection.transaction():
                yield _PostgresConnection(connection)
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == "23505":
                raise ExecutionConflictError("Execution journal uniqueness conflict") from exc
            raise

    async def _migration_lock(self, db: _Connection) -> None:
        # Constant is journal-specific; lock works even before the schema exists.
        await db.fetch("SELECT pg_advisory_xact_lock(734658101)", {})

    async def close(self) -> None:
        """Close only a pool created by this store; supplied pools remain caller-owned."""
        if self._pool is not None and self._owns_pool:
            await self._pool.close()
            self._pool = None
