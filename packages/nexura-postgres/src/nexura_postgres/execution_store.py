# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Postgres execution journal using asyncpg transactions and FOR UPDATE row locking.

Follows the repository.py transaction/CAS patterns without importing the runtime
or changing its older repository tables. Only the optional Postgres package needs
asyncpg; the shared journal logic and DDL remain in the stdlib-only SDK store.

Round trips (MVP WP11a): every statement costs two (asyncpg's statement cache is off), so the
journal combines statements where one snapshot or one lock covers them. ``get`` is one
statement (the run row and its steps, effects and events: one snapshot, no lock, no
transaction). An edit locks the run row, then reads the database time and the steps, effects
and latest event in one statement after the lock, and writes the fenced run update with its new
steps, effects and events in one statement. Child rows are returned as parallel arrays of text
(numbers as their exact text form), never as JSON.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from typing import Any

from nexura_agents.contracts.execution import ExecutionId, LeaseLostError
from nexura_agents.execution_store.base import _json
from nexura_agents.execution_store.models import ExecutionConflictError, ExecutionSnapshot
from nexura_agents.execution_store.sql import _INSERT_RUN, _Connection, _identity, _SQLStore

from nexura_postgres.db import noop_reset, normalize_url


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


_RUN_COLUMNS = (
    "workspace_id, run_id, definition_digest, journal_schema_version, state_version, status, "
    "lease_owner, lease_expires_at, fencing_token, revision, run_json"
)
# $1 workspace, $2 run, $3 step keys (NULL: all), $4 effect ids (NULL: all), $5 only the
# latest event. Each child table is read once per column, in primary-key order, inside one
# statement, so the arrays line up.
_CHILD_ARRAYS = """
    ARRAY(SELECT {col} FROM nexura_exec_steps WHERE workspace_id = $1 AND run_id = $2
          AND ($3::text[] IS NULL OR step_key = ANY($3::text[])) ORDER BY step_key) AS {alias}
"""
_STEP_ARRAYS = ", ".join(
    _CHILD_ARRAYS.format(col=col, alias="s_" + col)
    for col in ("step_key", "intent_json", "outcome_json")
)
_EFFECT_ARRAYS = ", ".join(
    f"""ARRAY(SELECT {col} FROM nexura_exec_effects WHERE workspace_id = $1 AND run_id = $2
          AND ($4::text[] IS NULL OR effect_id = ANY($4::text[])) ORDER BY effect_id)
        AS f_{col}"""
    for col in ("effect_id", "record_json")
)
# Numbers travel as text, because asyncpg introspects int8[]/float8[] result arrays on every
# new connection. bigint text is exact; double precision text is exact on Postgres 12 and
# later only while the server's extra_float_digits is 1 or more (the default; WP11a review L2).
# "AS v" keeps ORDER BY on the bigint column, not on a text output column of the same name.
_EVENT_ARRAYS = ", ".join(
    f"""ARRAY(SELECT {col}::text AS v FROM (SELECT * FROM nexura_exec_events
          WHERE workspace_id = $1 AND run_id = $2
          ORDER BY sequence DESC LIMIT CASE WHEN $5::boolean THEN 1 END) AS latest
          ORDER BY sequence) AS e_{col}"""
    for col in ("sequence", "event_type", "payload_json", "timestamp")
)
_CHILDREN = f"{_STEP_ARRAYS}, {_EFFECT_ARRAYS}, {_EVENT_ARRAYS}"
#: ``get``: run row and every child row, one statement.
_GET_SQL = (
    f"SELECT {_RUN_COLUMNS}, {_CHILDREN} FROM nexura_exec_runs "
    "WHERE workspace_id = $1 AND run_id = $2"
)
#: After the row lock: the database time and the selected child rows, one statement.
_AFTER_LOCK_SQL = f"SELECT EXTRACT(EPOCH FROM clock_timestamp()) AS now, {_CHILDREN}"
#: The fenced run update and the new child rows in one statement; returns the updated count
#: (0: fence refused; the child inserts then insert nothing).
_SAVE_SQL = """
WITH run AS (
    UPDATE nexura_exec_runs SET
        definition_digest = $3, journal_schema_version = $4, state_version = $5, status = $6,
        lease_owner = $7, lease_expires_at = $8, fencing_token = $9, revision = $10,
        run_json = $11
    WHERE workspace_id = $1 AND run_id = $2 AND fencing_token = $12 AND revision = $13
        AND lease_owner IS NOT DISTINCT FROM $14
    RETURNING 1
), steps AS (
    INSERT INTO nexura_exec_steps (workspace_id, run_id, step_key, intent_json, outcome_json)
    SELECT $1, $2, k.key, k.intent, k.outcome
    FROM unnest($15::text[], $16::text[], $17::text[]) AS k(key, intent, outcome)
    WHERE EXISTS (SELECT 1 FROM run)
    ON CONFLICT (workspace_id, run_id, step_key) DO UPDATE SET
        intent_json = excluded.intent_json, outcome_json = excluded.outcome_json
), effects AS (
    INSERT INTO nexura_exec_effects
        (workspace_id, run_id, effect_id, idempotency_key, status, resolution, resolved_by,
         record_json)
    SELECT $1, $2, f.effect, f.key, f.status, f.resolution, f.actor, f.record
    FROM unnest($18::text[], $19::text[], $20::text[], $21::text[], $22::text[], $23::text[])
        AS f(effect, key, status, resolution, actor, record)
    WHERE EXISTS (SELECT 1 FROM run)
    ON CONFLICT (workspace_id, run_id, effect_id) DO UPDATE SET
        status = excluded.status, resolution = excluded.resolution,
        resolved_by = excluded.resolved_by, record_json = excluded.record_json
), events AS (
    INSERT INTO nexura_exec_events
        (workspace_id, run_id, sequence, event_type, payload_json, timestamp)
    SELECT $1, $2, e.sequence, e.type, e.payload, e.now
    FROM unnest($24::bigint[], $25::text[], $26::text[], $27::double precision[])
        AS e(sequence, type, payload, now)
    WHERE EXISTS (SELECT 1 FROM run)
)
SELECT count(*) FROM run
"""


def _snapshot(row: Any) -> ExecutionSnapshot:
    """A journal snapshot from a ``_GET_SQL`` / ``_AFTER_LOCK_SQL`` row (and the run row)."""
    steps = [
        {"step_key": k, "intent_json": i, "outcome_json": o}
        for k, i, o in zip(
            row["s_step_key"], row["s_intent_json"], row["s_outcome_json"], strict=True
        )
    ]
    effects = [
        {"effect_id": k, "record_json": r}
        for k, r in zip(row["f_effect_id"], row["f_record_json"], strict=True)
    ]
    events = [
        {"sequence": int(n), "event_type": t, "payload_json": p, "timestamp": float(ts)}
        for n, t, p, ts in zip(
            row["e_sequence"],
            row["e_event_type"],
            row["e_payload_json"],
            row["e_timestamp"],
            strict=True,
        )
    ]
    return _SQLStore._snapshot(dict(row), steps, effects, events)


def _keys(keys: Sequence[str] | None) -> list[str] | None:
    return None if keys is None else list(keys)


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

                self._pool = await asyncpg.create_pool(
                    normalize_url(self._dsn, strict=False),
                    min_size=1,
                    max_size=5,
                    statement_cache_size=0,
                    reset=noop_reset,
                )
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

    async def _insert(self, run: Any, ttl: float) -> Any:
        """``_SQLStore._insert`` without a transaction (MVP WP11a): the clock read needs no
        lock and the insert is one atomic statement, so ``BEGIN``/``COMMIT`` add nothing. A
        run id that is taken still raises ``ExecutionConflictError``."""
        pool = await self._get_pool()
        try:
            async with pool.acquire() as connection:
                db = _PostgresConnection(connection)
                now = await self._now(db)
                run = replace(run, created_at=now, updated_at=now, lease_expires_at=now + ttl)
                await db.execute(_INSERT_RUN, self._run_params(run))
                return run
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == "23505":
                raise ExecutionConflictError("Execution journal uniqueness conflict") from exc
            raise

    async def get(self, execution_id: ExecutionId) -> ExecutionSnapshot:
        """The run and its journals, read in one statement (one snapshot).

        No lock and no transaction: a concurrent commit is seen entirely or not at all.
        """
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                _GET_SQL, execution_id.workspace_id, execution_id.run_id, None, None, False
            )
        if row is None:
            raise KeyError(execution_id)
        return _snapshot(row)

    async def _load_locked(
        self,
        db: _Connection,
        identity: ExecutionId,
        *,
        step_keys: Sequence[str] | None = None,
        effect_ids: Sequence[str] | None = None,
    ) -> tuple[ExecutionSnapshot, float]:
        """Lock the run row, then read the time and the child rows in one statement. The
        second statement starts after the lock is held, so it sees every row the previous
        lock holder committed, and its clock reading cannot predate a lock wait."""
        connection = db.connection  # type: ignore[attr-defined]
        run_row = await connection.fetchrow(
            f"SELECT {_RUN_COLUMNS} FROM nexura_exec_runs "
            "WHERE workspace_id = $1 AND run_id = $2 FOR UPDATE",
            identity.workspace_id,
            identity.run_id,
        )
        if run_row is None:
            raise KeyError(identity)
        after = await connection.fetchrow(
            _AFTER_LOCK_SQL,
            identity.workspace_id,
            identity.run_id,
            _keys(step_keys),
            _keys(effect_ids),
            step_keys is not None,
        )
        # Empty key lists select nothing (as _load does without querying).
        merged = {**dict(run_row), **dict(after)}
        return _snapshot(merged), float(after["now"])

    async def _save(
        self, db: _Connection, before: ExecutionSnapshot, after: ExecutionSnapshot
    ) -> None:
        """The fenced run update and the new journal rows, one statement."""
        run = after.run
        identity = _identity(run.execution_id)
        steps = [(k, s) for k, s in after.steps.items() if before.steps.get(k) != s]
        effects = [(k, e) for k, e in after.effects.items() if before.effects.get(k) != e]
        events = after.events[len(before.events) :]
        connection = db.connection  # type: ignore[attr-defined]
        changed = await connection.fetchval(
            _SAVE_SQL,
            identity["workspace"],
            identity["run"],
            run.definition_digest,
            run.journal_schema_version,
            run.state_version,
            run.status,
            run.lease_owner,
            run.lease_expires_at,
            run.fencing_token,
            run.revision,
            _json(asdict(run)),
            before.run.fencing_token,
            before.run.revision,
            before.run.lease_owner,
            [k for k, _ in steps],
            [_json(asdict(s.intent)) if s.intent is not None else None for _, s in steps],
            [_json(asdict(s.outcome)) if s.outcome is not None else None for _, s in steps],
            [k for k, _ in effects],
            [e.intent.idempotency_key for _, e in effects],
            [e.status.value for _, e in effects],
            [e.resolution.value if e.resolution else None for _, e in effects],
            [e.resolved_by for _, e in effects],
            [_json(asdict(e)) for _, e in effects],
            [e.sequence for e in events],
            [e.event_type for e in events],
            [_json(e.payload) for e in events],
            [e.timestamp for e in events],
        )
        if changed != 1:
            raise LeaseLostError("Fenced journal update refused")

    async def _migration_lock(self, db: _Connection) -> None:
        # Constant is journal-specific; lock works even before the schema exists.
        await db.fetch("SELECT pg_advisory_xact_lock(734658101)", {})

    async def close(self) -> None:
        """Close only a pool created by this store; supplied pools remain caller-owned."""
        if self._pool is not None and self._owns_pool:
            await self._pool.close()
            self._pool = None
