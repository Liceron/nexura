# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Shared database bridge and transaction-pool-safe asyncpg configuration.

Callbacks receive a connection inside a transaction. They must not retain it or
change session state. URLs are never included in logs (they contain credentials).

``autocommit=True`` (MVP WP11a) runs a callback without ``BEGIN``/``COMMIT``: each statement
commits on its own. It saves two network round trips per call, which on a remote database
(Cloud Run to Neon, about 25 ms each) is most of a one-query call. Use it only for a callback
that sends **one** statement, or reads that need neither one snapshot nor any lock
(transaction-level advisory locks and ``FOR UPDATE`` end with each statement). It stays safe
behind a transaction-mode pooler: asyncpg re-sends an unnamed statement's ``Parse`` with its
``Bind``/``Execute``, so each statement is complete on whatever server connection runs it.

A ``Database`` is not fork-safe: its loop thread does not exist in a forked child, so
using an instance created before ``fork`` raises ``RuntimeError``. Create one per process.
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Awaitable, Callable, Coroutine
from concurrent.futures import Future
from typing import Any, TypeVar

from nexura_runtime.service.storage_url import normalize_database_url as normalize_url

T = TypeVar("T")
#: Seconds to open one connection (asyncpg's default is 60).
CONNECT_TIMEOUT = 10.0
#: Seconds a readiness ping may take, so a slow or unreachable database fails fast.
PING_TIMEOUT = 10.0
#: Transaction isolation levels ``Database.run`` accepts (``None``: the server default, READ
#: COMMITTED). They are set per transaction (``BEGIN ISOLATION LEVEL ...``), never per session.
ISOLATION_LEVELS = frozenset({None, "read_committed", "repeatable_read", "serializable"})

__all__ = ["Database", "LoopThread", "noop_reset", "normalize_url", "pool_options"]


async def noop_reset(connection: Any) -> None:
    """No session state is used; asyncpg already rolls back unfinished transactions."""


def pool_options() -> dict[str, Any]:
    maximum = int(os.environ.get("NEXURA_DATABASE_POOL_MAX_SIZE", "5"))
    if maximum < 1:
        raise ValueError("NEXURA_DATABASE_POOL_MAX_SIZE must be positive")
    return dict(
        min_size=0,
        max_size=maximum,
        statement_cache_size=0,
        reset=noop_reset,
        max_inactive_connection_lifetime=60,
        command_timeout=30,
        timeout=CONNECT_TIMEOUT,
    )


