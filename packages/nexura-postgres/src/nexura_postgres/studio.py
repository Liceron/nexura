# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Studio workflows, run records and run events on Postgres (MVP Postgres WP3, store #1).

``PostgresStudioStore`` is a ``StudioStore`` whose rows live in the shared service ``Database``
instead of the SQLite file, so every Cloud Run instance sees the same runs, traces and run
history. It keeps the public API, the table and column names and the returned dictionaries of
the SQLite store (both use the row converters in ``nexura_runtime.studio.store``).

How the SQLite mechanisms map (design §4, WP3 row):

- Tables come from the numbered migration component ``studio`` (the SQLite store adds columns
  with ``ALTER TABLE`` inside ``suppress``); the event id is an identity column.
- ``event_seq`` is ``MAX + 1`` under a per-run transaction-level advisory lock, so concurrent
  writers on any instance get unique, gap-free numbers; ``UNIQUE (run_id, event_seq)`` is the
  backstop. Event inserts take the lock (event, interrupt, open-time sweep). Since MVP WP11a a
  plain event append is one statement that takes the lock itself and retries when the
  constraint refuses a number read from a snapshot older than the lock (``_APPEND_EVENT``),
  and a run's first row and its RUN_STARTED event are one atomic statement without the lock
  (no other writer can number an event of a run it cannot see yet).
- A run read (row plus events) is one ``REPEATABLE READ READ ONLY`` transaction, the snapshot
  the SQLite store gets from ``BEGIN … ROLLBACK``.
- ``run_id = ANY($n::text[])`` replaces ``json_each``; ``(started_by_json::jsonb ->>
  'api_key_id')`` replaces ``json_extract``; ``IS NOT DISTINCT FROM`` replaces ``IS``; ``LIMIT
  ALL`` replaces ``LIMIT -1``; run ids sort with ``COLLATE "C"`` (SQLite's binary order).
- ``data_json`` is ``TEXT`` and holds the blob codec's text unchanged (never ``JSONB``, which
  reorders keys and would hide blob references from ``db blob-gc``).

Pooled-endpoint safety (§2b-5): every call is one transaction on the shared pool, or (MVP
WP11a) one statement without ``BEGIN``/``COMMIT`` when the call sends a single statement
(``_run1``); locks are transaction-scoped; no session state. Blob I/O happens outside any
database transaction.

Round trips (MVP WP11a): with asyncpg's statement cache off, every statement costs two round
trips and a transaction two more, so related statements are combined: the run row and its
events are read in one statement (one snapshot), a run's first row and its RUN_STARTED event
are one statement, and an event append checks its parent run inside the insert.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Collection, Sequence
from pathlib import Path
from typing import Any

from nexura_agents.durable import _host_identity, _start_ticks, lease_owner_is_dead
from nexura_agents.errors import (
    WorkflowConflictError,
    WorkflowError,
    WorkflowValidationError,
)

from nexura_postgres.blobs import PostgresBlobBinding
from nexura_postgres.migrations import MIGRATION_TIMEOUT, Migration, migrate_all, register
from nexura_runtime.studio.builtin_modules import get_default_workflow_spec
from nexura_runtime.studio.store import (
    DEFAULT_WORKFLOW_ID,
    EVENT_COLUMNS,
    RUN_DETAIL_COLUMNS,
    RUN_SUMMARY_COLUMNS,
    SWEEP_ERROR,
    SWEEP_REASON,
    WORKFLOW_COLUMNS,
    StudioStore,
    run_detail,
    run_event,
    run_summary,
    workflow_detail,
    workflow_summary,
)
from nexura_runtime.workflow import PendingApproval, WorkflowSpec

logger = logging.getLogger("nexura_runtime.studio.store")

