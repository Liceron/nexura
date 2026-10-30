# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Opt-in capture of failed service runs as portable repro bundles.

Off by default. Turn it on with ``create_api_app(capture_failed_runs=True)``,
``nexura-runtime serve --capture-failed-runs`` or ``NEXURA_CAPTURE_FAILED_RUNS=1``.
While it is on, every workflow run started through the service (``POST /api/v1/runs``
and the trigger worker) is executed under :class:`nexura_agents.repro.ReproRecorder`.
Approval decisions made in the same process continue the recording. When the run's
stored status ends ``failed`` the recording is exported with the usual redaction
(:func:`nexura_agents.repro.export_repro_bundle`, plus the workspace's known credential
values) and kept in the service database. Download it with
``GET /api/v1/runs/{run_id}/repro`` or ``nexura-runtime repro export RUN_ID --db ...``.

Not captured: runs continued after a restart (the in-memory recording is gone), runs
paused for ``interrupt()`` input (answers are not part of a bundle), forks, and agent
chat. Those runs simply have no bundle.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from collections import OrderedDict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from nexura_agents.repro import ReproRecorder, export_repro_bundle

__all__ = [
    "CAPTURE_ENV",
    "FailedRunCapture",
    "capture_failed_runs_enabled",
    "capture_recorder",
    "load_captured_bundle",
    "settle_failed_run_capture",
]

logger = logging.getLogger(__name__)

CAPTURE_ENV = "NEXURA_CAPTURE_FAILED_RUNS"
TABLE = "nexura_service_repro_bundles"
# Recordings of runs paused for approval stay in memory until decided; bound them.
MAX_OPEN_RECORDINGS = 256


def capture_failed_runs_enabled(explicit: bool | None = None) -> bool:
    """The explicit setting wins; otherwise ``NEXURA_CAPTURE_FAILED_RUNS`` (default off)."""
    if explicit is not None:
        return bool(explicit)
    return os.environ.get(CAPTURE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {TABLE} ("
        "run_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, "
        "created_at REAL NOT NULL, bundle_json TEXT NOT NULL)"
    )
    return conn


class FailedRunCapture:
    """Recordings of in-flight service runs and the bundles of runs that failed."""

    def __init__(self, db_path: str | Path | None) -> None:
        path = str(db_path) if db_path is not None else None
        self.db_path = path if path and path != ":memory:" else None
        self._recorders: OrderedDict[str, ReproRecorder] = OrderedDict()
        self._memory: dict[str, tuple[str, dict[str, Any]]] = {}
        if self.db_path is not None:
            _connect(self.db_path).close()

    def start(self, run_id: str) -> ReproRecorder:
        """A fresh recorder for a new run."""
        recorder = ReproRecorder()
        self._recorders[run_id] = recorder
        while len(self._recorders) > MAX_OPEN_RECORDINGS:
            dropped, _ = self._recorders.popitem(last=False)
            logger.warning("Repro capture dropped the recording of paused run %s", dropped)
        return recorder

    def recorder(self, run_id: str) -> ReproRecorder | None:
        return self._recorders.get(run_id)

    def discard(self, run_id: str) -> None:
        self._recorders.pop(run_id, None)

    async def settle(
        self,
        run_id: str,
        workspace_id: str,
        status: str | None,
        credential_values: Iterable[str] = (),
    ) -> bool:
        """Call once the run's status is stored. Writes a bundle when it is ``failed``.

        A run paused for approval keeps its recording for the decision; any other
        status ends it. Returns True when a bundle was written.
        """
        recorder = self._recorders.get(run_id)
        if recorder is None or status == "pending_approval":
            return False
        self._recorders.pop(run_id, None)
        if status != "failed" or not recorder.data:
            return False
        known = {f"credential_{i}": value for i, value in enumerate(credential_values) if value}
        try:
            data = export_repro_bundle(recorder, credential_values=known).data
        except (ValueError, TypeError, KeyError, RecursionError) as exc:
            # A recording that cannot be exported (for example a non-JSON node output)
            # must never turn into a second failure of the run.
            logger.warning("Repro capture of failed run %s was not written: %s", run_id, exc)
            return False
        if self.db_path is None:
            self._memory[run_id] = (workspace_id, data)
            return True
        await asyncio.to_thread(self._write, run_id, workspace_id, data)
        return True

    def _write(self, run_id: str, workspace_id: str, data: dict[str, Any]) -> None:
        assert self.db_path is not None
        conn = _connect(self.db_path)
        try:
            with conn:
                conn.execute(
                    f"INSERT OR REPLACE INTO {TABLE} "
                    "(run_id, workspace_id, created_at, bundle_json) VALUES (?, ?, ?, ?)",
                    (run_id, workspace_id, time.time(), json.dumps(data, ensure_ascii=False)),
                )
        finally:
            conn.close()

    def load(self, run_id: str) -> tuple[str, dict[str, Any]] | None:
        """``(workspace_id, bundle data)`` for a captured run, or None."""
        if run_id in self._memory:
            return self._memory[run_id]
        if self.db_path is None:
            return None
        return load_captured_bundle(self.db_path, run_id)


def load_captured_bundle(db_path: str | Path, run_id: str) -> tuple[str, dict[str, Any]] | None:
    """Read a captured bundle from a service database without creating anything."""
    if not Path(db_path).is_file():
        return None
    conn = sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True, timeout=30.0)
    try:
        row = conn.execute(
            f"SELECT workspace_id, bundle_json FROM {TABLE} WHERE run_id = ?", (run_id,)
        ).fetchone()
    except sqlite3.OperationalError:
        return None  # capture was never enabled for this database
    finally:
        conn.close()
    return None if row is None else (str(row[0]), json.loads(row[1]))


def capture_recorder(app_state: Any, run_id: str, *, start: bool = False) -> ReproRecorder | None:
    """The recorder for a run when capture is on (a new one with ``start=True``)."""
    capture: FailedRunCapture | None = getattr(app_state, "repro_capture", None)
    if capture is None:
        return None
    return capture.start(run_id) if start else capture.recorder(run_id)


async def settle_failed_run_capture(app_state: Any, run_id: str, workspace_id: str) -> None:
    """Write the bundle of a run whose stored status is ``failed`` (no-op when off)."""
    capture: FailedRunCapture | None = getattr(app_state, "repro_capture", None)
    if capture is None or capture.recorder(run_id) is None:
        return
    try:
        status = await app_state.store.get_run_status(run_id)
        credentials = getattr(app_state, "credential_store", None)
        secrets = credentials._redaction_values(workspace_id) if credentials else []
        await capture.settle(run_id, workspace_id, status, secrets)
    except Exception:  # capture is diagnostics: never fail the request because of it
        logger.exception("Repro capture of run %s failed", run_id)
        capture.discard(run_id)
