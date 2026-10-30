# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Private SQL journal implementation; driver adapters use only this small protocol."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import asdict, replace
from typing import Any, Protocol

from nexura_agents.contracts.execution import (
    EffectRecord,
    ExecutionId,
    LeaseLostError,
    StepIntent,
    StepOutcome,
)

from .base import _json, _Store, _Transaction, _versions
from .migrations import MIGRATIONS
from .models import (
    ExecutionEvent,
    ExecutionRun,
    ExecutionSnapshot,
    ExecutionStep,
    ExecutionVersionError,
)


class _Connection(Protocol):
    async def execute(self, sql: str, params: Mapping[str, Any]) -> int: ...
    async def fetch(self, sql: str, params: Mapping[str, Any]) -> list[dict[str, Any]]: ...


def _identity(identity: ExecutionId) -> dict[str, Any]:
    return {"workspace": identity.workspace_id, "run": identity.run_id}


def _run(row: dict[str, Any]) -> ExecutionRun:
    data = json.loads(row["run_json"])
    data["execution_id"] = ExecutionId(**data["execution_id"])
    # Indexed columns are authoritative, including when testing old/future envelopes.
    for name in (
        "definition_digest",
        "journal_schema_version",
        "state_version",
        "status",
        "lease_owner",
        "lease_expires_at",
        "fencing_token",
        "revision",
    ):
        data[name] = row[name]
    result = ExecutionRun(**data)
    _versions(result)
    return result


_INSERT_RUN = """INSERT INTO nexura_exec_runs (
    workspace_id, run_id, definition_digest, journal_schema_version, state_version,
    status, lease_owner, lease_expires_at, fencing_token, revision, run_json)
    VALUES (:workspace, :run, :definition_digest, :journal_schema_version,
    :state_version, :status, :lease_owner, :lease_expires_at, :fencing_token,
    :revision, :run_json)"""


