# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Durable effect tracking and explicit reconciliation engine."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Literal

from nexura_agents.contracts.canonical import canonical_digest, canonical_json

from nexura_runtime.runtime.effects.errors import (
    EffectError,
    ReconciliationError,
)
from nexura_runtime.runtime.effects.models import EffectRecord
from nexura_runtime.storage.migrations import run_sqlite_migrations


class EffectReconciler:
    """Tracks external mutations, ensuring lost responses remain indeterminate until reconciled."""

    def __init__(self, db_path_or_conn: str | Path | sqlite3.Connection = ":memory:") -> None:
        if isinstance(db_path_or_conn, (str, Path)):
            self.db_path = str(db_path_or_conn)
            self._shared_conn: sqlite3.Connection | None = None
            conn = self._get_connection()
            try:
                run_sqlite_migrations(conn)
            finally:
                conn.close()
        else:
            self.db_path = ":memory:"
            self._shared_conn = db_path_or_conn
            run_sqlite_migrations(self._shared_conn)

    def _get_connection(self) -> sqlite3.Connection:
        if self._shared_conn is not None:
            return self._shared_conn
        conn = sqlite3.connect(
            self.db_path,
            timeout=30.0,
            autocommit=True,
        )
        conn.row_factory = sqlite3.Row
        return conn

    def record_intent(
        self,
        workspace_id: str,
        effect_id: str,
        run_id: str,
        action_name: str,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> EffectRecord:
        """Record pre-dispatch intent in durable storage before executing side effects."""
        conn = self._get_connection()
        try:
            digest = canonical_digest(payload)
            now = time.time()
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO effect_records (
                    workspace_id, effect_id, run_id, action_name,
                    idempotency_key, payload_digest, status, dispatched_at,
                    response_json, reconciliation_notes, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    workspace_id,
                    effect_id,
                    run_id,
                    action_name,
                    idempotency_key,
                    digest,
                    "pending",
                    now,
                    None,
                    None,
                    now,
                    now,
                ),
            )
            conn.execute("COMMIT")
            return EffectRecord(
                workspace_id=workspace_id,
                effect_id=effect_id,
                run_id=run_id,
                action_name=action_name,
                idempotency_key=idempotency_key,
                payload_digest=digest,
                status="pending",
                dispatched_at=now,
                created_at=now,
                updated_at=now,
            )
        except Exception:
            with contextlib.suppress(Exception):
                conn.execute("ROLLBACK")
            raise
        finally:
            if self._shared_conn is None:
                conn.close()

    def record_confirmed(
        self,
        workspace_id: str,
        effect_id: str,
        response_payload: dict[str, Any],
    ) -> EffectRecord:
        """Mark an effect as successfully confirmed by the remote system."""
        conn = self._get_connection()
        try:
            now = time.time()
            resp_json = canonical_json(response_payload)
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE effect_records
                SET status = 'confirmed', response_json = ?, updated_at = ?
                WHERE workspace_id = ? AND effect_id = ?
                """,
                (resp_json, now, workspace_id, effect_id),
            )
            conn.execute("COMMIT")
            eff = self.get_effect(workspace_id, effect_id)
            if not eff:
                raise EffectError(f"Effect '{effect_id}' not found after update.")
            return eff
        except Exception:
            with contextlib.suppress(Exception):
                conn.execute("ROLLBACK")
            raise
        finally:
            if self._shared_conn is None:
                conn.close()

    def record_indeterminate(
        self,
        workspace_id: str,
        effect_id: str,
        reason: str = "Connection lost during dispatch",
    ) -> EffectRecord:
        """Mark an effect as indeterminate when response is lost or transport dropped."""
        conn = self._get_connection()
        try:
            now = time.time()
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE effect_records
                SET status = 'indeterminate', reconciliation_notes = ?, updated_at = ?
                WHERE workspace_id = ? AND effect_id = ?
                """,
                (f"INDETERMINATE: {reason}", now, workspace_id, effect_id),
            )
            conn.execute("COMMIT")
            eff = self.get_effect(workspace_id, effect_id)
            if not eff:
                raise EffectError(f"Effect '{effect_id}' not found.")
            return eff
        except Exception:
            with contextlib.suppress(Exception):
                conn.execute("ROLLBACK")
            raise
        finally:
            if self._shared_conn is None:
                conn.close()

    def get_effect(self, workspace_id: str, effect_id: str) -> EffectRecord | None:
        """Retrieve an effect record by workspace and effect ID."""
        conn = self._get_connection()
        try:
            cur = conn.execute(
                """
                SELECT * FROM effect_records
                WHERE workspace_id = ? AND effect_id = ?
                """,
                (workspace_id, effect_id),
            )
            row = cur.fetchone()
            if not row:
                return None
            resp = json.loads(row["response_json"]) if row["response_json"] else None
            return EffectRecord(
                workspace_id=row["workspace_id"],
                effect_id=row["effect_id"],
                run_id=row["run_id"],
                action_name=row["action_name"],
                idempotency_key=row["idempotency_key"],
                payload_digest=row["payload_digest"],
                status=row["status"],
                dispatched_at=row["dispatched_at"],
                response_payload=resp,
                reconciliation_notes=row["reconciliation_notes"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
        finally:
            if self._shared_conn is None:
                conn.close()

    def reconcile_effect(
        self,
        workspace_id: str,
        effect_id: str,
        resolution: Literal["confirmed", "rolled_back", "voided"],
        notes: str,
    ) -> EffectRecord:
        """Explicitly reconcile an indeterminate effect with audit notes."""
        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            eff = self.get_effect(workspace_id, effect_id)
            if not eff:
                raise EffectError(f"Effect '{effect_id}' not found in workspace '{workspace_id}'.")

            if eff.status != "indeterminate":
                raise ReconciliationError(
                    f"Effect '{effect_id}' has status '{eff.status}'; only 'indeterminate' "
                    f"effects require manual reconciliation.",
                    effect_id=effect_id,
                    current_status=eff.status,
                )

            now = time.time()
            new_notes = f"RECONCILED [{resolution.upper()}]: {notes}"
            conn.execute(
                """
                UPDATE effect_records
                SET status = 'reconciled', reconciliation_notes = ?, updated_at = ?
                WHERE workspace_id = ? AND effect_id = ?
                """,
                (new_notes, now, workspace_id, effect_id),
            )
            conn.execute("COMMIT")
            eff.status = "reconciled"
            eff.reconciliation_notes = new_notes
            eff.updated_at = now
            return eff
        except Exception:
            with contextlib.suppress(Exception):
                conn.execute("ROLLBACK")
            raise
        finally:
            if self._shared_conn is None:
                conn.close()
