# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Evaluation datasets, runs and traces, and Studio evals, on Postgres (MVP WP8, stores #14, #15).

``PostgresEvaluationStore`` has the API of ``nexura_runtime.evaluation.store.SqliteEvaluationStore``
(migration component ``evaluations``); ``PostgresStudioEvalStore`` the API of
``nexura_runtime.evaluation.studio_store.StudioEvalStore`` (component ``studio_evaluations``).
Table and column names are SQLite's.

- **Blob references (MVP WP2b).** ``eval_traces.trace_json`` is ``TEXT`` holding the blob codec's
  text unchanged (never ``JSONB``: it reorders keys, and ``db blob-gc`` would miss every marker).
  Setting ``blob_payloads`` registers ``PostgresBlobBinding`` for this database, as the Postgres
  run store does, so a large trace goes to the blob store and the row keeps the reference.
- **Stable order.** SQLite breaks ``created_at`` ties with ``rowid``; here every table that is
  listed has a ``seq`` identity column, and lists order by ``created_at`` then ``seq``. A Studio
  eval run takes its ``created_at`` from the database clock (``clock_timestamp()``) while it holds
  its workspace lock, so runs saved on several instances list newest first in the order they
  were saved, whatever the instance clocks say.
- **Concurrency.** A Studio dataset save is a compare-and-set on its revision: it takes a
  transaction-level advisory lock on (workspace, dataset), then reads, checks and writes, so two
  instances saving revision ``n`` give one ``n + 1`` and one ``ValueError('conflict:n+1')``
  (SQLite: its process lock). A Studio run save takes the workspace's run lock, so the
  ``MAX_RUNS_PER_WORKSPACE`` retention keeps exactly the newest runs. Other writes are single
  upserts.
- **NUL.** Postgres ``TEXT`` cannot hold NUL. JSON columns never contain one (the encoders escape
  it); in every other text value NUL is stored, and looked up, as U+FFFD.

Every synchronous call is a network round trip (``blocking_io = True``): the service's async
routes run them in a worker thread. Pooled-endpoint safety: one transaction per call,
transaction-scoped locks, no session state.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from nexura_postgres.blobs import PostgresBlobBinding
from nexura_postgres.migrations import MIGRATION_TIMEOUT, Migration, migrate_all, register
from nexura_runtime.blobs.base import BlobIntegrityError, BlobNotFoundError
from nexura_runtime.blobs.payloads import EVAL_TRACE_NAMESPACE
from nexura_runtime.evaluation import studio_store
from nexura_runtime.evaluation.dataset import create_synthetic_support_dataset
from nexura_runtime.evaluation.errors import DatasetNotFoundError
from nexura_runtime.evaluation.models import (
    DatasetExample,
    EvaluationDataset,
    EvaluationSummary,
    StoredTrace,
)
from nexura_runtime.store_retention import DEFAULT_PRUNE_LIMIT, prune_cutoff

logger = logging.getLogger("nexura_runtime.evaluation.store")

COMPONENT = "evaluations"
MIGRATIONS: tuple[Migration, ...] = (
    (
        1,
        "evaluation datasets, runs and traces",
        (
            """CREATE TABLE eval_datasets (
                dataset_id TEXT NOT NULL,
                version TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                description TEXT NOT NULL,
                examples_json TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                created_at DOUBLE PRECISION NOT NULL,
                seq BIGINT GENERATED ALWAYS AS IDENTITY,
                PRIMARY KEY (dataset_id, version, workspace_id))""",
            "CREATE INDEX eval_datasets_ws ON eval_datasets "
            "(workspace_id, created_at DESC, seq DESC)",
            """CREATE TABLE eval_runs (
                run_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                dataset_id TEXT NOT NULL,
                dataset_version TEXT NOT NULL,
                workflow_version TEXT,
                spec_hash TEXT,
                pass_rate DOUBLE PRECISION NOT NULL,
                mean_score DOUBLE PRECISION NOT NULL,
                total_examples BIGINT NOT NULL,
                passed_examples BIGINT NOT NULL,
                failed_examples BIGINT NOT NULL,
                total_latency_ms DOUBLE PRECISION NOT NULL,
                total_cost_usd TEXT NOT NULL,
                created_at DOUBLE PRECISION NOT NULL,
                summary_json TEXT NOT NULL,
                seq BIGINT GENERATED ALWAYS AS IDENTITY)""",
            "CREATE INDEX eval_runs_ws ON eval_runs (workspace_id, created_at DESC, seq DESC)",
            # trace_json holds the blob codec's text unchanged: TEXT, never JSONB (WP2b).
            """CREATE TABLE eval_traces (
                run_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                trace_json TEXT NOT NULL,
                PRIMARY KEY (run_id, workspace_id))""",
        ),
    ),
)
register(COMPONENT, MIGRATIONS)

