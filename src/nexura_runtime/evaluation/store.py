# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Persistent SQLite and in-memory store for evaluation datasets, runs, and comparison reports."""

from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any, Protocol

from nexura_runtime.evaluation.dataset import create_synthetic_support_dataset
from nexura_runtime.evaluation.errors import DatasetNotFoundError
from nexura_runtime.evaluation.models import (
    DatasetExample,
    EvaluationDataset,
    EvaluationSummary,
)


class EvaluationStore(Protocol):
    """Storage protocol for datasets, evaluation runs, and comparisons."""

    def save_dataset(self, dataset: EvaluationDataset, workspace_id: str) -> None: ...
    def get_dataset(
        self, dataset_id: str, version: str | None = None, workspace_id: str = "default"
    ) -> EvaluationDataset: ...
    def list_datasets(self, workspace_id: str = "default") -> list[dict[str, Any]]: ...
    def delete_dataset(self, dataset_id: str, workspace_id: str = "default") -> bool: ...

    def save_run(self, summary: EvaluationSummary, workspace_id: str) -> None: ...
    def get_run(self, run_id: str, workspace_id: str = "default") -> EvaluationSummary | None: ...
    def list_runs(
        self, workspace_id: str = "default", limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]: ...


class SqliteEvaluationStore:
    """Crash-resilient SQLite storage for datasets and evaluation runs."""

    def __init__(self, db_path: str = ":memory:") -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS eval_datasets (
                    dataset_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    description TEXT NOT NULL,
                    examples_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (dataset_id, version, workspace_id)
                );

                CREATE TABLE IF NOT EXISTS eval_runs (
                    run_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    dataset_id TEXT NOT NULL,
                    dataset_version TEXT NOT NULL,
                    workflow_version TEXT,
                    spec_hash TEXT,
                    pass_rate REAL NOT NULL,
                    mean_score REAL NOT NULL,
                    total_examples INTEGER NOT NULL,
                    passed_examples INTEGER NOT NULL,
                    failed_examples INTEGER NOT NULL,
                    total_latency_ms REAL NOT NULL,
                    total_cost_usd TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    summary_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_eval_runs_ws
                    ON eval_runs (workspace_id, created_at DESC);
            """)

    def save_dataset(self, dataset: EvaluationDataset, workspace_id: str = "default") -> None:
        with self._lock, self._conn:
            examples_raw = [e.model_dump(mode="json") for e in dataset.examples]
            self._conn.execute(
                """
                INSERT INTO eval_datasets
                (dataset_id, version, workspace_id, description, examples_json,
                 metadata_json, content_hash, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(dataset_id, version, workspace_id) DO UPDATE SET
                    description = excluded.description,
                    examples_json = excluded.examples_json,
                    metadata_json = excluded.metadata_json,
                    content_hash = excluded.content_hash,
                    created_at = excluded.created_at
                """,
                (
                    dataset.dataset_id,
                    dataset.version,
                    workspace_id,
                    dataset.description,
                    json.dumps(examples_raw),
                    json.dumps(dataset.metadata),
                    dataset.content_hash,
                    dataset.created_at,
                ),
            )

    def get_dataset(
        self, dataset_id: str, version: str | None = None, workspace_id: str = "default"
    ) -> EvaluationDataset:
        # Check built-in fixtures first
        builtin = create_synthetic_support_dataset()
        if dataset_id == builtin.dataset_id and (version is None or version == builtin.version):
            return builtin

        with self._lock:
            if version:
                cur = self._conn.execute(
                    "SELECT * FROM eval_datasets WHERE dataset_id = ? AND version = ? AND "
                    "workspace_id = ?",
                    (dataset_id, version, workspace_id),
                )
            else:
                cur = self._conn.execute(
                    "SELECT * FROM eval_datasets WHERE dataset_id = ? AND workspace_id = ? ORDER "
                    "BY created_at DESC LIMIT 1",
                    (dataset_id, workspace_id),
                )
            row = cur.fetchone()
            if not row:
                raise DatasetNotFoundError(dataset_id, version)

            examples_raw = json.loads(row["examples_json"])
            examples = [DatasetExample.model_validate(ex) for ex in examples_raw]
            return EvaluationDataset(
                dataset_id=row["dataset_id"],
                version=row["version"],
                description=row["description"],
                created_at=row["created_at"],
                examples=examples,
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

        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM eval_datasets WHERE workspace_id = ? ORDER BY created_at DESC",
                (workspace_id,),
            )
            rows = cur.fetchall()
            for r in rows:
                if r["dataset_id"] == builtin.dataset_id:
                    continue
                ex_count = len(json.loads(r["examples_json"]))
                results.append(
                    {
                        "dataset_id": r["dataset_id"],
                        "version": r["version"],
                        "description": r["description"],
                        "example_count": ex_count,
                        "content_hash": r["content_hash"],
                        "created_at": r["created_at"],
                        "is_builtin": False,
                    }
                )
        return results

    def delete_dataset(self, dataset_id: str, workspace_id: str = "default") -> bool:
        builtin = create_synthetic_support_dataset()
        if dataset_id == builtin.dataset_id:
            return False

        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM eval_datasets WHERE dataset_id = ? AND workspace_id = ?",
                (dataset_id, workspace_id),
            )
            return cur.rowcount > 0

    def save_run(self, summary: EvaluationSummary, workspace_id: str = "default") -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO eval_runs
                (run_id, workspace_id, dataset_id, dataset_version, workflow_version,
                 spec_hash, pass_rate, mean_score, total_examples, passed_examples,
                 failed_examples, total_latency_ms, total_cost_usd, created_at, summary_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    summary_json = excluded.summary_json
                """,
                (
                    summary.run_id,
                    workspace_id,
                    summary.dataset_id,
                    summary.dataset_version,
                    summary.workflow_version,
                    summary.spec_hash,
                    summary.pass_rate,
                    summary.mean_score,
                    summary.total_examples,
                    summary.passed_examples,
                    summary.failed_examples,
                    summary.total_latency_ms,
                    str(summary.total_cost_usd),
                    summary.timestamp,
                    summary.model_dump_json(),
                ),
            )

    def get_run(self, run_id: str, workspace_id: str = "default") -> EvaluationSummary | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT summary_json FROM eval_runs WHERE run_id = ? AND workspace_id = ?",
                (run_id, workspace_id),
            )
            row = cur.fetchone()
            if not row:
                return None
            return EvaluationSummary.model_validate_json(row["summary_json"])

    def list_runs(
        self, workspace_id: str = "default", limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT run_id, dataset_id, dataset_version, workflow_version, spec_hash,
                       pass_rate, mean_score, total_examples, passed_examples, failed_examples,
                       total_latency_ms, total_cost_usd, created_at
                FROM eval_runs
                WHERE workspace_id = ?
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                (workspace_id, limit, offset),
            )
            rows = cur.fetchall()
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
                for r in rows
            ]
