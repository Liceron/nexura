# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Audit records of policy decisions, and where they go.

One ``AuditRecord`` per decision: the run, agent, stage, tool, rule, check, action and the
plain classification fields (categories, score, latency, source). Never the checked text,
tool arguments or results. Only decisions other than ``allow`` are written unless the policy
sets ``audit_allows=True``.

A sink failure is logged and counted (``GuardrailPolicy.audit_errors``); with
``audit_required=True`` it fails the run instead (fail closed).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "AuditRecord",
    "AuditSink",
    "CallbackAuditSink",
    "JsonlAuditSink",
    "MemoryAuditSink",
]


@dataclass(frozen=True)
class AuditRecord:
    """One policy decision, without content."""

    stage: str
    action: str
    rule_id: str | None = None
    check: str | None = None
    backend: str | None = None
    decision: str | None = None
    categories: tuple[str, ...] = ()
    score: float | None = None
    latency_ms: float | None = None
    cached: bool = False
    source: str = "live"
    error_type: str | None = None
    run_id: str | None = None
    workspace_id: str | None = None
    agent: str | None = None
    tool: str | None = None
    tool_call_id: str | None = None
    policy: str | None = None
    policy_digest: str | None = None
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """A JSON-safe dict without ``None`` values."""
        data = dataclasses.asdict(self)
        data["categories"] = list(self.categories)
        return {key: value for key, value in data.items() if value is not None}


@runtime_checkable
class AuditSink(Protocol):
    """Receives every audited decision (``async def write(record)``)."""

    async def write(self, record: AuditRecord) -> None:
        """Store ``record``; raise to report a failure."""
        ...


class MemoryAuditSink:
    """Keeps records in a list (tests, notebooks)."""

    def __init__(self) -> None:
        self.records: list[AuditRecord] = []

    async def write(self, record: AuditRecord) -> None:
        """Append ``record``."""
        self.records.append(record)


class CallbackAuditSink:
    """Calls ``callback(record)`` (sync or async) for every record."""

    def __init__(self, callback: Callable[[AuditRecord], Awaitable[None] | None]) -> None:
        if not callable(callback):
            raise TypeError("callback must be callable.")
        self.callback = callback

    async def write(self, record: AuditRecord) -> None:
        """Call the callback (awaited when it returns an awaitable)."""
        result = self.callback(record)
        if isinstance(result, Awaitable):
            await result


class JsonlAuditSink:
    """Appends one JSON line per record to ``path`` (created owner-only, ``0600``).

    Lines of ``block`` and ``fail`` decisions are flushed to disk (``fsync``) before the
    decision takes effect; others are written but not synced. Relative paths are taken as
    given (the policy loader resolves them against the policy file's directory).
    """

    def __init__(self, path: str | Path, *, fsync_actions: tuple[str, ...] = ("block", "fail")):
        self.path = Path(path)
        self.fsync_actions = frozenset(fsync_actions)
        self._lock = threading.Lock()

    def _append(self, line: str, sync: bool) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, line.encode("utf-8"))
                if sync:
                    os.fsync(fd)
            finally:
                os.close(fd)

    async def write(self, record: AuditRecord) -> None:
        """Append ``record`` as one line (in a worker thread)."""
        line = json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True) + "\n"
        await asyncio.to_thread(self._append, line, record.action in self.fsync_actions)

    def __repr__(self) -> str:
        return f"JsonlAuditSink({str(self.path)!r})"