RETENTION_COMPONENT = "evaluation_trace_retention"
RETENTION_MIGRATIONS: tuple[Migration, ...] = (
    (
        1,
        "evaluation trace retention timestamps",
        (
            "ALTER TABLE eval_traces ADD COLUMN created_at DOUBLE PRECISION",
            "CREATE INDEX eval_trace_retention ON eval_traces (created_at, run_id, workspace_id)",
        ),
    ),
    (
        2,
        "index evaluation run timestamps for trace retention",
        ("CREATE INDEX eval_run_retention ON eval_runs (created_at, run_id, workspace_id)",),
    ),
)
register(RETENTION_COMPONENT, RETENTION_MIGRATIONS)

STUDIO_COMPONENT = "studio_evaluations"
STUDIO_MIGRATIONS: tuple[Migration, ...] = (
    (
        1,
        "studio eval datasets and runs",
        (
            """CREATE TABLE studio_eval_datasets (
                workspace_id TEXT NOT NULL,
                dataset_id TEXT NOT NULL,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                revision BIGINT NOT NULL,
                content_hash TEXT NOT NULL,
                cases_json TEXT NOT NULL,
                created_at DOUBLE PRECISION NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL,
                workflow_id TEXT,
                PRIMARY KEY (workspace_id, dataset_id))""",
            # seq replaces SQLite's rowid as the tie-break of the run list and retention.
            """CREATE TABLE studio_eval_runs (
                workspace_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                dataset_id TEXT NOT NULL,
                dataset_revision BIGINT NOT NULL,
                dataset_hash TEXT NOT NULL,
                label TEXT NOT NULL,
                target_json TEXT NOT NULL,
                scorers_json TEXT NOT NULL,
                summary_json TEXT NOT NULL,
                report_json TEXT NOT NULL,
                created_at DOUBLE PRECISION NOT NULL,
                seq BIGINT GENERATED ALWAYS AS IDENTITY,
                PRIMARY KEY (workspace_id, run_id))""",
            "CREATE INDEX studio_eval_runs_ws ON studio_eval_runs "
            "(workspace_id, created_at DESC, seq DESC)",
        ),
    ),
)
register(STUDIO_COMPONENT, STUDIO_MIGRATIONS)

#: One Studio dataset's read-check-write (revision compare-and-set), on every instance.
DATASET_LOCK_SQL = (
    "SELECT pg_advisory_xact_lock(hashtextextended("
    "'nexura-studio-eval-ds/' || length($1) || '/' || $1 || '/' || $2, 0))"
)
#: A workspace's Studio run inserts and their retention, on every instance.
RUNS_LOCK_SQL = (
    "SELECT pg_advisory_xact_lock(hashtextextended('nexura-studio-eval-runs/' || $1, 0))"
)


def _t(value: str) -> str:
    """Postgres ``TEXT`` cannot hold NUL: stored (and looked up) as U+FFFD."""
    return value.replace("\x00", "\ufffd")


def _t_opt(value: str | None) -> str | None:
    return None if value is None else _t(value)


def _migrate(database: Any, components: dict[str, tuple[Migration, ...]]) -> None:
    async def apply(conn: Any) -> None:
        await migrate_all(conn, components)

    database.run(apply, timeout=MIGRATION_TIMEOUT + 30)


