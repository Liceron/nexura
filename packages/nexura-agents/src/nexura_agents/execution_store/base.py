# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Backend-independent execution store protocol and journal invariants (stdlib only)."""

from __future__ import annotations

import copy
import json
import math
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import asdict, dataclass, replace
from typing import Any, Protocol, runtime_checkable

from nexura_agents.contracts.execution import (
    DefinitionMismatchError,
    EffectRecord,
    EffectResolution,
    EffectStatus,
    ExecutionId,
    LeaseLostError,
    StepIntent,
    StepOutcome,
)

from .models import (
    JOURNAL_SCHEMA_VERSION,
    STATE_VERSION,
    EffectAlreadyResolvedError,
    ExecutionConflictError,
    ExecutionEvent,
    ExecutionLease,
    ExecutionRun,
    ExecutionSnapshot,
    ExecutionStep,
    ExecutionVersionError,
)

_LEGACY_REMOVAL = (
    "durable_graph writes were removed (P35 A6 S12). Use "
    "nexura_runtime.workflow.WorkflowEngine for new workflows. Existing legacy rows "
    "are read-only through ExecutionStore.get; claim/renew/release and resolve_effect "
    "remain available to settle an indeterminate effect. See docs/durable-execution.md."
)


@runtime_checkable
class ExecutionStore(Protocol):
    """Atomic asynchronous journal API; implementations never dispatch external effects.

    Create requires an owner so the original run is leased from its first write.
    Busy claims return None. Missing runs raise KeyError. Conflicting journal
    changes raise ExecutionConflictError. Every mutation after create requires
    an unexpired owner/token pair, including operator effect resolution.
    """

    async def init_db(self) -> None:
        """Apply versioned migrations; reject databases from a newer release."""
        ...

    async def create(
        self,
        execution_id: ExecutionId,
        *,
        owner: str,
        definition_digest: str,
        state: dict[str, Any] | None = None,
        ttl: float = 30.0,
        kind: str = "agent",
        parent_run_id: str | None = None,
        origin: dict[str, Any] | None = None,
    ) -> ExecutionRun:
        """Atomically create a leased run; duplicate identity is an error."""
        ...

    async def get(self, execution_id: ExecutionId) -> ExecutionSnapshot:
        """Read a consistent detached snapshot; accept current and previous versions."""
        ...

    async def claim(
        self, execution_id: ExecutionId, owner: str, ttl: float, *, definition_digest: str
    ) -> ExecutionLease | None:
        """Claim a free/expired/own lease; every acquisition after expiry increments token."""
        ...

    async def renew(self, lease: ExecutionLease, ttl: float) -> ExecutionLease:
        """Renew active ownership; stale handles raise LeaseLostError."""
        ...

    async def release(self, lease: ExecutionLease) -> None:
        """Release current ownership without reducing its fencing token."""
        ...

    async def commit(
        self,
        lease: ExecutionLease,
        *,
        state: dict[str, Any],
        cursor: str | None = None,
        status: str = "running",
        steps: Sequence[StepIntent | StepOutcome] = (),
        effects: Sequence[EffectRecord] = (),
        events: Sequence[dict[str, Any]] = (),
        state_version: int = STATE_VERSION,
        expected_revision: int | None = None,
    ) -> ExecutionRun:
        """Commit state/cursor/steps/effects/events together; failures roll everything back."""
        ...

    async def list_recoverable(self, workspace_id: str | None = None) -> list[ExecutionRun]:
        """List running/interrupted runs with free or expired leases, using store time."""
        ...

    async def resolve_effect(
        self,
        lease: ExecutionLease,
        effect_id: str,
        resolution: EffectResolution,
        *,
        actor: str,
        result: Any = None,
        reason: str | None = None,
    ) -> EffectRecord:
        """Resolve an indeterminate effect once and audit the actor in the same transaction."""
        ...

    async def close(self) -> None:
        """Release owned backend resources."""
        ...


