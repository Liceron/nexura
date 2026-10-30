# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""The usage ledger on Postgres (MVP Postgres WP5, store #33).

``PostgresUsageLedger`` is a subclass of ``nexura_agents.usage.ledger.UsageLedger``: every budget
rule, reservation, reconciliation and status computation is the base class's own code, unchanged
(``nexura-agents`` stays stdlib-only; this module carries the Postgres part). The SQLite ledger
gets its guarantee from ``BEGIN IMMEDIATE``, a write lock on the whole file, around "reload,
check, write". Here (design §2b-5, lock table "Usage ledger reserve (#33)") every ledger operation
runs in one transaction that first takes ``pg_advisory_xact_lock(734658120)`` (the same global
scope as SQLite's file lock), then brings the in-memory state up to date, then runs the base
class's check in the caller's thread while the transaction (and so the lock) stays open, and
writes its rows before the commit. Concurrent reservations from any number of processes or
instances therefore never exceed a cap.

**Reload.** Every row carries ``change_seq`` from one sequence, taken while the global lock is
held, so sequence order is commit order and each transaction sees a prefix of it. A ledger
therefore reloads only the rows changed since the last one it saw (the SQLite ledger rereads
every row on every operation). A failed or rolled-back operation that had changed the in-memory
state drops it, and the next operation reloads everything.

**First load without the lock.** An instance's first operation (and the first after a dropped
state) reads the whole history in one ``REPEATABLE READ``, read-only snapshot *before* it takes
the global lock, so a cold start never holds every other instance's budget operations while the
history crosses the network. That snapshot is a committed prefix: a writer that has not committed
holds the lock and took its ``change_seq`` values after every committed row, so the delta read
under the lock picks it up.

**Rule for future code.** Every write to ``nexura_usage_entries`` or
``nexura_usage_reservations`` (a retention job, a backfill, manual SQL) must take
``pg_advisory_xact_lock(LEDGER_LOCK)`` first and give the rows a new ``change_seq``. A row
committed outside the lock could carry a sequence value below one a reader has already passed,
and that reader would never load it. ``nexura_usage_budget_policies`` is not reloaded this way
and needs no lock.

**Event loop.** ``blocking_io`` is true, so async callers in ``nexura-agents`` and the runtime
(``ledger_call``/``budget_call``) run every operation that can wait on the global lock in a
worker thread.

**Reads.** ``get_entries`` (run diagnostics, the usage module) is one indexed, read-only query
filtered by workspace and run; it takes no lock and loads no other entry. ``budget_policy`` is
one ``INSERT … ON CONFLICT`` (first registration wins, or replace), also without the global lock.

Tables: ``nexura_usage_entries``, ``nexura_usage_reservations`` and
``nexura_usage_budget_policies`` (the SQLite ``usage_entries``, ``reservations`` and
``budget_policies`` with a prefix, since the names are generic in a shared schema). Amounts stay
Decimal text, as in SQLite. Entries are listed in commit order (SQLite: by timestamp; the same
order unless instance clocks disagree).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator
from concurrent.futures import FIRST_COMPLETED, Future, wait
from typing import Any

from nexura_agents.usage.ledger import (
    ENTRY_COLUMNS,
    RESERVATION_COLUMNS,
    UsageLedger,
    entry_from_row,
    entry_values,
    reservation_from_row,
    reservation_values,
)
from nexura_agents.usage.models import LedgerEntry, Reservation

from nexura_postgres.migrations import MIGRATION_TIMEOUT, Migration, migrate_all, register

COMPONENT = "usage"
#: The one global ledger lock (transaction-scoped), the scope of SQLite's ``BEGIN IMMEDIATE``.
LEDGER_LOCK = 734658120
LOCK_SQL = "SELECT pg_advisory_xact_lock($1)"
#: Seconds to wait for the global lock (and for each statement of the transaction).
LOCK_TIMEOUT = 30.0
#: Seconds the transaction waits for the caller's check to finish before it rolls back.
HOLD_TIMEOUT = 60.0

MIGRATIONS: tuple[Migration, ...] = (
    (
        1,
        "usage ledger",
        (
            "CREATE SEQUENCE nexura_usage_change_seq",
            """CREATE TABLE nexura_usage_entries (
                entry_id TEXT PRIMARY KEY,
                timestamp DOUBLE PRECISION NOT NULL,
                workspace_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                parent_run_id TEXT,
                node_id TEXT,
                entry_type TEXT NOT NULL,
                entry_category TEXT NOT NULL DEFAULT 'provider_call',
                amount_usd TEXT NOT NULL,
                tokens BIGINT,
                prompt_tokens BIGINT,
                completion_tokens BIGINT,
                usage_state TEXT NOT NULL,
                model_id TEXT,
                turns BIGINT NOT NULL DEFAULT 0,
                tool_calls BIGINT NOT NULL DEFAULT 0,
                reservation_id TEXT,
                provider_call_id TEXT,
                pricing_version TEXT,
                description TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                change_seq BIGINT NOT NULL DEFAULT nextval('nexura_usage_change_seq'))""",
            """CREATE TABLE nexura_usage_reservations (
                reservation_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                parent_run_id TEXT,
                node_id TEXT,
                model_id TEXT,
                estimated_tokens BIGINT NOT NULL DEFAULT 0,
                estimated_cost_usd TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at DOUBLE PRECISION NOT NULL,
                timeout_seconds DOUBLE PRECISION NOT NULL DEFAULT 300.0,
                provider_call_id TEXT,
                pricing_version TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                change_seq BIGINT NOT NULL DEFAULT nextval('nexura_usage_change_seq'))""",
            """CREATE TABLE nexura_usage_budget_policies (
                kind TEXT NOT NULL,
                name TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (kind, name))""",
            "CREATE INDEX idx_usage_entries_change ON nexura_usage_entries (change_seq)",
            "CREATE INDEX idx_usage_entries_ws_run ON nexura_usage_entries (workspace_id, run_id)",
            "CREATE INDEX idx_usage_entries_parent ON nexura_usage_entries (parent_run_id)",
            "CREATE INDEX idx_usage_res_change ON nexura_usage_reservations (change_seq)",
            "CREATE INDEX idx_usage_res_ws_status ON nexura_usage_reservations "
            "(workspace_id, status)",
        ),
    ),
)
register(COMPONENT, MIGRATIONS)


def _upsert(table: str, columns: tuple[str, ...], key: str) -> str:
    """``INSERT OR REPLACE`` as one statement; a replaced row gets a new ``change_seq``."""
    names = ", ".join(columns)
    params = ", ".join(f"${i}" for i in range(1, len(columns) + 1))
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns if c != key)
    return (
        f"INSERT INTO {table} ({names}) VALUES ({params}) ON CONFLICT ({key}) DO UPDATE SET "
        f"{updates}, change_seq = nextval('nexura_usage_change_seq')"
    )


