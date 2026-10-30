# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Public snapshots, lease handles and errors for the execution journal."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from nexura_agents.contracts.execution import EffectRecord, ExecutionId, StepIntent, StepOutcome
from nexura_agents.errors import NexuraError

JOURNAL_SCHEMA_VERSION = 1
STATE_VERSION = 2


class ExecutionConflictError(NexuraError):
    """Creation, immutable journal data or an idempotency key conflicts with stored data."""


class EffectAlreadyResolvedError(ExecutionConflictError):
    """An operator tried to resolve an effect more than once."""


class ExecutionVersionError(NexuraError):
    """A journal, migration or engine state version cannot be read or written safely."""


@dataclass(frozen=True)
class ExecutionLease:
    """Fenced authority for one run; expiry is always checked using the store clock."""

    execution_id: ExecutionId
    owner: str
    fencing_token: int
    expires_at: float


@dataclass(frozen=True)
class ExecutionRun:
    """Detached run snapshot, including versions, cursor, identity and lease metadata."""

    execution_id: ExecutionId
    definition_digest: str
    state: dict[str, Any]
    kind: Literal["agent", "workflow", "durable_graph"] = "agent"
    parent_run_id: str | None = None
    origin: dict[str, Any] = field(default_factory=lambda: {"source": "sdk"})
    status: Literal["running", "interrupted", "paused", "completed", "failed"] = "running"
    cursor: str | None = None
    journal_schema_version: int = JOURNAL_SCHEMA_VERSION
    state_version: int = STATE_VERSION
    lease_owner: str | None = None
    lease_expires_at: float | None = None
    fencing_token: int = 0
    revision: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0

    @property
    def lease(self) -> ExecutionLease:
        """Return the stored lease handle, or raise ValueError if this run is unleased."""
        if self.lease_owner is None or self.lease_expires_at is None:
            raise ValueError("Run has no lease")
        return ExecutionLease(
            self.execution_id, self.lease_owner, self.fencing_token, self.lease_expires_at
        )


@dataclass(frozen=True)
class ExecutionStep:
    """At most one immutable intent and one immutable outcome per step key."""

    intent: StepIntent | None = None
    outcome: StepOutcome | None = None


@dataclass(frozen=True)
class ExecutionEvent:
    """Append-only event, sequenced within a run by its fenced transaction."""

    sequence: int
    event_type: str
    payload: dict[str, Any]
    timestamp: float


@dataclass(frozen=True)
class ExecutionSnapshot:
    """Consistent detached snapshot of run, steps, effects and ordered events."""

    run: ExecutionRun
    steps: dict[str, ExecutionStep] = field(default_factory=dict)
    effects: dict[str, EffectRecord] = field(default_factory=dict)
    events: list[ExecutionEvent] = field(default_factory=list)
