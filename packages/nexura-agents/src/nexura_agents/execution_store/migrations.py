# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Versioned execution-journal DDL shared by SQLite and Postgres.

The new journal has its own migration ledger, so opening an SDK store cannot
migrate or alter the runtime's older runs/approvals/session tables. All statements
are run inside a backend transaction, never sqlite executescript's implicit commit.
"""

from __future__ import annotations

MIGRATIONS: tuple[tuple[int, str, tuple[str, ...]], ...] = (
    (
        1,
        "execution_contract_v1",
        (
            """CREATE TABLE nexura_exec_runs (
            workspace_id TEXT NOT NULL, run_id TEXT NOT NULL,
            definition_digest TEXT NOT NULL,
            journal_schema_version INTEGER NOT NULL, state_version INTEGER NOT NULL,
            status TEXT NOT NULL, lease_owner TEXT, lease_expires_at DOUBLE PRECISION,
            fencing_token BIGINT NOT NULL, revision BIGINT NOT NULL,
            run_json TEXT NOT NULL,
            PRIMARY KEY (workspace_id, run_id)
        )""",
            """CREATE TABLE nexura_exec_steps (
            workspace_id TEXT NOT NULL, run_id TEXT NOT NULL, step_key TEXT NOT NULL,
            intent_json TEXT, outcome_json TEXT,
            PRIMARY KEY (workspace_id, run_id, step_key),
            FOREIGN KEY (workspace_id, run_id) REFERENCES nexura_exec_runs
                (workspace_id, run_id)
        )""",
            """CREATE TABLE nexura_exec_effects (
            workspace_id TEXT NOT NULL, run_id TEXT NOT NULL, effect_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL, status TEXT NOT NULL,
            resolution TEXT, resolved_by TEXT, record_json TEXT NOT NULL,
            PRIMARY KEY (workspace_id, run_id, effect_id),
            UNIQUE (workspace_id, idempotency_key),
            CHECK (status IN ('pending', 'confirmed', 'indeterminate', 'resolved')),
            CHECK ((status = 'resolved' AND resolution IS NOT NULL) OR
                   (status <> 'resolved' AND resolution IS NULL)),
            CHECK (resolution IS NULL OR resolution IN ('confirmed', 'not_applied', 'failed')),
            FOREIGN KEY (workspace_id, run_id) REFERENCES nexura_exec_runs
                (workspace_id, run_id)
        )""",
            """CREATE TABLE nexura_exec_events (
            workspace_id TEXT NOT NULL, run_id TEXT NOT NULL, sequence BIGINT NOT NULL,
            event_type TEXT NOT NULL, payload_json TEXT NOT NULL,
            timestamp DOUBLE PRECISION NOT NULL,
            PRIMARY KEY (workspace_id, run_id, sequence),
            FOREIGN KEY (workspace_id, run_id) REFERENCES nexura_exec_runs
                (workspace_id, run_id)
        )""",
            "CREATE INDEX nexura_exec_recovery ON nexura_exec_runs (status, lease_expires_at)",
        ),
    ),
)