_ENTRY_UPSERT = _upsert("nexura_usage_entries", ENTRY_COLUMNS, "entry_id")
_RESERVATION_UPSERT = _upsert("nexura_usage_reservations", RESERVATION_COLUMNS, "reservation_id")
_ENTRY_SELECT = f"SELECT {', '.join(ENTRY_COLUMNS)} FROM nexura_usage_entries"
_RESERVATION_SELECT = f"SELECT {', '.join(RESERVATION_COLUMNS)} FROM nexura_usage_reservations"


class _Rollback(Exception):
    """Raised inside the held transaction to roll it back (the caller's check failed)."""


async def _changes(conn: Any, since: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Ledger rows changed after ``since``, in commit order, with their ``change_seq``."""
    entries = await conn.fetch(
        f"{_ENTRY_SELECT.replace(' FROM', ', change_seq FROM', 1)} "
        "WHERE change_seq > $1 ORDER BY change_seq",
        since,
    )
    reservations = await conn.fetch(
        f"{_RESERVATION_SELECT.replace(' FROM', ', change_seq FROM', 1)} "
        "WHERE change_seq > $1 ORDER BY change_seq",
        since,
    )
    return [dict(r) for r in entries], [dict(r) for r in reservations]


def _settle(future: Future[Any]) -> None:
    """Wait for a rolled-back transaction to end; its own error is not the caller's."""
    with contextlib.suppress(BaseException):
        future.result(LOCK_TIMEOUT)


class PostgresUsageLedger(UsageLedger):
    """Spend entries, reservations and budget policies in the shared Postgres ``Database``.

    ``database``: a ``nexura_postgres.Database`` (the service's shared pool); the ledger never
    closes it. Opening the ledger applies its migrations (component ``usage``); the rows are
    loaded on the first operation that needs them.
    """

    #: Operations can wait on the database-wide lock: async callers run them off the loop.
    blocking_io = True

    def __init__(self, database: Any, *, migrate: bool = True) -> None:
        super().__init__(None)  # in-memory fields only; ``db_path`` stays None
        self.database = database
        self._seen = 0  # highest change_seq applied to the in-memory state
        self._loaded = False  # the lock-free first load has run
        self._writes: list[tuple[str, tuple[Any, ...]]] | None = None
        if migrate:
            components = {COMPONENT: MIGRATIONS}

            async def apply(conn: Any) -> None:
                await migrate_all(conn, components)

            database.run(apply, timeout=MIGRATION_TIMEOUT + 30)

    # -- the global transaction --------------------------------------------------------------

    def _forget(self) -> None:
        """Drop the in-memory state; the next operation reloads every row."""
        self._entries = []
        self._reservations = {}
        self._seen = 0
        self._loaded = False

    def _apply(self, entries: list[dict[str, Any]], reservations: list[dict[str, Any]]) -> None:
        if entries:
            positions = {e.entry_id: i for i, e in enumerate(self._entries)}
            for row in entries:
                entry = entry_from_row(row)
                index = positions.get(entry.entry_id)
                if index is None:
                    positions[entry.entry_id] = len(self._entries)
                    self._entries.append(entry)
                else:
                    self._entries[index] = entry
        for row in reservations:
            res = reservation_from_row(row)
            self._reservations[res.reservation_id] = res
        seqs = [int(r["change_seq"]) for r in (*entries, *reservations)]
        self._seen = max([self._seen, *seqs])

    def _load_snapshot(self) -> None:
        """The whole history, read without the global lock (review WP5 M4): one read-only
        ``REPEATABLE READ`` transaction, so both tables come from one committed prefix."""

        async def read(conn: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            return await _changes(conn, 0)

        self._forget()
        try:
            self._apply(*self.database.run(read, isolation="repeatable_read", readonly=True))
            self._loaded = True
        except BaseException:
            self._forget()
            raise

    @contextlib.contextmanager
    def _atomic(self) -> Iterator[None]:
        """Global lock, reload of what changed, the caller's check, then its writes; one
        transaction. The check runs in the caller's thread (its context variables, such as the
        dispatch path ``usage.linkage`` records, stay in place) while the lock is held."""
        with self._thread_lock:
            if not self._loaded:
                self._load_snapshot()
            ready: Future[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = Future()
            verdict: Future[list[tuple[str, tuple[Any, ...]]] | None] = Future()
            since = self._seen

            async def hold(conn: Any) -> None:
                await conn.execute(LOCK_SQL, LEDGER_LOCK, timeout=LOCK_TIMEOUT)
                ready.set_result(await _changes(conn, since))
                writes = await asyncio.wait_for(asyncio.wrap_future(verdict), HOLD_TIMEOUT)
                if writes is None:
                    raise _Rollback
                for sql, args in writes:
                    await conn.execute(sql, *args, timeout=LOCK_TIMEOUT)

            outer = self.database.submit(hold)
            done, _ = wait([ready, outer], timeout=LOCK_TIMEOUT + 10, return_when=FIRST_COMPLETED)
            if ready not in done:
                verdict.set_result(None)
                if outer in done:
                    outer.result()  # the transaction failed before the check: its error
                outer.cancel()
                raise TimeoutError("Timed out waiting for the usage ledger lock")
            try:
                self._apply(*ready.result())
            except BaseException:
                verdict.set_result(None)
                _settle(outer)
                self._forget()
                raise
            self._writes = []
            sizes = (len(self._entries), len(self._reservations))
            try:
                yield
            except BaseException:
                changed = bool(self._writes) or sizes != (
                    len(self._entries),
                    len(self._reservations),
                )
                self._writes = None
                verdict.set_result(None)
                _settle(outer)
                if changed:
                    self._forget()  # the in-memory state holds what was rolled back
                raise
            writes, self._writes = self._writes, None
            verdict.set_result(writes)
            try:
                outer.result(LOCK_TIMEOUT + HOLD_TIMEOUT)
            except BaseException:
                self._forget()
                raise

    def _buffer(self, sql: str, args: tuple[Any, ...]) -> None:
        if self._writes is None:
            raise RuntimeError("Usage ledger rows are written only inside its transaction")
        self._writes.append((sql, args))

    def _save_entry_to_sqlite(self, entry: LedgerEntry) -> None:
        self._buffer(_ENTRY_UPSERT, entry_values(entry))

    def _save_reservation_to_sqlite(self, res: Reservation) -> None:
        self._buffer(_RESERVATION_UPSERT, reservation_values(res))

    # -- operations without the global lock --------------------------------------------------

    def budget_policy(
        self, kind: str, name: str, payload: str | None = None, *, replace: bool = False
    ) -> str | None:
        """Persist named policies alongside spend; first run registration wins atomically."""

        async def run(conn: Any) -> Any:
            if payload is not None:
                await conn.execute(
                    "INSERT INTO nexura_usage_budget_policies (kind, name, payload) "
                    "VALUES ($1, $2, $3) ON CONFLICT (kind, name) DO "
                    + ("UPDATE SET payload = EXCLUDED.payload" if replace else "NOTHING"),
                    kind,
                    name,
                    payload,
                )
            return await conn.fetchval(
                "SELECT payload FROM nexura_usage_budget_policies WHERE kind = $1 AND name = $2",
                kind,
                name,
            )

        # A pure read is one statement: no transaction (MVP WP11a).
        row = self.database.run(run, autocommit=payload is None)
        return None if row is None else str(row)

    def get_entries(
        self,
        workspace_id: str | None = None,
        run_id: str | None = None,
    ) -> list[LedgerEntry]:
        """Entries of a workspace and/or run (its own or as parent), in commit order.

        One indexed, read-only query: no global lock, and no other entry is loaded.
        """
        if any(v is not None and "\x00" in v for v in (workspace_id, run_id)):
            return []  # Postgres TEXT cannot hold NUL: no such entry exists
        clauses: list[str] = []
        args: list[Any] = []
        if workspace_id is not None:
            args.append(workspace_id)
            clauses.append(f"workspace_id = ${len(args)}")
        if run_id is not None:
            args.append(run_id)
            clauses.append(f"(run_id = ${len(args)} OR parent_run_id = ${len(args)})")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

        async def run(conn: Any) -> Any:
            return await conn.fetch(f"{_ENTRY_SELECT}{where} ORDER BY change_seq", *args)

        return [entry_from_row(dict(row)) for row in self.database.run(run, readonly=True)]

    async def reserve_async(self, **kwargs: Any) -> Reservation:
        """``reserve`` in a worker thread, so the event loop never waits on the lock."""
        return await asyncio.to_thread(lambda: self.reserve(**kwargs))

    def close(self) -> None:
        """Nothing to release: the shared ``Database`` belongs to the app."""


__all__ = ["COMPONENT", "LEDGER_LOCK", "MIGRATIONS", "PostgresUsageLedger"]
