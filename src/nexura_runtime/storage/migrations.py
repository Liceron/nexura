# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Versioned schema migrations for SQLite and PostgreSQL profiles."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    up_sql: str


MIGRATIONS: list[Migration] = [
    Migration(
        version=1,
        name="001_legacy_tables",
        up_sql="""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            data TEXT NOT NULL,
            revision INTEGER NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS workflow_checkpoints (
            workflow_run_id TEXT PRIMARY KEY,
            workflow_spec_hash TEXT NOT NULL,
            current_node TEXT NOT NULL,
            state_json TEXT NOT NULL,
            step_count INTEGER NOT NULL,
            status TEXT NOT NULL,
            revision INTEGER NOT NULL,
            pending_approval_json TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        """,
    ),
    Migration(
        version=2,
        name="002_unified_schema",
        up_sql="""
        CREATE TABLE IF NOT EXISTS workspaces (
            workspace_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS unified_sessions (
            workspace_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            data TEXT NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (workspace_id, session_id)
        );
        CREATE TABLE IF NOT EXISTS runs (
            workspace_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            status TEXT NOT NULL,
            revision INTEGER NOT NULL,
            input_json TEXT NOT NULL,
            state_json TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (workspace_id, run_id)
        );
        CREATE TABLE IF NOT EXISTS run_events (
            workspace_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            timestamp REAL NOT NULL,
            PRIMARY KEY (workspace_id, run_id, seq)
        );
        """,
    ),
    Migration(
        version=3,
        name="003_approvals_and_effects",
        up_sql="""
        CREATE TABLE IF NOT EXISTS approval_requests (
            workspace_id TEXT NOT NULL,
            approval_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            node_id TEXT,
            attempt_id TEXT NOT NULL,
            action_kind TEXT NOT NULL,
            required_role TEXT NOT NULL,
            action_payload_json TEXT NOT NULL,
            payload_digest TEXT NOT NULL,
            spec_digest TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            status TEXT NOT NULL,
            claimed_by TEXT,
            claimed_at REAL,
            expires_at REAL NOT NULL,
            decision_json TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (workspace_id, approval_id)
        );
        CREATE TABLE IF NOT EXISTS effect_records (
            workspace_id TEXT NOT NULL,
            effect_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            action_name TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            payload_digest TEXT NOT NULL,
            status TEXT NOT NULL,
            dispatched_at REAL NOT NULL,
            response_json TEXT,
            reconciliation_notes TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (workspace_id, effect_id)
        );
        """,
    ),
    Migration(
        version=4,
        name="004_long_term_memory",
        up_sql="""
        CREATE TABLE IF NOT EXISTS long_term_facts (
            workspace_id TEXT NOT NULL,
            principal_id TEXT NOT NULL,
            fact_id TEXT NOT NULL,
            fact_key TEXT NOT NULL,
            value_json TEXT NOT NULL,
            category TEXT NOT NULL,
            source_session_id TEXT,
            source_message_index INTEGER,
            confidence REAL NOT NULL,
            is_deleted INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (workspace_id, fact_id)
        );
        CREATE INDEX IF NOT EXISTS idx_facts_workspace_principal
            ON long_term_facts (workspace_id, principal_id, is_deleted);
        """,
    ),
    Migration(
        version=5,
        name="005_approval_dispatch_identity",
        # Columns are added in Python (see _add_missing_columns) so a second process that
        # races this migration does not fail on a duplicate column. Existing rows get an
        # empty identity_digest: they cannot prove which action they approved, so the
        # dispatch claim refuses them (ApprovalIdentityMissingError) instead of trusting them.
        up_sql="",
    ),
]

#: Columns added by migration 5 to ``approval_requests``: (name, DDL type and default).
_APPROVAL_IDENTITY_COLUMNS: tuple[tuple[str, str], ...] = (
    ("action_name", "TEXT NOT NULL DEFAULT ''"),
    ("identity_digest", "TEXT NOT NULL DEFAULT ''"),
)


def _add_missing_columns(
    conn: sqlite3.Connection, table: str, columns: tuple[tuple[str, str], ...]
) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, ddl in columns:
        if name in existing:
            continue
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise


def run_sqlite_migrations(conn: sqlite3.Connection) -> int:
    """Apply all pending migrations to an SQLite database within a transaction.

    Returns the number of migrations applied.
    """
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at REAL NOT NULL
            )
        """)

        cursor = conn.execute("SELECT version FROM schema_migrations")
        applied_versions = {row[0] for row in cursor.fetchall()}

        applied_count = 0
        for m in MIGRATIONS:
            if m.version not in applied_versions:
                if m.up_sql.strip():
                    conn.executescript(m.up_sql)

                if m.version == 5:
                    _add_missing_columns(conn, "approval_requests", _APPROVAL_IDENTITY_COLUMNS)

                # If applying migration 2, migrate legacy sessions into unified_sessions
                if m.version == 2:
                    # Check for nexura_sessions
                    cur = conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name='nexura_sessions'"
                    )

                    if cur.fetchone():
                        conn.execute("""
                            INSERT OR IGNORE INTO unified_sessions (
                                workspace_id, session_id, revision, data, updated_at
                            )
                            SELECT 'default', session_id, revision, data, updated_at
                            FROM nexura_sessions
                        """)

                    # Check for sessions
                    cur = conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'"
                    )
                    if cur.fetchone():
                        conn.execute("""
                            INSERT OR IGNORE INTO unified_sessions (
                                workspace_id, session_id, revision, data, updated_at
                            )
                            SELECT 'default', session_id, revision, data, updated_at
                            FROM sessions
                        """)

                    # Check for nexura_workflow_checkpoints
                    cur = conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND "
                        "name='nexura_workflow_checkpoints'"
                    )
                    if cur.fetchone():
                        conn.execute("""
                            INSERT OR IGNORE INTO runs (
                                workspace_id, run_id, status, revision, input_json, state_json,
                                metadata_json, created_at, updated_at
                            )
                            SELECT 'default', workflow_run_id, status, 1, '{}', state_json,
                                   '{}', updated_at, updated_at
                            FROM nexura_workflow_checkpoints
                        """)

                conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations (version, name, applied_at) "
                    "VALUES (?, ?, ?)",
                    (m.version, m.name, time.time()),
                )
                applied_count += 1

    return applied_count