COMPONENT = "studio"
MIGRATIONS: tuple[Migration, ...] = (
    (
        1,
        "studio workflows, runs and run events",
        (
            """CREATE TABLE nexura_studio_workflows (
                workflow_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1,
                spec_json TEXT NOT NULL,
                created_at DOUBLE PRECISION NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL)""",
            """CREATE TABLE nexura_studio_runs (
                run_id TEXT PRIMARY KEY,
                workflow_id TEXT,
                spec_hash TEXT NOT NULL,
                spec_json TEXT NOT NULL,
                status TEXT NOT NULL,
                current_node_id TEXT,
                input_state_json TEXT NOT NULL,
                current_state_json TEXT NOT NULL,
                step_count INTEGER NOT NULL DEFAULT 0,
                approval_id TEXT,
                approval_json TEXT,
                error TEXT,
                created_at DOUBLE PRECISION NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL,
                pending_input_json TEXT,
                forked_from_json TEXT,
                cancel_reason TEXT,
                started_by_json TEXT,
                owner_process TEXT,
                journal_workspace TEXT,
                pending_effect_json TEXT)""",
            """CREATE INDEX idx_studio_runs_created
                ON nexura_studio_runs (created_at DESC, run_id COLLATE "C" DESC)""",
            """CREATE INDEX idx_studio_runs_workflow
                ON nexura_studio_runs (workflow_id, created_at DESC)""",
            """CREATE INDEX idx_studio_runs_status ON nexura_studio_runs (status)""",
            """CREATE INDEX idx_studio_runs_journal
                ON nexura_studio_runs (journal_workspace, status)
                WHERE journal_workspace IS NOT NULL""",
            """CREATE TABLE nexura_studio_run_events (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                run_id TEXT NOT NULL,
                event_seq BIGINT NOT NULL,
                event_type TEXT NOT NULL,
                node_id TEXT,
                data_json TEXT NOT NULL,
                timestamp DOUBLE PRECISION NOT NULL,
                CONSTRAINT uq_studio_events_run_seq UNIQUE (run_id, event_seq))""",
        ),
    ),
)
register(COMPONENT, MIGRATIONS)

#: Serialises every event append of one run, on every instance (transaction-scoped).
RUN_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtextextended('nexura-studio-run/' || $1, 0))"
_INSERT_NEXT_EVENT = (
    "INSERT INTO nexura_studio_run_events "
    "(run_id, event_seq, event_type, node_id, data_json, timestamp) "
    "SELECT $1::text, COALESCE(MAX(event_seq), 0) + 1, $2::text, $3::text, $4::text, "
    "$5::double precision "
    "FROM nexura_studio_run_events WHERE run_id = $1 RETURNING event_seq"
)
#: One-statement event append (MVP WP11a), run without a transaction: it takes the run's lock
#: before its insert (so writers that lock first and then read ``MAX`` in a later statement,
#: such as ``append_event`` callers and older instances, wait for it to commit) and inserts
#: nothing (no row returned) when the parent run is missing. Its ``MAX`` comes from the
#: statement's snapshot, taken before the lock is granted, so after a competing append it is
#: stale: ``uq_studio_events_run_seq`` then refuses the duplicate, and the caller falls back to
#: ``_LOCKED_APPEND`` (lock, then ``MAX`` in a later statement), which cannot be stale.
_APPEND_EVENT = (
    "WITH locked AS MATERIALIZED (SELECT pg_advisory_xact_lock("
    "hashtextextended('nexura-studio-run/' || $1, 0))) "
    "INSERT INTO nexura_studio_run_events "
    "(run_id, event_seq, event_type, node_id, data_json, timestamp) "
    "SELECT $1::text, COALESCE(MAX(event_seq), 0) + 1, $2::text, $3::text, $4::text, "
    "$5::double precision "
    "FROM nexura_studio_run_events WHERE run_id = $1 "
    # The aggregate always yields one row, so HAVING always runs: the lock is held before
    # any row reaches the insert.
    "HAVING (SELECT count(*) FROM locked) = 1 "
    "AND EXISTS (SELECT 1 FROM nexura_studio_runs WHERE run_id = $1) "
    "RETURNING event_seq"
)
_EVENT_SEQ_CONSTRAINT = "uq_studio_events_run_seq"
#: The fallback append, after ``RUN_LOCK_SQL`` in the same transaction: the statement's
#: snapshot is taken after the lock is held, so ``MAX`` is current.
_LOCKED_APPEND = (
    "INSERT INTO nexura_studio_run_events "
    "(run_id, event_seq, event_type, node_id, data_json, timestamp) "
    "SELECT $1::text, COALESCE(MAX(event_seq), 0) + 1, $2::text, $3::text, $4::text, "
    "$5::double precision "
    "FROM nexura_studio_run_events WHERE run_id = $1 "
    "HAVING EXISTS (SELECT 1 FROM nexura_studio_runs WHERE run_id = $1) "
    "RETURNING event_seq"
)
#: The run row (``RUN_DETAIL_COLUMNS``) and its events as parallel text arrays (one per
#: ``EVENT_COLUMNS`` column, in ``event_seq`` order), from one statement (one snapshot).
#: Numbers travel as text: asyncpg would introspect int8[]/float8[] result arrays on every new
#: connection. Timestamps are exact on Postgres 12 and later while the server's
#: extra_float_digits is 1 or more (the default; WP11a review L2).
_EVENT_FIELDS = tuple(column.strip() for column in EVENT_COLUMNS.split(","))
_READ_RUN = (
    "SELECT {columns}, "
    + ", ".join(
        # "AS v": ORDER BY must name the bigint column, not this text output column.
        f"ARRAY(SELECT {col}::text AS v FROM nexura_studio_run_events WHERE run_id = $1 "
        f"ORDER BY event_seq) AS ev_{col}"
        for col in _EVENT_FIELDS
    )
    + " FROM nexura_studio_runs WHERE run_id = $1"
)


