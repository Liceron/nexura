# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Test helpers: reach the service's own state on whichever backend the test run uses.

Tests that drive a ``WorkflowEngine`` next to the service, read its execution journal or look
at Studio run rows used to open the SQLite file directly. Under ``--service-backend=postgres``
(``NEXURA_DATABASE_URL`` is set to a per-test schema, MVP WP3) that state lives in Postgres, so
these helpers open the same store the service uses: the SQLite file at ``db`` without the
variable, the Postgres schema with it. The assertions of the tests do not change.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from nexura_agents.execution_store import SQLiteExecutionStore

from nexura_runtime.workflow import SQLiteWorkflowStore


def service_database_url() -> str | None:
    """The Postgres service database of this test run, if any."""
    return os.environ.get("NEXURA_DATABASE_URL") or None


def service_workflow_store(db: str | Path) -> Any:
    """The workflow checkpoint store a service on ``db`` uses (the caller closes it)."""
    url = service_database_url()
    if url is None:
        return SQLiteWorkflowStore(db)
    from nexura_runtime.service.workflow_state import open_workflow_state_store

    return open_workflow_state_store(url)


class _PostgresJournal:
    """The Postgres execution journal of a fresh ``PostgresWorkflowStore``; ``close`` closes
    the pool it runs on (closing the journal alone is a no-op)."""

    def __init__(self, url: str) -> None:
        from nexura_runtime.service.workflow_state import open_workflow_state_store

        self._owner = open_workflow_state_store(url)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._owner.execution_store, name)

    async def close(self) -> None:
        await self._owner.close()


def service_execution_store(db: str | Path) -> Any:
    """The execution journal a service on ``db`` reads (the caller closes it)."""
    url = service_database_url()
    if url is None:
        return SQLiteExecutionStore(db)
    return _PostgresJournal(url)


def studio_sql(db: str | Path, sql: str, *params: Any) -> list[tuple[Any, ...]]:
    """Run one statement on the service's Studio tables; ``?`` placeholders, rows as tuples."""
    url = service_database_url()
    if url is None:
        import sqlite3

        conn = sqlite3.connect(str(db))
        try:
            with conn:
                return [tuple(row) for row in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()
    from nexura_postgres import Database

    counter = iter(range(1, len(params) + 1))
    statement = re.sub(r"\?", lambda _: f"${next(counter)}", sql)
    database = Database.from_url(url)

    async def run(conn: Any) -> Any:
        return await conn.fetch(statement, *params)

    try:
        return [tuple(row) for row in database.run(run)]
    finally:
        database.close()


def reopen_studio_store(db: str | Path) -> None:
    """Open (and close) the service's run store again, as a second process does; opening it
    sweeps legacy runs whose owner process is gone."""
    url = service_database_url()
    if url is None:
        from nexura_runtime.studio.store import StudioStore

        StudioStore(db)._close_sync()
        return
    from nexura_postgres import Database, PostgresStudioStore

    database = Database.from_url(url)
    try:
        PostgresStudioStore(database, checkpoint_store=object(), db_path=db)._close_connection()
    finally:
        database.close()


def owner_of_process(pid: int) -> str:
    """The ``owner_process`` a run executed by live process ``pid`` records, in the format of
    the service's run store: B46 ``host:pid:marker`` on SQLite, the lease-owner format on
    Postgres (MVP WP3 review M1). Call it while ``pid`` is alive."""
    if service_database_url() is None:
        from nexura_runtime.studio import store as store_module

        marker = store_module._process_start_marker(pid)
        return f"{store_module._host_id()}:{pid}:{marker}"
    from nexura_agents.durable import _host_identity, _start_ticks

    return f"nx1.{_host_identity()}.{pid}.{_start_ticks(pid)}.{'f' * 32}"


def service_usage_ledger(db: str | Path) -> Any:
    """A fresh usage ledger on the service's ledger tables (MVP WP5), as another process opens
    it: ``UsageLedger`` on the SQLite file at ``db``, or ``PostgresUsageLedger`` on the Postgres
    schema (its shared pool is closed when the test's schema is dropped)."""
    url = service_database_url()
    if url is None:
        from nexura_agents.usage.ledger import UsageLedger

        return UsageLedger(str(db))
    from nexura_postgres import Database, PostgresUsageLedger

    return PostgresUsageLedger(Database.from_url(url))


class _PostgresStore:
    """A Postgres store on a pool of its own; ``close`` closes that pool."""

    def __init__(self, store: Any) -> None:
        self._store = store

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def close(self) -> None:
        self._store.database.close()


def service_evaluation_store(db: str | Path) -> Any:
    """A fresh evaluation store on the service's evaluation tables (MVP WP8), as another process
    opens it: ``SqliteEvaluationStore`` on the file at ``db``, or ``PostgresEvaluationStore`` on
    the Postgres schema. The caller closes it."""
    url = service_database_url()
    if url is None:
        from nexura_runtime.evaluation.store import SqliteEvaluationStore

        return SqliteEvaluationStore(str(db))
    from nexura_postgres import Database, PostgresEvaluationStore

    return _PostgresStore(PostgresEvaluationStore(Database.from_url(url)))


def store_sql(store: Any, sql: str, *params: Any) -> list[tuple[Any, ...]]:
    """Run one statement (``?`` placeholders) on the tables of a service store, whichever backend
    it uses: the store's SQLite connection, or its Postgres ``Database`` (MVP WP8)."""
    database = getattr(store, "database", None)
    if database is None:
        conn = store._conn
        with conn:
            return [tuple(row) for row in conn.execute(sql, params).fetchall()]
    counter = iter(range(1, len(params) + 1))
    statement = re.sub(r"\?", lambda _: f"${next(counter)}", sql)

    async def run(conn: Any) -> Any:
        return await conn.fetch(statement, *params)

    return [tuple(row) for row in database.run(run)]