class PostgresEvaluationStore:
    """Evaluation datasets, runs and imported traces in the shared Postgres ``Database``.

    ``database``: a ``nexura_postgres.Database`` (the service's shared pool); the store never
    closes it. Opening the store applies its migrations (component ``evaluations``).
    """

    blocking_io = True

    def __init__(self, database: Any, *, blob_payloads: Any = None, migrate: bool = True) -> None:
        self.database = database
        self._blob_payloads: Any = None
        if migrate:
            _migrate(
                database,
                {COMPONENT: MIGRATIONS, RETENTION_COMPONENT: RETENTION_MIGRATIONS},
            )
        self.blob_payloads = blob_payloads

    @property
    def blob_payloads(self) -> Any:
        return self._blob_payloads

    @blob_payloads.setter
    def blob_payloads(self, codec: Any) -> None:
        """Set the codec and register this database as one holding its references (WP2b)."""
        self._blob_payloads = codec
        if codec is not None and hasattr(codec, "add_binding"):
            codec.add_binding(PostgresBlobBinding(self.database))

    # -------------------------------------------------------------- datasets

    def save_dataset(self, dataset: EvaluationDataset, workspace_id: str = "default") -> None:
        examples = json.dumps([e.model_dump(mode="json") for e in dataset.examples])
        args = (
            _t(dataset.dataset_id),
            _t(dataset.version),
            _t(workspace_id),
            _t(dataset.description),
            examples,
            json.dumps(dataset.metadata),
            dataset.content_hash,
            dataset.created_at,
        )

        async def run(conn: Any) -> None:
            await conn.execute(
                """INSERT INTO eval_datasets
                (dataset_id, version, workspace_id, description, examples_json,
                 metadata_json, content_hash, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (dataset_id, version, workspace_id) DO UPDATE SET
                    description = EXCLUDED.description,
                    examples_json = EXCLUDED.examples_json,
                    metadata_json = EXCLUDED.metadata_json,
                    content_hash = EXCLUDED.content_hash,
                    created_at = EXCLUDED.created_at""",
                *args,
            )

        self.database.run(run)

    def get_dataset(
        self, dataset_id: str, version: str | None = None, workspace_id: str = "default"
    ) -> EvaluationDataset:
        builtin = create_synthetic_support_dataset()
        if dataset_id == builtin.dataset_id and (version is None or version == builtin.version):
            return builtin

        async def run(conn: Any) -> Any:
            if version:
                return await conn.fetchrow(
                    "SELECT * FROM eval_datasets WHERE dataset_id = $1 AND version = $2 "
                    "AND workspace_id = $3",
                    _t(dataset_id),
                    _t(version),
                    _t(workspace_id),
                )
            return await conn.fetchrow(
                "SELECT * FROM eval_datasets WHERE dataset_id = $1 AND workspace_id = $2 "
                "ORDER BY created_at DESC, seq DESC LIMIT 1",
                _t(dataset_id),
                _t(workspace_id),
            )

        row = self.database.run(run)
        if not row:
            raise DatasetNotFoundError(dataset_id, version)
        return EvaluationDataset(
            dataset_id=row["dataset_id"],
            version=row["version"],
            description=row["description"],
            created_at=row["created_at"],
            examples=[DatasetExample.model_validate(ex) for ex in json.loads(row["examples_json"])],
            metadata=json.loads(row["metadata_json"]),
            content_hash=row["content_hash"],
        )

    def list_datasets(self, workspace_id: str = "default") -> list[dict[str, Any]]:
        builtin = create_synthetic_support_dataset()
        results: list[dict[str, Any]] = [
            {
                "dataset_id": builtin.dataset_id,
                "version": builtin.version,
                "description": builtin.description,
                "example_count": len(builtin.examples),
                "content_hash": builtin.content_hash,
                "created_at": builtin.created_at,
                "is_builtin": True,
            }
        ]

        async def run(conn: Any) -> Any:
            return await conn.fetch(
                "SELECT * FROM eval_datasets WHERE workspace_id = $1 "
                "ORDER BY created_at DESC, seq DESC",
                _t(workspace_id),
            )

        for r in self.database.run(run):
            if r["dataset_id"] == builtin.dataset_id:
                continue
            results.append(
                {
                    "dataset_id": r["dataset_id"],
                    "version": r["version"],
                    "description": r["description"],
                    "example_count": len(json.loads(r["examples_json"])),
                    "content_hash": r["content_hash"],
                    "created_at": r["created_at"],
                    "is_builtin": False,
                }
            )
        return results

    def delete_dataset(self, dataset_id: str, workspace_id: str = "default") -> bool:
        if dataset_id == create_synthetic_support_dataset().dataset_id:
            return False

        async def run(conn: Any) -> bool:
            deleted = await conn.fetch(
                "DELETE FROM eval_datasets WHERE dataset_id = $1 AND workspace_id = $2 RETURNING 1",
                _t(dataset_id),
                _t(workspace_id),
            )
            return bool(deleted)

        return bool(self.database.run(run))

    # ------------------------------------------------------------------ runs

    def delete_run(self, run_id: str, workspace_id: str = "default") -> bool:
        """Delete one evaluation run (its summary and per-example results) of a workspace."""

        async def run(conn: Any) -> bool:
            deleted = await conn.fetch(
                "DELETE FROM eval_runs WHERE run_id = $1 AND workspace_id = $2 RETURNING 1",
                _t(run_id),
                _t(workspace_id),
            )
            return bool(deleted)

        return bool(self.database.run(run))

    def save_run(self, summary: EvaluationSummary, workspace_id: str = "default") -> None:
        args = (
            _t(summary.run_id),
            _t(workspace_id),
            _t(summary.dataset_id),
            _t(summary.dataset_version),
            _t_opt(summary.workflow_version),
            _t_opt(summary.spec_hash),
            summary.pass_rate,
            summary.mean_score,
            summary.total_examples,
            summary.passed_examples,
            summary.failed_examples,
            summary.total_latency_ms,
            str(summary.total_cost_usd),
            summary.timestamp,
            summary.model_dump_json(),
        )

        async def run(conn: Any) -> None:
            await conn.execute(
                """INSERT INTO eval_runs
                (run_id, workspace_id, dataset_id, dataset_version, workflow_version,
                 spec_hash, pass_rate, mean_score, total_examples, passed_examples,
                 failed_examples, total_latency_ms, total_cost_usd, created_at, summary_json)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
                ON CONFLICT (run_id) DO UPDATE SET summary_json = EXCLUDED.summary_json""",
                *args,
            )

        self.database.run(run)

    def get_run(self, run_id: str, workspace_id: str = "default") -> EvaluationSummary | None:
        async def run(conn: Any) -> Any:
            return await conn.fetchval(
                "SELECT summary_json FROM eval_runs WHERE run_id = $1 AND workspace_id = $2",
                _t(run_id),
                _t(workspace_id),
            )

        text = self.database.run(run)
        return None if text is None else EvaluationSummary.model_validate_json(text)

    def list_runs(
        self, workspace_id: str = "default", limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        async def run(conn: Any) -> Any:
            return await conn.fetch(
                """SELECT run_id, dataset_id, dataset_version, workflow_version, spec_hash,
                       pass_rate, mean_score, total_examples, passed_examples, failed_examples,
                       total_latency_ms, total_cost_usd, created_at
                FROM eval_runs WHERE workspace_id = $1
                ORDER BY created_at DESC, seq DESC LIMIT $2 OFFSET $3""",
                _t(workspace_id),
                limit,
                offset,
            )

        return [
            {
                "run_id": r["run_id"],
                "dataset_id": r["dataset_id"],
                "dataset_version": r["dataset_version"],
                "workflow_version": r["workflow_version"],
                "spec_hash": r["spec_hash"],
                "pass_rate": r["pass_rate"],
                "mean_score": r["mean_score"],
                "total_examples": r["total_examples"],
                "passed_examples": r["passed_examples"],
                "failed_examples": r["failed_examples"],
                "total_latency_ms": r["total_latency_ms"],
                "total_cost_usd": float(r["total_cost_usd"]),
                "created_at": r["created_at"],
            }
            for r in self.database.run(run)
        ]

    # ---------------------------------------------------------------- traces

    def save_trace(self, trace: StoredTrace, workspace_id: str = "default") -> None:
        """Store an imported recorded run snapshot without executing it."""
        text = trace.model_dump_json()
        codec = self.blob_payloads
        if codec is not None:  # a large trace goes to the blob store before the transaction
            text = codec.encode(workspace_id, EVAL_TRACE_NAMESPACE, text)

        async def run(conn: Any) -> None:
            await conn.execute(
                "INSERT INTO eval_traces (run_id, workspace_id, trace_json, created_at) "
                "VALUES ($1, $2, $3, $4) ON CONFLICT (run_id, workspace_id) DO UPDATE SET "
                "trace_json = EXCLUDED.trace_json, created_at = EXCLUDED.created_at",
                _t(trace.run_id),
                _t(workspace_id),
                text,
                trace.timestamp,
            )

        self.database.run(run)

    def prune(
        self,
        older_than_seconds: float,
        *,
        now: float | None = None,
        limit: int = DEFAULT_PRUNE_LIMIT,
        dry_run: bool = False,
    ) -> dict[str, int]:
        """Expire trace rows only, with the SQLite policy; never perform blob I/O."""
        cutoff = prune_cutoff(older_than_seconds, now, limit)
        # Limit each indexed age source before merging. Lock inside each branch so
        # locked rows do not consume its limit; the final merge deletes at most $2.
        # At most 2 * $2 trace rows are locked, all within this single statement.
        # Wrap the locking SELECTs: PostgreSQL forbids locking a UNION's direct inputs.
        lock = "" if dry_run else " FOR UPDATE OF t SKIP LOCKED"
        selection = (
            "SELECT run_id, workspace_id FROM (SELECT * FROM ("
            "SELECT t.run_id, t.workspace_id, r.created_at AS retention_time "
            "FROM eval_runs r JOIN eval_traces t "
            "ON t.run_id = r.run_id AND t.workspace_id = r.workspace_id "
            "WHERE r.created_at < $1 ORDER BY r.created_at, t.run_id, t.workspace_id "
            f"LIMIT $2{lock}) summary_candidates UNION ALL SELECT * FROM ("
            "SELECT t.run_id, t.workspace_id, t.created_at AS retention_time "
            "FROM eval_traces t WHERE t.created_at < $1 AND NOT EXISTS ("
            "SELECT 1 FROM eval_runs r "
            "WHERE r.run_id = t.run_id AND r.workspace_id = t.workspace_id) "
            f"ORDER BY t.created_at, t.run_id, t.workspace_id LIMIT $2{lock}) "
            "standalone_candidates) eligible "
            "ORDER BY retention_time, run_id, workspace_id LIMIT $2"
        )
        query = (
            f"SELECT count(*) FROM ({selection}) candidates"
            if dry_run
            else f"WITH candidates AS ({selection}), "
            "deleted AS (DELETE FROM eval_traces t USING candidates c "
            "WHERE t.run_id = c.run_id AND t.workspace_id = c.workspace_id RETURNING 1) "
            "SELECT count(*) FROM deleted"
        )

        async def run(conn: Any) -> int:
            return int(await conn.fetchval(query, cutoff, limit))

        return {"eval_traces": self.database.run(run)}

    def list_traces(self, workspace_id: str = "default") -> list[StoredTrace]:
        """Stored traces, by run id (byte order). One whose blob cannot be read is skipped and
        logged, integrity failures at ERROR (as on SQLite)."""

        async def run(conn: Any) -> Any:
            return await conn.fetch(
                "SELECT trace_json FROM eval_traces WHERE workspace_id = $1 "
                'ORDER BY run_id COLLATE "C"',
                _t(workspace_id),
            )

        rows = self.database.run(run)
        codec = self.blob_payloads
        if codec is None:
            return [StoredTrace.model_validate_json(row["trace_json"]) for row in rows]
        traces: list[StoredTrace] = []
        for row in rows:
            try:
                text = codec.decode(row["trace_json"], workspace_id)
            except BlobNotFoundError:
                logger.warning("A stored trace's payload blob is missing; skipping it")
                continue
            except BlobIntegrityError:
                logger.error(
                    "A stored trace failed its integrity check; skipping it", exc_info=True
                )
                continue
            except Exception:
                logger.warning("A stored trace could not be read; skipping it", exc_info=True)
                continue
            traces.append(StoredTrace.model_validate_json(text))
        return traces

    def close(self) -> None:
        """Nothing to release: the shared ``Database`` belongs to the app."""


class PostgresStudioEvalStore:
    """Studio eval datasets and run reports in the shared Postgres ``Database`` (component
    ``studio_evaluations``). Same API, errors and retention as ``StudioEvalStore``."""

    blocking_io = True

    def __init__(self, database: Any, *, migrate: bool = True) -> None:
        self.database = database
        if migrate:
            _migrate(database, {STUDIO_COMPONENT: STUDIO_MIGRATIONS})

    # ------------------------------------------------------------- datasets

    @staticmethod
    def _dataset(row: Any, *, with_cases: bool) -> dict[str, Any]:
        cases = json.loads(row["cases_json"])
        out: dict[str, Any] = {
            "dataset_id": row["dataset_id"],
            "name": row["name"],
            "description": row["description"],
            "revision": row["revision"],
            "content_hash": row["content_hash"],
            "case_count": len(cases),
            "workflow_id": row["workflow_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        if with_cases:
            out["cases"] = cases
        return out

    @staticmethod
    async def _fetch_dataset(conn: Any, workspace_id: str, dataset_id: str) -> Any:
        return await conn.fetchrow(
            "SELECT * FROM studio_eval_datasets WHERE workspace_id = $1 AND dataset_id = $2",
            _t(workspace_id),
            _t(dataset_id),
        )

    def get_dataset(self, workspace_id: str, dataset_id: str) -> dict[str, Any] | None:
        async def run(conn: Any) -> Any:
            return await self._fetch_dataset(conn, workspace_id, dataset_id)

        row = self.database.run(run)
        return self._dataset(row, with_cases=True) if row else None

    def list_datasets(
        self, workspace_id: str, workflow_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Datasets of the workspace (by id, byte order); only those bound to ``workflow_id``
        when it is given."""

        async def run(conn: Any) -> Any:
            if workflow_id is None:
                return await conn.fetch(
                    "SELECT * FROM studio_eval_datasets WHERE workspace_id = $1 "
                    'ORDER BY dataset_id COLLATE "C"',
                    _t(workspace_id),
                )
            return await conn.fetch(
                "SELECT * FROM studio_eval_datasets WHERE workspace_id = $1 "
                'AND workflow_id = $2 ORDER BY dataset_id COLLATE "C"',
                _t(workspace_id),
                _t(workflow_id),
            )

        return [self._dataset(r, with_cases=False) for r in self.database.run(run)]

    def count_datasets(self, workspace_id: str) -> int:
        async def run(conn: Any) -> Any:
            return await conn.fetchval(
                "SELECT COUNT(*) FROM studio_eval_datasets WHERE workspace_id = $1",
                _t(workspace_id),
            )

        return int(self.database.run(run))

    def save_dataset(
        self,
        workspace_id: str,
        dataset_id: str,
        *,
        name: str,
        description: str,
        cases: list[dict[str, Any]],
        content_hash: str,
        expected_revision: int | None,
        workflow_id: str | None = None,
    ) -> dict[str, Any]:
        """Create (revision 1) or replace (revision + 1). Raises ``ValueError('conflict:N')``.

        The read-check-write runs under the dataset's advisory lock, so it is a compare-and-set
        across instances.
        """
        ws, ds = _t(workspace_id), _t(dataset_id)
        cases_json = json.dumps(cases)
        now = time.time()

        async def run(conn: Any) -> Any:
            await conn.execute(DATASET_LOCK_SQL, ws, ds)
            row = await conn.fetchrow(
                "SELECT revision, created_at FROM studio_eval_datasets "
                "WHERE workspace_id = $1 AND dataset_id = $2",
                ws,
                ds,
            )
            if row is None:
                if expected_revision not in (None, 0):
                    raise ValueError("conflict:0")
                revision, created = 1, now
            else:
                if expected_revision is not None and expected_revision != row["revision"]:
                    raise ValueError(f"conflict:{row['revision']}")
                revision, created = int(row["revision"]) + 1, row["created_at"]
            await conn.execute(
                """INSERT INTO studio_eval_datasets
                (workspace_id, dataset_id, name, description, revision, content_hash,
                 cases_json, created_at, updated_at, workflow_id)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                ON CONFLICT (workspace_id, dataset_id) DO UPDATE SET
                    name = EXCLUDED.name, description = EXCLUDED.description,
                    revision = EXCLUDED.revision, content_hash = EXCLUDED.content_hash,
                    cases_json = EXCLUDED.cases_json, created_at = EXCLUDED.created_at,
                    updated_at = EXCLUDED.updated_at, workflow_id = EXCLUDED.workflow_id""",
                ws,
                ds,
                _t(name),
                _t(description),
                revision,
                content_hash,
                cases_json,
                created,
                now,
                _t_opt(workflow_id),
            )
            return await self._fetch_dataset(conn, workspace_id, dataset_id)

        row = self.database.run(run)
        return self._dataset(row, with_cases=True)

    def delete_dataset(self, workspace_id: str, dataset_id: str) -> bool:
        async def run(conn: Any) -> bool:
            deleted = await conn.fetch(
                "DELETE FROM studio_eval_datasets WHERE workspace_id = $1 AND dataset_id = $2 "
                "RETURNING 1",
                _t(workspace_id),
                _t(dataset_id),
            )
            return bool(deleted)

        return bool(self.database.run(run))

    # ----------------------------------------------------------------- runs

    @staticmethod
    def _run(row: Any, *, with_report: bool) -> dict[str, Any]:
        out: dict[str, Any] = {
            "run_id": row["run_id"],
            "dataset_id": row["dataset_id"],
            "dataset_revision": row["dataset_revision"],
            "dataset_hash": row["dataset_hash"],
            "label": row["label"],
            "target": json.loads(row["target_json"]),
            "scorers": json.loads(row["scorers_json"]),
            "summary": json.loads(row["summary_json"]),
            "created_at": row["created_at"],
        }
        if with_report:
            out["report"] = json.loads(row["report_json"])
        return out

    def save_run(
        self,
        workspace_id: str,
        *,
        run_id: str,
        dataset_id: str,
        dataset_revision: int,
        dataset_hash: str,
        label: str,
        target: dict[str, Any],
        scorers: list[dict[str, Any]],
        summary: dict[str, Any],
        report: dict[str, Any],
    ) -> dict[str, Any]:
        """Insert the run (a duplicate id raises) and keep only the newest
        ``MAX_RUNS_PER_WORKSPACE`` runs of the workspace, under the workspace's run lock."""
        ws = _t(workspace_id)
        args = (
            ws,
            _t(run_id),
            _t(dataset_id),
            dataset_revision,
            _t(dataset_hash),
            _t(label),
            json.dumps(target),
            json.dumps(scorers),
            json.dumps(summary),
            json.dumps(report),
        )

        async def run(conn: Any) -> Any:
            await conn.execute(RUNS_LOCK_SQL, ws)
            # The database clock, read under the lock: lock order is created_at order.
            await conn.execute(
                """INSERT INTO studio_eval_runs (workspace_id, run_id, dataset_id,
                dataset_revision, dataset_hash, label, target_json, scorers_json,
                summary_json, report_json, created_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10, EXTRACT(EPOCH FROM clock_timestamp()))""",
                *args,
            )
            # Read at call time, as the SQLite store reads its module constant.
            keep = studio_store.MAX_RUNS_PER_WORKSPACE
            await conn.execute(
                """DELETE FROM studio_eval_runs WHERE workspace_id = $1 AND seq NOT IN (
                    SELECT seq FROM studio_eval_runs WHERE workspace_id = $1
                    ORDER BY created_at DESC, seq DESC LIMIT $2)""",
                ws,
                keep,
            )
            return await conn.fetchrow(
                "SELECT * FROM studio_eval_runs WHERE workspace_id = $1 AND run_id = $2",
                ws,
                _t(run_id),
            )

        row = self.database.run(run)
        if row is None:  # pragma: no cover - only with a retention limit below 1
            raise LookupError(f"Studio eval run '{run_id}' was not kept")
        return self._run(row, with_report=True)

    def get_run(self, workspace_id: str, run_id: str) -> dict[str, Any] | None:
        async def run(conn: Any) -> Any:
            return await conn.fetchrow(
                "SELECT * FROM studio_eval_runs WHERE workspace_id = $1 AND run_id = $2",
                _t(workspace_id),
                _t(run_id),
            )

        row = self.database.run(run)
        return self._run(row, with_report=True) if row else None

    def list_runs(
        self, workspace_id: str, dataset_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        async def run(conn: Any) -> Any:
            if dataset_id is None:
                return await conn.fetch(
                    "SELECT * FROM studio_eval_runs WHERE workspace_id = $1 "
                    "ORDER BY created_at DESC, seq DESC LIMIT $2",
                    _t(workspace_id),
                    limit,
                )
            return await conn.fetch(
                "SELECT * FROM studio_eval_runs WHERE workspace_id = $1 AND dataset_id = $2 "
                "ORDER BY created_at DESC, seq DESC LIMIT $3",
                _t(workspace_id),
                _t(dataset_id),
                limit,
            )

        return [self._run(r, with_report=False) for r in self.database.run(run)]

    def delete_run(self, workspace_id: str, run_id: str) -> bool:
        async def run(conn: Any) -> bool:
            deleted = await conn.fetch(
                "DELETE FROM studio_eval_runs WHERE workspace_id = $1 AND run_id = $2 RETURNING 1",
                _t(workspace_id),
                _t(run_id),
            )
            return bool(deleted)

        return bool(self.database.run(run))

    def close(self) -> None:
        """Nothing to release: the shared ``Database`` belongs to the app."""


__all__ = [
    "COMPONENT",
    "MIGRATIONS",
    "STUDIO_COMPONENT",
    "STUDIO_MIGRATIONS",
    "PostgresEvaluationStore",
    "PostgresStudioEvalStore",
]