class LoopThread:
    """One private loop; both synchronous and asynchronous callers submit work."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        ready = threading.Event()

        def main() -> None:
            asyncio.set_event_loop(self.loop)
            self.loop.call_soon(ready.set)
            self.loop.run_forever()
            self.loop.close()

        self.thread = threading.Thread(target=main, name="nexura-database", daemon=True)
        self.thread.start()
        ready.wait()

    def submit(self, coroutine: Coroutine[Any, Any, T]) -> Future[T]:
        if threading.current_thread() is self.thread:
            coroutine.close()
            raise RuntimeError("Database bridge cannot be called from its own loop")
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop)

    def close(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=10)


class Database:
    """Process-local shared pool, keyed by normalized URL, opened lazily.

    ``close`` releases one factory owner's reference; the last owner closes the pool.
    Acquire once per app, then share that object with all of its stores.
    """

    _instances: dict[tuple[int, str], Database] = {}
    _lock = threading.Lock()

    def __init__(self, url: str) -> None:
        self._url = url
        pool_options()  # validate the environment override early
        self._pid = os.getpid()
        self._bridge = LoopThread()
        self._pool: Any = None
        self._pool_lock: asyncio.Lock | None = None
        self._owners = 1
        self._closed = False

    @classmethod
    def from_url(cls, url: str) -> Database:
        url = normalize_url(url)
        with cls._lock:
            key = (os.getpid(), url)
            if key in cls._instances:
                db = cls._instances[key]
                db._owners += 1
                return db
            db = cls(url)
            cls._instances[key] = db
            return db

    async def _call(
        self,
        fn: Callable[[Any], Awaitable[T]],
        isolation: str | None,
        readonly: bool,
        autocommit: bool = False,
    ) -> T:
        if self._pool_lock is None:
            self._pool_lock = asyncio.Lock()
        async with self._pool_lock:
            if self._pool is None:
                import asyncpg  # type: ignore[import-untyped]

                self._pool = await asyncpg.create_pool(self._url, **pool_options())
        async with self._pool.acquire() as conn:
            if autocommit:
                return await fn(conn)
            # Options only when asked for: the default stays the plain ``conn.transaction()``.
            options: dict[str, Any] = {}
            if isolation is not None:
                options["isolation"] = isolation
            if readonly:
                options["readonly"] = True
            async with conn.transaction(**options):
                return await fn(conn)

    def _submit(
        self,
        fn: Callable[[Any], Awaitable[T]],
        isolation: str | None = None,
        readonly: bool = False,
        autocommit: bool = False,
    ) -> Future[T]:
        if isolation not in ISOLATION_LEVELS:
            raise ValueError(f"isolation must be one of {sorted(map(str, ISOLATION_LEVELS))}")
        if autocommit and (isolation is not None or readonly):
            raise ValueError("autocommit runs no transaction: isolation and readonly do not apply")
        if self._closed:
            raise RuntimeError("Database is closed")
        if os.getpid() != self._pid:
            raise RuntimeError("Database was created before fork; create one per process")
        return self._bridge.submit(self._call(fn, isolation, readonly, autocommit))

    def run(
        self,
        fn: Callable[[Any], Awaitable[T]],
        *,
        timeout: float = 60.0,
        isolation: str | None = None,
        readonly: bool = False,
        autocommit: bool = False,
    ) -> T:
        """Run ``fn(conn)`` in one transaction (READ COMMITTED unless ``isolation`` is
        ``"repeatable_read"`` or ``"serializable"``; ``readonly`` makes it READ ONLY).

        ``autocommit=True``: no transaction, for a one-statement callback (module docstring).
        """
        future = self._submit(fn, isolation, readonly, autocommit)
        try:
            return future.result(timeout)
        except TimeoutError:
            future.cancel()
            raise

    def submit(
        self,
        fn: Callable[[Any], Awaitable[T]],
        *,
        isolation: str | None = None,
        readonly: bool = False,
    ) -> Future[T]:
        """Start ``fn(conn)`` in one transaction and return its future without waiting.

        For callers that keep a transaction open while they compute in their own thread (the
        usage ledger's global lock): ``fn`` hands data back and waits for the caller's verdict
        through futures of its own. ``fn`` must bound every wait, so a caller that never answers
        cannot hold the connection for ever.
        """
        return self._submit(fn, isolation, readonly)

    async def arun(
        self,
        fn: Callable[[Any], Awaitable[T]],
        *,
        isolation: str | None = None,
        readonly: bool = False,
        autocommit: bool = False,
    ) -> T:
        return await asyncio.wrap_future(self._submit(fn, isolation, readonly, autocommit))

    def ping(self) -> bool:
        """``SELECT 1``, bounded by ``PING_TIMEOUT`` seconds."""

        async def query(conn: Any) -> bool:
            return bool(await conn.fetchval("SELECT 1", timeout=PING_TIMEOUT) == 1)

        return self.run(query, timeout=PING_TIMEOUT)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._owners -= 1
            if self._owners:
                return
            self._closed = True
            self._instances.pop((os.getpid(), self._url), None)
        try:
            if self._pool is not None:
                self._bridge.submit(self._pool.close()).result(10)
        finally:
            self._bridge.close()

    @classmethod
    def _close_all_for_tests(cls, url: str) -> None:
        """Close the shared pool for ``url`` whatever its owner count (test teardown only)."""
        url = normalize_url(url)
        with cls._lock:
            db = cls._instances.get((os.getpid(), url))
            if db is None:
                return
            db._owners = 1
        db.close()

    async def aclose(self) -> None:
        await asyncio.to_thread(self.close)