class _SQLStore(_Store):
    _lock_suffix = ""
    _clock_sql = ""

    def _connection(self) -> AbstractAsyncContextManager[_Connection]:
        raise NotImplementedError

    async def _now(self, db: _Connection) -> float:
        rows = await db.fetch(self._clock_sql, {})
        return float(rows[0]["now"])

    async def _migration_lock(self, db: _Connection) -> None:
        pass

    async def init_db(self) -> None:
        """Apply the journal's versioned migrations atomically and refuse future versions."""
        async with self._connection() as db:
            await self._migration_lock(db)
            await db.execute(
                """CREATE TABLE IF NOT EXISTS nexura_exec_migrations (
                version INTEGER PRIMARY KEY, name TEXT NOT NULL,
                applied_at DOUBLE PRECISION NOT NULL)""",
                {},
            )
            # Postgres adapter takes a transaction advisory lock before reaching here.
            versions = {
                row["version"]
                for row in await db.fetch("SELECT version FROM nexura_exec_migrations", {})
            }
            known = {version for version, _, _ in MIGRATIONS}
            if versions - known:
                raise ExecutionVersionError("Unsupported execution migration version")
            for version, name, statements in MIGRATIONS:
                if version in versions:
                    continue
                for statement in statements:
                    await db.execute(statement, {})
                await db.execute(
                    "INSERT INTO nexura_exec_migrations (version, name, applied_at) "
                    "VALUES (:version, :name, :now)",
                    {"version": version, "name": name, "now": await self._now(db)},
                )

    async def _insert(self, run: ExecutionRun, ttl: float) -> ExecutionRun:
        async with self._connection() as db:
            now = await self._now(db)
            run = replace(run, created_at=now, updated_at=now, lease_expires_at=now + ttl)
            params = self._run_params(run)
            await db.execute(_INSERT_RUN, params)
            return run

    @staticmethod
    def _run_params(run: ExecutionRun) -> dict[str, Any]:
        return dict(
            _identity(run.execution_id),
            definition_digest=run.definition_digest,
            journal_schema_version=run.journal_schema_version,
            state_version=run.state_version,
            status=run.status,
            lease_owner=run.lease_owner,
            lease_expires_at=run.lease_expires_at,
            fencing_token=run.fencing_token,
            revision=run.revision,
            run_json=_json(asdict(run)),
        )

    async def _load(
        self,
        db: _Connection,
        identity: ExecutionId,
        *,
        step_keys: Sequence[str] | None = None,
        effect_ids: Sequence[str] | None = None,
    ) -> ExecutionSnapshot:
        params = _identity(identity)
        rows = await db.fetch(
            "SELECT * FROM nexura_exec_runs WHERE workspace_id=:workspace AND run_id=:run"
            + self._lock_suffix,
            params,
        )
        if not rows:
            raise KeyError(identity)

        async def selected(
            table: str, column: str, keys: Sequence[str] | None
        ) -> list[dict[str, Any]]:
            query = f"SELECT * FROM {table} WHERE workspace_id=:workspace AND run_id=:run"
            binds = dict(params)
            if keys is not None:
                if not keys:
                    return []
                names = [f"key{index}" for index in range(len(keys))]
                binds.update(zip(names, keys, strict=True))
                query += f" AND {column} IN (" + ",".join(f":{name}" for name in names) + ")"
            return await db.fetch(query, binds)

        step_rows = await selected("nexura_exec_steps", "step_key", step_keys)
        effect_rows = await selected("nexura_exec_effects", "effect_id", effect_ids)
        event_rows = await db.fetch(
            "SELECT * FROM nexura_exec_events WHERE workspace_id=:workspace "
            "AND run_id=:run ORDER BY sequence"
            + (" DESC LIMIT 1" if step_keys is not None else ""),
            params,
        )
        return self._snapshot(rows[0], step_rows, effect_rows, event_rows)

    @staticmethod
    def _snapshot(
        run_row: dict[str, Any],
        step_rows: Sequence[dict[str, Any]],
        effect_rows: Sequence[dict[str, Any]],
        event_rows: Sequence[dict[str, Any]],
    ) -> ExecutionSnapshot:
        """A snapshot from table rows (``SELECT *`` shaped dictionaries)."""
        run = _run(run_row)
        steps = {
            row["step_key"]: ExecutionStep(
                StepIntent(**json.loads(row["intent_json"])) if row["intent_json"] else None,
                StepOutcome(**json.loads(row["outcome_json"])) if row["outcome_json"] else None,
            )
            for row in step_rows
        }
        effects = {
            row["effect_id"]: EffectRecord(**json.loads(row["record_json"])) for row in effect_rows
        }
        events = [
            ExecutionEvent(
                row["sequence"],
                row["event_type"],
                json.loads(row["payload_json"]),
                row["timestamp"],
            )
            for row in event_rows
        ]
        return ExecutionSnapshot(run, steps, effects, events)

    async def _load_locked(
        self,
        db: _Connection,
        identity: ExecutionId,
        *,
        step_keys: Sequence[str] | None = None,
        effect_ids: Sequence[str] | None = None,
    ) -> tuple[ExecutionSnapshot, float]:
        """``_load`` (the run row locked) and then the database time, read after the lock so a
        lock wait cannot revive an expired lease. Backends may combine the reads."""
        snapshot = await self._load(db, identity, step_keys=step_keys, effect_ids=effect_ids)
        return snapshot, await self._now(db)

    @asynccontextmanager
    async def _edit(
        self,
        execution_id: ExecutionId,
        *,
        step_keys: Sequence[str] | None = None,
        effect_ids: Sequence[str] | None = None,
    ) -> AsyncIterator[_Transaction]:
        async with self._connection() as db:
            # Time is fetched AFTER obtaining the row lock, so lock wait cannot revive expiry.
            before, now = await self._load_locked(
                db, execution_id, step_keys=step_keys, effect_ids=effect_ids
            )
            tx = _Transaction(
                replace(
                    before,
                    steps=dict(before.steps),
                    effects=dict(before.effects),
                    events=list(before.events),
                ),
                now,
            )
            yield tx
            if tx.snapshot != before:
                await self._save(db, before, tx.snapshot)

    async def _save(
        self, db: _Connection, before: ExecutionSnapshot, after: ExecutionSnapshot
    ) -> None:
        params = self._run_params(after.run)
        params.update(
            old_owner=before.run.lease_owner,
            old_token=before.run.fencing_token,
            old_revision=before.run.revision,
        )
        changed = await db.execute(
            """UPDATE nexura_exec_runs SET
            definition_digest=:definition_digest, journal_schema_version=:journal_schema_version,
            state_version=:state_version, status=:status, lease_owner=:lease_owner,
            lease_expires_at=:lease_expires_at, fencing_token=:fencing_token,
            revision=:revision, run_json=:run_json
            WHERE workspace_id=:workspace AND run_id=:run AND fencing_token=:old_token
            AND revision=:old_revision AND
            (lease_owner=:old_owner OR (lease_owner IS NULL AND :old_owner IS NULL))""",
            params,
        )
        if changed != 1:
            raise LeaseLostError("Fenced journal update refused")
        identity = _identity(after.run.execution_id)
        for key, step in after.steps.items():
            if before.steps.get(key) == step:
                continue
            await db.execute(
                """INSERT INTO nexura_exec_steps
                (workspace_id, run_id, step_key, intent_json, outcome_json)
                VALUES (:workspace, :run, :key, :intent, :outcome)
                ON CONFLICT (workspace_id, run_id, step_key) DO UPDATE SET
                    intent_json=excluded.intent_json, outcome_json=excluded.outcome_json""",
                dict(
                    identity,
                    key=key,
                    intent=_json(asdict(step.intent)) if step.intent is not None else None,
                    outcome=_json(asdict(step.outcome)) if step.outcome is not None else None,
                ),
            )
        for effect_id, effect in after.effects.items():
            if before.effects.get(effect_id) == effect:
                continue
            await db.execute(
                """INSERT INTO nexura_exec_effects
                (workspace_id, run_id, effect_id, idempotency_key, status, resolution,
                resolved_by, record_json) VALUES (:workspace, :run, :effect, :key, :status,
                :resolution, :actor, :record)
                ON CONFLICT (workspace_id, run_id, effect_id) DO UPDATE SET
                    status=excluded.status, resolution=excluded.resolution,
                    resolved_by=excluded.resolved_by, record_json=excluded.record_json""",
                dict(
                    identity,
                    effect=effect_id,
                    key=effect.intent.idempotency_key,
                    status=effect.status.value,
                    resolution=effect.resolution.value if effect.resolution else None,
                    actor=effect.resolved_by,
                    record=_json(asdict(effect)),
                ),
            )
        for event in after.events[len(before.events) :]:
            await db.execute(
                """INSERT INTO nexura_exec_events
                (workspace_id, run_id, sequence, event_type, payload_json, timestamp)
                VALUES (:workspace, :run, :sequence, :type, :payload, :now)""",
                dict(
                    identity,
                    sequence=event.sequence,
                    type=event.event_type,
                    payload=_json(event.payload),
                    now=event.timestamp,
                ),
            )

    async def list_recoverable(self, workspace_id: str | None = None) -> list[ExecutionRun]:
        """Use the database clock to list free/expired running or interrupted runs."""
        async with self._connection() as db:
            rows = await db.fetch(
                """SELECT * FROM nexura_exec_runs
                WHERE status IN ('running', 'interrupted')
                AND (lease_owner IS NULL OR lease_expires_at IS NULL OR lease_expires_at<=:now)
                AND (CAST(:workspace AS TEXT) IS NULL OR workspace_id=:workspace)
                ORDER BY workspace_id, run_id""",
                {"now": await self._now(db), "workspace": workspace_id},
            )
            return [_run(row) for row in rows]