# ---------------------------------------------------------------------------------------------
# Run owners across instances (review M1)
#
# The SQLite store records "<host>:<pid>:<start marker>" and treats a run of its own host whose
# process is gone as interrupted (B46). That rule assumes one machine per database file. Here
# every instance reads every other instance's rows, and host names are not a machine identity
# (a container may be "localhost", and pid 1 everywhere). So a Postgres run owner is a durable
# lease owner: ``nx1.<host identity>.<pid>.<start ticks>.<process id>``, where the host
# identity hashes the host name, boot id and pid namespace (``nexura_agents.durable``) and the
# process id is random per process. An owner counts as gone only when it is a process in this
# same pid namespace that has exited (``lease_owner_is_dead``); any other owner, including every
# host:pid owner, is never judged here. Runs of a killed instance are recovered through their
# run lease, or, when the journal cannot tell, once the row has been idle for several lease
# periods (``nexura_runtime.service.run_liveness``).
# ---------------------------------------------------------------------------------------------

_OWNER_LOCK = threading.Lock()
_PROCESS_OWNER: tuple[int, str] | None = None


def process_run_owner() -> str:
    """This process's run owner (created once per process; a forked child gets a new one)."""
    global _PROCESS_OWNER
    pid = os.getpid()
    with _OWNER_LOCK:
        if _PROCESS_OWNER is None or _PROCESS_OWNER[0] != pid:
            owner = f"nx1.{_host_identity()}.{pid}.{_start_ticks(pid)}.{uuid.uuid4().hex}"
            _PROCESS_OWNER = (pid, owner)
        return _PROCESS_OWNER[1]


def run_owner_is_gone(owner: str | None) -> bool:
    """True when the run's owner is a process of this pid namespace that has exited.

    A row without an owner predates owners and counts as gone, as on SQLite (B46). Every other
    owner (another instance, another machine with the same host name, a host:pid owner from a
    SQLite import) is never judged gone here.
    """
    if not owner:
        return True
    if owner == process_run_owner():
        return False
    return bool(lease_owner_is_dead(owner))


async def append_event(
    conn: Any, run_id: str, event_type: str, node_id: str | None, data_json: str, now: float
) -> int:
    """Append one event as ``MAX(event_seq) + 1``. The caller holds the run's lock."""
    seq: int = await conn.fetchval(_INSERT_NEXT_EVENT, run_id, event_type, node_id, data_json, now)
    return seq


def _has_nul(*values: Any) -> bool:
    """Postgres TEXT cannot hold NUL; such an id can never name a stored row (review L6)."""
    return any(isinstance(value, str) and "\x00" in value for value in values)


