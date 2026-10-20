# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""In-memory execution journal with atomic copy-on-write transactions."""

from __future__ import annotations

import copy
import threading
import time
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace

from nexura_agents.contracts.execution import ExecutionId

from .base import _recoverable, _Store, _Transaction, _versions
from .models import ExecutionConflictError, ExecutionRun, ExecutionSnapshot


class MemoryExecutionStore(_Store):
    """Process-local journal; a locked snapshot is published only on successful commit.

    The optional clock is the store's clock, useful for deterministic tests. This
    backend is ephemeral and cannot coordinate processes or survive a restart.
    """

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._runs: dict[ExecutionId, ExecutionSnapshot] = {}

    async def init_db(self) -> None:
        """No schema initialization is needed for memory storage."""

    async def _insert(self, run: ExecutionRun, ttl: float) -> ExecutionRun:
        with self._lock:
            if run.execution_id in self._runs:
                raise ExecutionConflictError("Execution already exists")
            now = self._clock()
            run = replace(run, created_at=now, updated_at=now, lease_expires_at=now + ttl)
            self._runs[run.execution_id] = ExecutionSnapshot(copy.deepcopy(run))
            return copy.deepcopy(run)

    @asynccontextmanager
    async def _edit(
        self,
        execution_id: ExecutionId,
        *,
        step_keys: Sequence[str] | None = None,
        effect_ids: Sequence[str] | None = None,
    ) -> AsyncIterator[_Transaction]:
        with self._lock:
            snapshot = copy.deepcopy(self._runs[execution_id])
            _versions(snapshot.run)
            tx = _Transaction(snapshot, self._clock())
            yield tx
            keys: set[tuple[str, str]] = set()
            for identity, current in self._runs.items():
                candidate = tx.snapshot if identity == execution_id else current
                for effect in candidate.effects.values():
                    key = (identity.workspace_id, effect.intent.idempotency_key)
                    if key in keys:
                        raise ExecutionConflictError("Idempotency key already exists in workspace")
                    keys.add(key)
            self._runs[execution_id] = copy.deepcopy(tx.snapshot)

    async def list_recoverable(self, workspace_id: str | None = None) -> list[ExecutionRun]:
        """Return expired/free running runs according to the memory store clock."""
        with self._lock:
            runs = [
                snapshot.run
                for snapshot in self._runs.values()
                if _recoverable(snapshot.run, self._clock(), workspace_id)
            ]
            for run in runs:
                _versions(run)
            return copy.deepcopy(
                sorted(
                    runs, key=lambda run: (run.execution_id.workspace_id, run.execution_id.run_id)
                )
            )

    async def close(self) -> None:
        """No external resources are held; existing snapshots remain available."""
