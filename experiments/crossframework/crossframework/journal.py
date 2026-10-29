"""One durable dispatch slot per business request, in the approval database."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

UNKNOWN = {"status": "outcome_unknown", "executed": False}


class DispatchJournal:
    def __init__(self, path: Path) -> None:
        self.path = path
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS crossframework_dispatches (
                    request_id TEXT PRIMARY KEY,
                    payload_digest TEXT NOT NULL,
                    phase TEXT NOT NULL CHECK (phase IN ('ready', 'dispatching', 'completed')),
                    outcome_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )"""
            )

    def _connect(self) -> sqlite3.Connection:
        # Each operation uses an independent connection; BEGIN IMMEDIATE fences processes.
        return sqlite3.connect(self.path, timeout=30)

    def reserve(self, request_id: str, payload_digest: str) -> dict[str, Any] | None:
        """Return a stored outcome, or reserve dispatch before evaluating/claiming a gate.

        A dead owner leaves dispatching forever: absence of a receipt never permits replay.
        Pending/blocked/rejected outcomes are stored as ready, so the gate can be rechecked.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT payload_digest, phase, outcome_json FROM crossframework_dispatches "
                "WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row is not None:
                if row[0] != payload_digest:
                    # Report what the id already did: a different refund may have paid out.
                    return {
                        "status": "request_id_conflict",
                        "executed": False,
                        "prior_phase": row[1],
                    }
                if row[1] != "ready":
                    outcome = json.loads(row[2])
                    if not isinstance(outcome, dict):
                        raise ValueError("Dispatch journal outcome must be an object")
                    return outcome
                conn.execute(
                    "UPDATE crossframework_dispatches SET phase = 'dispatching', "
                    "outcome_json = ?, updated_at = ? WHERE request_id = ?",
                    (json.dumps(UNKNOWN), time.time(), request_id),
                )
            else:
                conn.execute(
                    "INSERT INTO crossframework_dispatches VALUES (?, ?, 'dispatching', ?, ?)",
                    (request_id, payload_digest, json.dumps(UNKNOWN), time.time()),
                )
            conn.commit()
            return None
        finally:
            conn.close()

    def finish(self, request_id: str, outcome: dict[str, Any]) -> None:
        phase = "completed" if outcome["executed"] else "ready"
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "UPDATE crossframework_dispatches SET phase = ?, outcome_json = ?, "
                    "updated_at = ? WHERE request_id = ?",
                    (phase, json.dumps(outcome), time.time(), request_id),
                )
        finally:
            conn.close()
