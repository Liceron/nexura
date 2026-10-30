# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Per-workspace SQLite storage for Studio eval datasets and runs.

Every query is keyed by ``workspace_id``; a row of another workspace is indistinguishable
from a missing one. Stored reports are already secret-redacted by the caller.

A dataset may be bound to the workflow it tests (``workflow_id``, P22 "workflow tests"). The
column was added later: databases created before it get it with ``ALTER TABLE`` on open, and
their datasets read back with ``workflow_id`` = ``None``.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any

#: Eval runs kept per workspace; saving one more deletes the oldest.
MAX_RUNS_PER_WORKSPACE = 500


class StudioEvalStore:
    """Datasets (cases with expected output/trajectory) and run reports.

    ``nexura_postgres.PostgresStudioEvalStore`` is the Postgres twin (MVP WP8).
    """

    #: Local file I/O: async callers may run calls inline (Postgres: a network round trip).
    blocking_io = False

    def __init__(self, db_path: str = ":memory:") -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS studio_eval_datasets (
                    workspace_id TEXT NOT NULL,
                    dataset_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    cases_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    workflow_id TEXT,
                    PRIMARY KEY (workspace_id, dataset_id)
                );
                CREATE TABLE IF NOT EXISTS studio_eval_runs (
                    workspace_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    dataset_id TEXT NOT NULL,
                    dataset_revision INTEGER NOT NULL,
                    dataset_hash TEXT NOT NULL,
                    label TEXT NOT NULL,
                    target_json TEXT NOT NULL,
                    scorers_json TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    report_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (workspace_id, run_id)
                );
                CREATE INDEX IF NOT EXISTS idx_studio_eval_runs_ws
                    ON studio_eval_runs (workspace_id, created_at DESC);
                """
            )
            columns = {r[1] for r in self._conn.execute("PRAGMA table_info(studio_eval_datasets)")}
            if "workflow_id" not in columns:
                try:
                    self._conn.execute(
                        "ALTER TABLE studio_eval_datasets ADD COLUMN workflow_id TEXT"
                    )
                except sqlite3.OperationalError as exc:
                    # Another process opening the same database added it first.
                    if "duplicate column" not in str(exc).lower():
                        raise

    # ------------------------------------------------------------- datasets

    @staticmethod
    def _dataset(row: sqlite3.Row, *, with_cases: bool) -> dict[str, Any]:
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

    def get_dataset(self, workspace_id: str, dataset_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM studio_eval_datasets WHERE workspace_id=? AND dataset_id=?",
                (workspace_id, dataset_id),
            ).fetchone()
        return self._dataset(row, with_cases=True) if row else None

    def list_datasets(
        self, workspace_id: str, workflow_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Datasets of the workspace; only those bound to ``workflow_id`` when it is given."""
        sql = "SELECT * FROM studio_eval_datasets WHERE workspace_id=?"
        args: list[Any] = [workspace_id]
        if workflow_id is not None:
            sql += " AND workflow_id=?"
            args.append(workflow_id)
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY dataset_id", args).fetchall()
        return [self._dataset(r, with_cases=False) for r in rows]

    def count_datasets(self, workspace_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM studio_eval_datasets WHERE workspace_id=?", (workspace_id,)
            ).fetchone()
        return int(row[0])

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

        ``workflow_id`` is stored as given (``None`` = not bound); callers that want to keep an
        existing binding pass it back.
        """
        now = time.time()
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT revision, created_at FROM studio_eval_datasets "
                "WHERE workspace_id=? AND dataset_id=?",
                (workspace_id, dataset_id),
            ).fetchone()
            if row is None:
                if expected_revision not in (None, 0):
                    raise ValueError("conflict:0")
                revision, created = 1, now
            else:
                if expected_revision is not None and expected_revision != row["revision"]:
                    raise ValueError(f"conflict:{row['revision']}")
                revision, created = row["revision"] + 1, row["created_at"]
            self._conn.execute(
                "INSERT OR REPLACE INTO studio_eval_datasets "
                "(workspace_id, dataset_id, name, description, revision, content_hash, "
                "cases_json, created_at, updated_at, workflow_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    workspace_id,
                    dataset_id,
                    name,
                    description,
                    revision,
                    content_hash,
                    json.dumps(cases),
                    created,
                    now,
                    workflow_id,
                ),
            )
        found = self.get_dataset(workspace_id, dataset_id)
        assert found is not None
        return found

    def delete_dataset(self, workspace_id: str, dataset_id: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM studio_eval_datasets WHERE workspace_id=? AND dataset_id=?",
                (workspace_id, dataset_id),
            )
        return cur.rowcount > 0

    # ----------------------------------------------------------------- runs

    @staticmethod
    def _run(row: sqlite3.Row, *, with_report: bool) -> dict[str, Any]:
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
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO studio_eval_runs (workspace_id, run_id, dataset_id, "
                "dataset_revision, dataset_hash, label, target_json, scorers_json, "
                "summary_json, report_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    workspace_id,
                    run_id,
                    dataset_id,
                    dataset_revision,
                    dataset_hash,
                    label,
                    json.dumps(target),
                    json.dumps(scorers),
                    json.dumps(summary),
                    json.dumps(report),
                    time.time(),
                ),
            )
            # Retention: only the newest MAX_RUNS_PER_WORKSPACE runs of a workspace are kept.
            self._conn.execute(
                "DELETE FROM studio_eval_runs WHERE workspace_id=? AND run_id NOT IN ("
                "SELECT run_id FROM studio_eval_runs WHERE workspace_id=? "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?)",
                (workspace_id, workspace_id, MAX_RUNS_PER_WORKSPACE),
            )
        found = self.get_run(workspace_id, run_id)
        assert found is not None
        return found

    def get_run(self, workspace_id: str, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM studio_eval_runs WHERE workspace_id=? AND run_id=?",
                (workspace_id, run_id),
            ).fetchone()
        return self._run(row, with_report=True) if row else None

    def list_runs(
        self, workspace_id: str, dataset_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM studio_eval_runs WHERE workspace_id=?"
        args: list[Any] = [workspace_id]
        if dataset_id is not None:
            sql += " AND dataset_id=?"
            args.append(dataset_id)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [self._run(r, with_report=False) for r in rows]

    def delete_run(self, workspace_id: str, run_id: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM studio_eval_runs WHERE workspace_id=? AND run_id=?",
                (workspace_id, run_id),
            )
        return cur.rowcount > 0