def _json(value: Any) -> str:
    # Strict payloads: no NaN, key coercion, arbitrary objects or lossy str fallback.
    def validate(item: Any) -> None:
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise TypeError("JSON object keys must be strings")
            for child in item.values():
                validate(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                validate(child)

    validate(value)
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _versions(run: ExecutionRun) -> None:
    if run.journal_schema_version not in (JOURNAL_SCHEMA_VERSION - 1, JOURNAL_SCHEMA_VERSION):
        raise ExecutionVersionError("Unsupported journal_schema_version")
    if run.state_version not in (STATE_VERSION - 1, STATE_VERSION):
        raise ExecutionVersionError("Unsupported state_version")


def _ttl(owner: str, ttl: float) -> None:
    if not isinstance(owner, str) or not owner:
        raise ValueError("owner must be nonempty")
    if isinstance(ttl, bool) or not math.isfinite(ttl) or ttl <= 0:
        raise ValueError("ttl must be positive and finite")


@dataclass
class _Transaction:
    snapshot: ExecutionSnapshot
    now: float

    def check(self, lease: ExecutionLease) -> None:
        run = self.snapshot.run
        if (
            run.execution_id != lease.execution_id
            or run.lease_owner != lease.owner
            or run.fencing_token != lease.fencing_token
            or run.lease_expires_at is None
            or run.lease_expires_at <= self.now
        ):
            raise LeaseLostError("Run lease is stale, expired or owned by another worker")

    def update(self, **changes: Any) -> ExecutionRun:
        run = replace(
            self.snapshot.run,
            **changes,
            revision=self.snapshot.run.revision + 1,
            updated_at=self.now,
        )
        self.snapshot = replace(self.snapshot, run=run)
        return run


class _Store:
    # Concrete backends supply transactions, insertion, and recovery enumeration.
    def _edit(
        self,
        execution_id: ExecutionId,
        *,
        step_keys: Sequence[str] | None = None,
        effect_ids: Sequence[str] | None = None,
    ) -> AbstractAsyncContextManager[_Transaction]:
        raise NotImplementedError

    async def _insert(self, run: ExecutionRun, ttl: float) -> ExecutionRun:
        raise NotImplementedError

    async def create(
        self,
        execution_id: ExecutionId,
        *,
        owner: str,
        definition_digest: str,
        state: dict[str, Any] | None = None,
        ttl: float = 30.0,
        kind: str = "agent",
        parent_run_id: str | None = None,
        origin: dict[str, Any] | None = None,
    ) -> ExecutionRun:
        """Create and lease a new run atomically, writing current versions only."""
        _ttl(owner, ttl)
        if not definition_digest:
            raise ValueError("definition_digest must be nonempty")
        if kind == "durable_graph":
            raise ExecutionConflictError(_LEGACY_REMOVAL)
        if kind not in ("agent", "workflow", "durable_graph"):
            raise ValueError("Unknown execution kind")
        values: dict[str, Any] = dict(
            execution_id=execution_id,
            definition_digest=definition_digest,
            state={} if state is None else state,
            kind=kind,
            parent_run_id=parent_run_id,
            origin={"source": "sdk"} if origin is None else origin,
            lease_owner=owner,
            fencing_token=1,
            revision=1,
        )
        run = ExecutionRun(**values)
        _json(asdict(run))
        return await self._insert(copy.deepcopy(run), ttl)

    async def get(self, execution_id: ExecutionId) -> ExecutionSnapshot:
        """Read detached state and journals in one consistent transaction."""
        async with self._edit(execution_id) as tx:
            return copy.deepcopy(tx.snapshot)

    async def claim(
        self, execution_id: ExecutionId, owner: str, ttl: float, *, definition_digest: str
    ) -> ExecutionLease | None:
        """Return None for a live competing owner; reject a definition mismatch first."""
        _ttl(owner, ttl)
        async with self._edit(execution_id, step_keys=(), effect_ids=()) as tx:
            run = tx.snapshot.run
            if run.definition_digest != definition_digest:
                raise DefinitionMismatchError("Execution definition_digest differs")
            if run.status in ("completed", "failed"):
                raise ExecutionConflictError("Terminal runs cannot be claimed")
            active = run.lease_expires_at is not None and run.lease_expires_at > tx.now
            if active and run.lease_owner not in (None, owner):
                return None
            token = run.fencing_token + (0 if active and run.lease_owner == owner else 1)
            return tx.update(
                lease_owner=owner, lease_expires_at=tx.now + ttl, fencing_token=token
            ).lease

    async def renew(self, lease: ExecutionLease, ttl: float) -> ExecutionLease:
        """Renew only an unexpired matching owner and fencing token."""
        _ttl(lease.owner, ttl)
        async with self._edit(lease.execution_id, step_keys=(), effect_ids=()) as tx:
            tx.check(lease)
            return tx.update(lease_expires_at=tx.now + ttl).lease

    async def release(self, lease: ExecutionLease) -> None:
        """Fenced release; an expired or replaced owner cannot release another lease."""
        async with self._edit(lease.execution_id, step_keys=(), effect_ids=()) as tx:
            tx.check(lease)
            tx.update(lease_owner=None, lease_expires_at=None)

    async def commit(
        self,
        lease: ExecutionLease,
        *,
        state: dict[str, Any],
        cursor: str | None = None,
        status: str = "running",
        steps: Sequence[StepIntent | StepOutcome] = (),
        effects: Sequence[EffectRecord] = (),
        events: Sequence[dict[str, Any]] = (),
        state_version: int = STATE_VERSION,
        expected_revision: int | None = None,
    ) -> ExecutionRun:
        """Atomically replace state/cursor and append immutable journal facts under a lease."""
        keys = {str(step.step_key) for step in steps}
        keys.update(str(effect.intent.step_key) for effect in effects)
        async with self._edit(
            lease.execution_id,
            step_keys=sorted(keys),
            effect_ids=[effect.effect_id for effect in effects],
        ) as tx:
            if tx.snapshot.run.kind == "durable_graph":
                raise ExecutionConflictError(_LEGACY_REMOVAL)
            tx.check(lease)
            if expected_revision is not None and tx.snapshot.run.revision != expected_revision:
                raise ExecutionConflictError("Execution revision changed")
            if state_version != STATE_VERSION:
                raise ExecutionVersionError("Writers must use the current state_version")
            if status not in ("running", "interrupted", "paused", "completed", "failed"):
                raise ValueError("Unknown run status")
            for step in steps:
                key = str(step.step_key)
                old = tx.snapshot.steps.get(key, ExecutionStep())
                if isinstance(step, StepIntent):
                    if old.intent is not None and old.intent != step:
                        raise ExecutionConflictError("Step intent is immutable")
                    if old.outcome is not None and old.intent is None:
                        raise ExecutionConflictError("Cannot dispatch a completed pure step")
                    tx.snapshot.steps[key] = replace(old, intent=copy.deepcopy(step))
                else:
                    if old.outcome is not None and old.outcome != step:
                        raise ExecutionConflictError("Step already has an outcome")
                    tx.snapshot.steps[key] = replace(old, outcome=copy.deepcopy(step))
            for effect in effects:
                if effect.execution_id != lease.execution_id:
                    raise ExecutionConflictError("Effect belongs to another execution")
                linked_step = tx.snapshot.steps.get(str(effect.intent.step_key))
                if linked_step is None or linked_step.intent != effect.intent:
                    raise ExecutionConflictError("Effect must link to the persisted step intent")
                old_effect = tx.snapshot.effects.get(effect.effect_id)
                if old_effect is None:
                    if effect.status != EffectStatus.PENDING:
                        raise ExecutionConflictError("New effects must be pending")
                elif old_effect != effect:
                    if old_effect.intent != effect.intent:
                        raise ExecutionConflictError("Effect intent is immutable")
                    allowed = {
                        EffectStatus.PENDING: (EffectStatus.CONFIRMED, EffectStatus.INDETERMINATE),
                        EffectStatus.INDETERMINATE: (),
                        EffectStatus.CONFIRMED: (),
                        EffectStatus.RESOLVED: (),
                    }
                    if effect.status not in allowed[old_effect.status]:
                        raise ExecutionConflictError(
                            "Invalid effect transition; use resolve_effect"
                        )
                if effect.resolution is not None or effect.resolved_by is not None:
                    raise ExecutionConflictError("Use resolve_effect to record operator resolution")
                tx.snapshot.effects[effect.effect_id] = copy.deepcopy(effect)
            for event in events:
                tx.snapshot.events.append(
                    ExecutionEvent(
                        (tx.snapshot.events[-1].sequence if tx.snapshot.events else 0) + 1,
                        str(event.get("type", "custom")),
                        copy.deepcopy(event),
                        tx.now,
                    )
                )
            updates: dict[str, Any] = dict(
                state=copy.deepcopy(state),
                cursor=cursor,
                status=status,
                journal_schema_version=JOURNAL_SCHEMA_VERSION,
                state_version=state_version,
            )
            if status in ("paused", "completed", "failed"):
                updates.update(lease_owner=None, lease_expires_at=None)
            run = tx.update(**updates)
            _json(asdict(tx.snapshot))
            return copy.deepcopy(run)

    async def resolve_effect(
        self,
        lease: ExecutionLease,
        effect_id: str,
        resolution: EffectResolution,
        *,
        actor: str,
        result: Any = None,
        reason: str | None = None,
    ) -> EffectRecord:
        """Resolve only indeterminate effects, once, recording actor and an audit event."""
        async with self._edit(lease.execution_id, step_keys=(), effect_ids=[effect_id]) as tx:
            tx.check(lease)
            if not isinstance(actor, str) or not actor:
                raise ValueError("actor must be nonempty")
            if reason is not None and not isinstance(reason, str):
                raise ValueError("reason must be a string")
            resolution = EffectResolution(resolution)
            old = tx.snapshot.effects[effect_id]
            if old.status == EffectStatus.RESOLVED:
                raise EffectAlreadyResolvedError("Effect is already resolved")
            if old.status != EffectStatus.INDETERMINATE:
                raise ExecutionConflictError("Only indeterminate effects may be resolved")
            effect = replace(
                old,
                status=EffectStatus.RESOLVED,
                resolution=resolution,
                result=copy.deepcopy(result),
                resolved_by=actor,
            )
            tx.snapshot.effects[effect_id] = effect
            tx.snapshot.events.append(
                ExecutionEvent(
                    (tx.snapshot.events[-1].sequence if tx.snapshot.events else 0) + 1,
                    "effect_resolved",
                    {
                        "type": "effect_resolved",
                        "effect_id": effect_id,
                        "resolution": resolution.value,
                        "actor": actor,
                        **({"reason": reason} if reason is not None else {}),
                    },
                    tx.now,
                )
            )
            changes = (
                {"lease_owner": None, "lease_expires_at": None}
                if tx.snapshot.run.status == "paused"
                else {}
            )
            tx.update(**changes)
            _json(asdict(tx.snapshot))
            return copy.deepcopy(effect)


def _recoverable(run: ExecutionRun, now: float, workspace_id: str | None) -> bool:
    return (
        run.status in ("running", "interrupted")
        and (workspace_id is None or workspace_id == run.execution_id.workspace_id)
        and (run.lease_owner is None or run.lease_expires_at is None or run.lease_expires_at <= now)
    )