class PostgresStudioStore(StudioStore):
    """Studio definitions, runs and run events in the shared Postgres ``Database``.

    ``database``: a ``nexura_postgres.Database`` (the service's shared pool). The store does not
    close it. ``checkpoint_store``: the workflow checkpoint store (normally a
    ``PostgresWorkflowStore`` on the same database); the store owns it and closes it with
    ``close()``. ``db_path``: the service's SQLite file, where the stores that are not on
    Postgres yet live; this store creates nothing there. ``blob_payloads``: as for
    ``StudioStore``; the database is then bound to the codec's blob store
    (``PostgresBlobBinding``).

    Opening the store applies its migrations, sweeps legacy (non-journaled) runs whose owning
    process on this host is gone, and seeds the default workflow into an empty table, as the
    SQLite store does.
    """

    def __init__(
        self,
        database: Any,
        *,
        checkpoint_store: Any,
        db_path: str | Path,
        blob_payloads: Any = None,
        migrate: bool = True,
    ) -> None:
        # StudioStore.__init__ is not called: it opens the SQLite file.
        if checkpoint_store is None:
            raise ValueError("PostgresStudioStore needs a checkpoint_store (workflow state)")
        self.database = database
        self.db_path = Path(db_path).resolve()
        self._lock = threading.Lock()
        self._closed = False
        self._conn = None
        self._blob_payloads: Any = None
        self.blob_payloads = blob_payloads
        if migrate:
            components = {COMPONENT: MIGRATIONS}

            async def apply(conn: Any) -> None:
                await migrate_all(conn, components)

            database.run(apply, timeout=MIGRATION_TIMEOUT + 30)
        self._sweep_and_seed()
        self.checkpoint_store: Any = checkpoint_store

    @property
    def blob_payloads(self) -> Any:
        return self._blob_payloads

    @blob_payloads.setter
    def blob_payloads(self, codec: Any) -> None:
        """Set the codec; the shared database is registered as one its references live in, so it
        is bound to the store (and the store to it, R3-1) before any blob reference is written."""
        self._blob_payloads = codec
        if codec is not None and hasattr(codec, "add_binding"):
            codec.add_binding(PostgresBlobBinding(self.database))

    #: Several instances share these rows: liveness also uses the row's idle time (M1).
    shared_across_instances = True

    def this_owner(self) -> str:
        """The ``owner_process`` value runs started or resumed by this process record."""
        return process_run_owner()

    def owner_is_gone(self, owner: str | None) -> bool:
        return run_owner_is_gone(owner)

    def _run(self, fn: Any, **options: Any) -> Any:
        if self._closed:
            raise WorkflowError("StudioStore connection is closed.")
        return self.database.run(fn, **options)

    def _run1(self, fn: Any) -> Any:
        """``_run`` for a callback that sends exactly one statement: no BEGIN/COMMIT."""
        return self._run(fn, autocommit=True)

    # -------------------------------------------------------------------------
    # Open: sweep legacy runs, seed the default workflow
    # -------------------------------------------------------------------------

    def _sweep_and_seed(self) -> None:
        async def candidates(conn: Any) -> Any:
            return await conn.fetch(
                "SELECT run_id, owner_process FROM nexura_studio_runs "
                "WHERE status = 'running' AND journal_workspace IS NULL"
            )

        for row in self._run(candidates):
            if run_owner_is_gone(row["owner_process"]):
                self._sweep_one(row["run_id"], row["owner_process"])

        default_spec = get_default_workflow_spec()
        spec_json = default_spec.to_json(indent=2)

        async def seed(conn: Any) -> None:
            now = time.time()
            # Two instances opening an empty database at once: one insert wins, the other is
            # a no-op on the primary key.
            await conn.execute(
                "INSERT INTO nexura_studio_workflows "
                "(workflow_id, name, revision, spec_json, created_at, updated_at) "
                "SELECT $1::text, $2::text, 1, $3::text, $4::double precision, "
                "$4::double precision "
                "WHERE NOT EXISTS (SELECT 1 FROM nexura_studio_workflows) "
                "ON CONFLICT (workflow_id) DO NOTHING",
                DEFAULT_WORKFLOW_ID,
                default_spec.name,
                spec_json,
                now,
            )

        self._run(seed)

    def _sweep_one(self, run_id: str, owner: str | None) -> bool:
        async def sweep(conn: Any) -> bool:
            await conn.execute(RUN_LOCK_SQL, run_id)
            now = time.time()
            # Compare-and-set: skip a run whose owner changed since it was read.
            swept = await conn.fetchval(
                "UPDATE nexura_studio_runs "
                "SET status = 'interrupted', error = $1, updated_at = $2 "
                "WHERE run_id = $3 AND status = 'running' "
                "AND owner_process IS NOT DISTINCT FROM $4 AND journal_workspace IS NULL "
                "RETURNING 1",
                SWEEP_ERROR,
                now,
                run_id,
                owner,
            )
            if swept is None:
                return False
            reason = json.dumps({"reason": SWEEP_REASON}, ensure_ascii=False)
            await append_event(conn, run_id, "RUN_INTERRUPTED", None, reason, now)
            return True

        return bool(self._run(sweep))

    # -------------------------------------------------------------------------
    # Workflows
    # -------------------------------------------------------------------------

    def _list_workflows_sync(self) -> list[dict[str, Any]]:
        async def fetch(conn: Any) -> Any:
            return await conn.fetch(
                f"SELECT {WORKFLOW_COLUMNS} FROM nexura_studio_workflows ORDER BY updated_at DESC"
            )

        return [workflow_summary(row) for row in self._run1(fetch)]

    def _get_workflow_sync(self, workflow_id: str) -> dict[str, Any] | None:
        if _has_nul(workflow_id):
            return None

        async def fetch(conn: Any) -> Any:
            return await conn.fetchrow(
                f"SELECT {WORKFLOW_COLUMNS} FROM nexura_studio_workflows WHERE workflow_id = $1",
                workflow_id,
            )

        row = self._run1(fetch)
        return None if row is None else workflow_detail(row)

    def _create_workflow_sync(
        self, spec: WorkflowSpec, workflow_id: str | None = None
    ) -> dict[str, Any]:
        wf_id = workflow_id or f"wf-{uuid.uuid4().hex[:12]}"
        if _has_nul(wf_id):
            raise WorkflowValidationError("Workflow IDs cannot contain NUL characters.")
        now = time.time()
        spec_json = spec.to_json(indent=2)

        async def insert(conn: Any) -> Any:
            return await conn.fetchval(
                "INSERT INTO nexura_studio_workflows "
                "(workflow_id, name, revision, spec_json, created_at, updated_at) "
                "VALUES ($1, $2, 1, $3, $4, $4) "
                "ON CONFLICT (workflow_id) DO NOTHING RETURNING 1",
                wf_id,
                spec.name,
                spec_json,
                now,
            )

        if self._run1(insert) is None:
            raise WorkflowValidationError(f"Workflow ID '{wf_id}' already exists.")
        return {
            "workflow_id": wf_id,
            "name": spec.name,
            "revision": 1,
            "spec": spec.to_dict(),
            "spec_hash": spec.canonical_hash(),
            "created_at": now,
            "updated_at": now,
        }

    def _update_workflow_sync(
        self, workflow_id: str, spec: WorkflowSpec, expected_revision: int
    ) -> dict[str, Any]:
        now = time.time()
        spec_json = spec.to_json(indent=2)

        async def update(conn: Any) -> tuple[int, float]:
            row = await conn.fetchrow(
                "SELECT revision, created_at FROM nexura_studio_workflows "
                "WHERE workflow_id = $1 FOR UPDATE",
                workflow_id,
            )
            if row is None:
                raise WorkflowValidationError(f"Workflow '{workflow_id}' not found.")
            current_rev, created_at = row["revision"], row["created_at"]
            if current_rev != expected_revision:
                raise WorkflowConflictError(
                    workflow_id=workflow_id,
                    expected_revision=expected_revision,
                    actual_revision=current_rev,
                )
            await conn.execute(
                "UPDATE nexura_studio_workflows "
                "SET name = $1, revision = $2, spec_json = $3, updated_at = $4 "
                "WHERE workflow_id = $5",
                spec.name,
                current_rev + 1,
                spec_json,
                now,
                workflow_id,
            )
            return current_rev + 1, created_at

        new_rev, created_at = self._run(update)
        return {
            "workflow_id": workflow_id,
            "name": spec.name,
            "revision": new_rev,
            "spec": spec.to_dict(),
            "spec_hash": spec.canonical_hash(),
            "created_at": created_at,
            "updated_at": now,
        }

    def _delete_workflow_sync(self, workflow_id: str) -> bool:
        if _has_nul(workflow_id):
            return False

        async def delete(conn: Any) -> Any:
            return await conn.fetchval(
                "DELETE FROM nexura_studio_workflows WHERE workflow_id = $1 RETURNING 1",
                workflow_id,
            )

        return self._run(delete) is not None

    # -------------------------------------------------------------------------
    # Runs and events
    # -------------------------------------------------------------------------

    def _record_run_start_sync(
        self,
        run_id: str,
        workflow_id: str | None,
        spec: WorkflowSpec,
        initial_state: dict[str, Any],
        forked_from: dict[str, Any] | None = None,
        started_by: dict[str, Any] | None = None,
        journal_workspace: str | None = None,
    ) -> None:
        # A large initial state goes to the blob store before any transaction starts.
        started_json = self._event_data_text(
            journal_workspace,
            {"workflow_id": workflow_id, "initial_state": initial_state},
        )
        now = time.time()
        forked_json = json.dumps(forked_from, ensure_ascii=False) if forked_from else None
        started_by_json = json.dumps(started_by, ensure_ascii=False) if started_by else None
        spec_json = spec.to_json(indent=2)
        spec_hash = spec.canonical_hash()
        state_json = json.dumps(initial_state, ensure_ascii=False)
        owner = process_run_owner()

        async def insert(conn: Any) -> None:
            # The run row and its RUN_STARTED event in one statement, with no transaction or
            # run lock (MVP WP11a). The statement is atomic: an event appender sees both rows
            # or neither, so it never finds the run without event 1 and cannot take number 1.
            # A run id that is taken (primary key violation) fails the whole statement.
            await conn.execute(
                "WITH run AS (INSERT INTO nexura_studio_runs ("
                "run_id, workflow_id, spec_hash, spec_json, status, current_node_id, "
                "input_state_json, current_state_json, step_count, created_at, updated_at, "
                "forked_from_json, started_by_json, owner_process, journal_workspace"
                ") VALUES ($1, $2, $3, $4, 'running', $5, $6, $6, 0, $7, $7, $8, $9, $10, $11) "
                "RETURNING run_id) "
                "INSERT INTO nexura_studio_run_events "
                "(run_id, event_seq, event_type, node_id, data_json, timestamp) "
                "SELECT run_id, 1, 'RUN_STARTED', $5, $12, $7 FROM run",
                run_id,
                workflow_id,
                spec_hash,
                spec_json,
                spec.entry_node,
                state_json,
                now,
                forked_json,
                started_by_json,
                owner,
                journal_workspace,
                started_json,
            )

        self._run1(insert)

    def _run_workspace(self, run_id: str) -> str | None:
        if _has_nul(run_id):
            return None

        async def fetch(conn: Any) -> Any:
            return await conn.fetchval(
                "SELECT journal_workspace FROM nexura_studio_runs WHERE run_id = $1", run_id
            )

        value = self._run1(fetch)
        return None if value is None else str(value)

    def _record_run_event_sync(
        self,
        run_id: str,
        event_type: str,
        node_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        data_json = json.dumps(data or {}, ensure_ascii=False)
        codec = self.blob_payloads
        if codec is not None and codec.needs_offload(data_json):
            # The blob is written before the transaction; an event that is then not recorded
            # (no parent run) leaves an unreferenced blob for blob retention.
            data_json = self._event_data_text(self._run_workspace(run_id), data_json)
        now = time.time()

        args = (run_id, event_type, node_id, data_json, now)

        async def append(conn: Any) -> bool | None:
            if _has_nul(run_id):
                return False  # no such parent run (TEXT cannot hold NUL)
            import asyncpg  # type: ignore[import-untyped]

            # One statement, no transaction (MVP WP11a); see ``_APPEND_EVENT``.
            try:
                seq = await conn.fetchval(_APPEND_EVENT, *args)
            except asyncpg.UniqueViolationError as exc:
                if exc.constraint_name != _EVENT_SEQ_CONSTRAINT:
                    raise
                return None  # a competing append took the number: fall back
            return seq is not None  # no row: no parent run

        async def locked_append(conn: Any) -> bool:
            await conn.execute(RUN_LOCK_SQL, run_id)
            return await conn.fetchval(_LOCKED_APPEND, *args) is not None

        recorded = self._run1(append)
        if recorded is None:
            recorded = self._run(locked_append)
        if not recorded:
            logger.warning(
                "Run event %s was not recorded: parent run %s does not exist", event_type, run_id
            )

    def _update_run_sync(
        self,
        run_id: str,
        status: str,
        current_node_id: str | None,
        current_state: dict[str, Any],
        step_count: int,
        pending_approval: PendingApproval | None = None,
        error: str | None = None,
        pending_input: dict[str, Any] | None = None,
        cancel_reason: str | None = None,
        unless_status: Collection[str] = (),
        pending_effect: dict[str, Any] | None = None,
    ) -> bool:
        if _has_nul(run_id):
            return False
        now = time.time()
        state_json = json.dumps(current_state, ensure_ascii=False)
        effect_json = (
            json.dumps(pending_effect, ensure_ascii=False) if pending_effect is not None else None
        )
        app_id = pending_approval.approval_id if pending_approval else None
        app_json = (
            json.dumps(pending_approval.to_dict(), ensure_ascii=False) if pending_approval else None
        )
        input_json = (
            json.dumps(pending_input, ensure_ascii=False) if pending_input is not None else None
        )
        # The process setting a run (back) to running now executes it.
        owner = process_run_owner() if status == "running" else None
        guarded = sorted(set(unless_status))

        async def update(conn: Any) -> Any:
            # Compare-and-set: the row is left alone while its status is in ``unless_status``.
            return await conn.fetchval(
                "UPDATE nexura_studio_runs "
                "SET status = $1, current_node_id = $2, current_state_json = $3, "
                "step_count = $4, approval_id = $5, approval_json = $6, error = $7, "
                "updated_at = $8, pending_input_json = $9, pending_effect_json = $10, "
                "cancel_reason = COALESCE($11, cancel_reason), "
                "owner_process = COALESCE($12, owner_process) "
                "WHERE run_id = $13 AND NOT (status = ANY($14::text[])) RETURNING 1",
                status,
                current_node_id,
                state_json,
                step_count,
                app_id,
                app_json,
                error,
                now,
                input_json,
                effect_json,
                cancel_reason,
                owner,
                run_id,
                guarded,
            )

        return self._run1(update) is not None

    def _get_run_status_sync(self, run_id: str) -> str | None:
        if _has_nul(run_id):
            return None

        async def fetch(conn: Any) -> Any:
            return await conn.fetchval(
                "SELECT status FROM nexura_studio_runs WHERE run_id = $1", run_id
            )

        value = self._run1(fetch)
        return None if value is None else str(value)

    def _list_run_ids_started_by_key_sync(self, api_key_id: str) -> list[str]:
        # Review L6: no jsonb cast in SQL (one row holding \u0000 would fail the whole query
        # and with it key-revocation cancellation). A substring pre-filter on the key's JSON
        # text (as the writer encodes it) narrows the rows; Python checks the parsed value.
        if "\x00" in api_key_id:
            return []
        needle = json.dumps(api_key_id, ensure_ascii=False)

        async def fetch(conn: Any) -> Any:
            return await conn.fetch(
                "SELECT run_id, started_by_json FROM nexura_studio_runs "
                "WHERE started_by_json IS NOT NULL AND strpos(started_by_json, $1) > 0",
                needle,
            )

        found: list[str] = []
        for row in self._run1(fetch):
            try:
                started_by = json.loads(row["started_by_json"])
            except ValueError:
                continue
            if isinstance(started_by, dict) and started_by.get("api_key_id") == api_key_id:
                found.append(str(row["run_id"]))
        return found

    @staticmethod
    def _pg_run_filter(
        workflow_id: str | None, run_ids: Sequence[str] | None, status: str | None
    ) -> tuple[str, list[Any]]:
        """WHERE clause for run listings; the workspace id set is applied in SQL."""
        clauses: list[str] = []
        params: list[Any] = []
        if _has_nul(workflow_id, status):
            clauses.append("FALSE")  # no stored row can match a NUL value
        if workflow_id:
            params.append(workflow_id)
            clauses.append(f"workflow_id = ${len(params)}")
        if status is not None:
            params.append(status)
            clauses.append(f"status = ${len(params)}")
        if run_ids is not None:
            params.append([run_id for run_id in run_ids if not _has_nul(run_id)])
            clauses.append(f"run_id = ANY(${len(params)}::text[])")
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    def _count_runs_sync(
        self,
        workflow_id: str | None = None,
        run_ids: Sequence[str] | None = None,
        status: str | None = None,
    ) -> int:
        where, params = self._pg_run_filter(workflow_id, run_ids, status)

        async def count(conn: Any) -> Any:
            return await conn.fetchval("SELECT COUNT(*) FROM nexura_studio_runs" + where, *params)

        return int(self._run1(count) or 0)

    def _list_runs_sync(
        self,
        workflow_id: str | None = None,
        limit: int | None = 50,
        run_ids: Sequence[str] | None = None,
        status: str | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        where, params = self._pg_run_filter(workflow_id, run_ids, status)
        if limit is None or int(limit) < 0:  # SQLite reads a negative LIMIT as "no limit"
            page = "LIMIT ALL"
        else:
            params.append(int(limit))
            page = f"LIMIT ${len(params)}"
        params.append(max(0, int(offset)))
        sql = (
            f"SELECT {RUN_SUMMARY_COLUMNS} FROM nexura_studio_runs"
            + where
            + f' ORDER BY created_at DESC, run_id COLLATE "C" DESC {page} OFFSET ${len(params)}'
        )

        async def fetch(conn: Any) -> Any:
            return await conn.fetch(sql, *params)

        return [run_summary(row) for row in self._run1(fetch)]

    def _read_run_sync(self, run_id: str, *, raw_data: bool = False) -> dict[str, Any] | None:
        if _has_nul(run_id):
            return None

        sql = _READ_RUN.format(columns=RUN_DETAIL_COLUMNS)

        async def fetch(conn: Any) -> Any:
            return await conn.fetchrow(sql, run_id)

        # The run row and its events come from one snapshot: one statement (MVP WP11a; it was
        # a REPEATABLE READ READ ONLY transaction of two statements).
        row = self._run1(fetch)
        if row is None:
            return None
        events = [
            (int(seq), ev_type, node, data, float(ts))
            for seq, ev_type, node, data, ts in zip(
                *(row["ev_" + col] for col in _EVENT_FIELDS), strict=True
            )
        ]
        detail = tuple(row[index] for index in range(len(row) - len(_EVENT_FIELDS)))
        return run_detail(detail, [run_event(ev, raw_data) for ev in events], raw_data)

    def _list_journaled_runs_sync(
        self,
        statuses: Sequence[str],
        workspace_id: str | None,
        run_ids: Sequence[str] | None,
    ) -> list[dict[str, Any]]:
        clauses = ["journal_workspace IS NOT NULL", "status = ANY($1::text[])"]
        params: list[Any] = [list(statuses)]
        if workspace_id is not None:
            params.append(workspace_id)
            clauses.append(f"journal_workspace = ${len(params)}")
        if run_ids is not None:
            params.append([run_id for run_id in run_ids if not _has_nul(run_id)])
            clauses.append(f"run_id = ANY(${len(params)}::text[])")
        if _has_nul(workspace_id):
            clauses.append("FALSE")
        sql = (
            "SELECT run_id, status, journal_workspace, owner_process, updated_at "
            "FROM nexura_studio_runs "
            "WHERE " + " AND ".join(clauses) + ' ORDER BY created_at, run_id COLLATE "C"'
        )

        async def fetch(conn: Any) -> Any:
            return await conn.fetch(sql, *params)

        return [
            {
                "run_id": row["run_id"],
                "status": row["status"],
                "journal_workspace": row["journal_workspace"],
                "owner_process": row["owner_process"],
                # Shared rows: run liveness also uses the row's idle time (review M1).
                "updated_at": row["updated_at"],
            }
            for row in self._run1(fetch)
        ]

    def _mark_interrupted_sync(self, run_id: str, error: str, reason: str) -> bool:
        if _has_nul(run_id):
            return False
        data_json = json.dumps({"reason": reason}, ensure_ascii=False)

        async def mark(conn: Any) -> bool:
            await conn.execute(RUN_LOCK_SQL, run_id)
            now = time.time()
            updated = await conn.fetchval(
                "UPDATE nexura_studio_runs SET status = 'interrupted', error = $1, "
                "updated_at = $2 WHERE run_id = $3 AND status = 'running' RETURNING 1",
                error,
                now,
                run_id,
            )
            if updated is None:
                return False
            await append_event(conn, run_id, "RUN_INTERRUPTED", None, data_json, now)
            return True

        return bool(self._run(mark))

    # -------------------------------------------------------------------------
    # Close
    # -------------------------------------------------------------------------

    def _close_connection(self) -> None:
        # The shared Database belongs to the app (ServiceStores); only this store is closed.
        self._closed = True

    def _close_sync(self) -> None:
        self._close_connection()
        close_sync = getattr(self.checkpoint_store, "close_sync", None)
        if callable(close_sync):  # as the SQLite store does (review L8)
            close_sync()

    async def close(self) -> None:
        self._close_connection()
        await self.checkpoint_store.close()
