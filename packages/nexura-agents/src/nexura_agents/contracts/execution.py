# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Execution Contract v1 value types; these do not change any executor's behavior.

Only the standard library and the SDK error base are used. Journal transactions,
leases, dispatch, reconciliation and recovery are implemented in later A6 steps.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Literal

from nexura_agents.errors import NexuraError


class EffectClass(StrEnum):
    """Recovery promise for an effect; undeclared effects default to non_idempotent.

    Pure effects may repeat freely. Idempotent effects must reuse their key.
    Non-idempotent effects must never silently repeat after an uncertain dispatch.
    Reconcilable effects first probe for Applied, NotApplied or Unknown.
    """

    PURE = "pure"
    IDEMPOTENT = "idempotent"
    NON_IDEMPOTENT = "non_idempotent"
    RECONCILABLE = "reconcilable"


def effect_class_from_policy(policy: str | None = None) -> EffectClass:
    """Map manifest/runtime EffectPolicy strings without importing the runtime.

    pure/read map to pure, idempotent_write to idempotent, and
    non_idempotent_write (or an absent declaration) to non_idempotent.
    Unknown spellings raise ValueError rather than weakening the recovery promise.
    """
    if policy is None:
        return EffectClass.NON_IDEMPOTENT
    policies = {
        "pure": EffectClass.PURE,
        "read": EffectClass.PURE,
        "idempotent_write": EffectClass.IDEMPOTENT,
        "non_idempotent_write": EffectClass.NON_IDEMPOTENT,
    }
    try:
        return policies[policy]
    except KeyError:
        raise ValueError(f"Unknown EffectPolicy: {policy!r}") from None


def effect_class_from_idempotent(idempotent: bool = False) -> EffectClass:
    """Map @tool(idempotent=True) to idempotent; plain @tool is non_idempotent."""
    if not isinstance(idempotent, bool):
        raise TypeError("idempotent must be a bool")
    return EffectClass.IDEMPOTENT if idempotent else EffectClass.NON_IDEMPOTENT


def _nonnegative(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


@dataclass(frozen=True)
class ExecutionId:
    """Workspace-scoped execution identity; run_id is an opaque, nonempty string.

    Kind, origin, parent_run_id and definition_digest belong to the run record,
    not this identity pair. Trigger reclaim preserves this pair.
    """

    workspace_id: str
    run_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.workspace_id, str) or not self.workspace_id:
            raise ValueError("workspace_id must be a nonempty string")
        if not isinstance(self.run_id, str) or not self.run_id:
            raise ValueError("run_id must be a nonempty string")


_AGENT_KEY = re.compile(r"turn:(0|[1-9][0-9]*)/(?:model|call:.+)", re.DOTALL)
_WORKFLOW_KEY = re.compile(
    r".+#(0|[1-9][0-9]*)(?:\.([1-9][0-9]*))?(?:\[(0|[1-9][0-9]*)\])?", re.DOTALL
)


class StepKey(str):
    """Validated string identity for an agent turn or workflow graph visit.

    Agent keys are turn:<n>/call:<id> or turn:<n>/model. Workflow keys are
    <node>#<step>[.<attempt>][[<item>]], where the item uses one literal bracket
    pair, for example charge#3.2[0]. Attempts start at 1; attempt 1 is omitted
    by the workflow helper to preserve existing child run id conventions.
    As a str subclass this type serializes directly with stdlib json.
    """

    def __new__(cls, value: str) -> StepKey:
        if not isinstance(value, str) or not (
            _AGENT_KEY.fullmatch(value) or _WORKFLOW_KEY.fullmatch(value)
        ):
            raise ValueError(f"Invalid StepKey: {value!r}")
        return super().__new__(cls, value)

    @classmethod
    def parse(cls, value: str) -> StepKey:
        """Validate a persisted key without changing its spelling."""
        return cls(value)

    @classmethod
    def agent_tool(cls, turn: int, tool_call_id: str) -> StepKey:
        """Build turn:<turn>/call:<tool_call_id> for one tool dispatch."""
        _nonnegative(turn, "turn")
        if not isinstance(tool_call_id, str) or not tool_call_id:
            raise ValueError("tool_call_id must be a nonempty string")
        return cls(f"turn:{turn}/call:{tool_call_id}")

    @classmethod
    def agent_model(cls, turn: int) -> StepKey:
        """Build turn:<turn>/model for a model call (pure for execution)."""
        _nonnegative(turn, "turn")
        return cls(f"turn:{turn}/model")

    @classmethod
    def workflow(
        cls, node_id: str, step_count: int, attempt: int = 1, item: int | None = None
    ) -> StepKey:
        """Build a workflow visit key, with optional retry and map-item suffixes."""
        if not isinstance(node_id, str) or not node_id:
            raise ValueError("node_id must be a nonempty string")
        _nonnegative(step_count, "step_count")
        _nonnegative(attempt, "attempt")
        if attempt == 0:
            raise ValueError("attempt must be at least 1")
        value = f"{node_id}#{step_count}"
        if attempt > 1:
            value += f".{attempt}"
        if item is not None:
            _nonnegative(item, "item")
            value += f"[{item}]"
        return cls(value)


def derive_effect_id(
    execution_id: ExecutionId, step_key: StepKey | str, effect_index: int = 0
) -> str:
    """SHA-256 of [workspace_id, run_id, step_key, effect_index] as compact JSON.

    Hash bytes are UTF-8, with ensure_ascii=False and separators=(',', ':').
    Tuple encoding preserves boundaries even for opaque ids containing delimiters.
    Only these four identity inputs participate, so takeover preserves the digest.
    """
    _nonnegative(effect_index, "effect_index")
    key = StepKey(step_key)
    payload = json.dumps(
        [execution_id.workspace_id, execution_id.run_id, str(key), effect_index],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def default_idempotency_key(
    execution_id: ExecutionId, step_key: StepKey | str, effect_index: int = 0
) -> str:
    """Return 'nx-' plus the first 32 hex characters of the deterministic effect id."""
    return "nx-" + derive_effect_id(execution_id, step_key, effect_index)[:32]


@dataclass(frozen=True)
class StepIntent:
    """Pre-dispatch record for one non-pure effect, committed with state and cursor.

    The key is persisted and reused across takeover. A pure step needs no intent.
    approval_id and reservation_ids link exact approval and budget records.
    JSON objects decoded from dataclasses.asdict can be passed to the constructor.
    """

    step_key: StepKey
    payload_digest: str
    idempotency_key: str
    effect_class: EffectClass = EffectClass.NON_IDEMPOTENT
    approval_id: str | None = None
    reservation_ids: list[str] = field(default_factory=list)
    status: Literal["dispatched"] = "dispatched"

    def __post_init__(self) -> None:
        object.__setattr__(self, "step_key", StepKey(self.step_key))
        object.__setattr__(self, "effect_class", EffectClass(self.effect_class))
        if self.status != "dispatched":
            raise ValueError("StepIntent status must be dispatched")


@dataclass(frozen=True)
class StepOutcome:
    """Completed or failed step output, to be committed atomically with cursor advance.

    Output must be JSON-compatible when persisted; no lossy string conversion is
    performed here. This value type does not execute or commit the step.
    """

    step_key: StepKey
    status: Literal["completed", "failed"]
    output: Any = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "step_key", StepKey(self.step_key))
        if self.status not in ("completed", "failed"):
            raise ValueError("StepOutcome status must be completed or failed")


class EffectStatus(StrEnum):
    """Journal effect state: pending, confirmed, indeterminate or resolved."""

    PENDING = "pending"
    CONFIRMED = "confirmed"
    INDETERMINATE = "indeterminate"
    RESOLVED = "resolved"


class EffectResolution(StrEnum):
    """Machine-readable resolution: confirmed, not_applied or failed (operator fail)."""

    CONFIRMED = "confirmed"
    NOT_APPLIED = "not_applied"
    FAILED = "failed"


@dataclass(frozen=True)
class EffectRecord:
    """One journaled effect with its identity, intent and explicit resolution.

    Allowed states are pending, confirmed, indeterminate and resolved. Only a
    resolved record carries a resolution. Store code will enforce transitions,
    fencing and uniqueness of (workspace_id, intent.idempotency_key).
    effect_id is derived from identity, intent.step_key and effect_index.
    """

    execution_id: ExecutionId
    intent: StepIntent
    effect_index: int = 0
    status: EffectStatus = EffectStatus.PENDING
    resolution: EffectResolution | None = None
    result: Any = None
    resolved_by: str | None = None

    def __post_init__(self) -> None:
        # Restore nested dataclasses and enums after a stdlib JSON round trip.
        if isinstance(self.execution_id, dict):
            object.__setattr__(self, "execution_id", ExecutionId(**self.execution_id))
        if isinstance(self.intent, dict):
            object.__setattr__(self, "intent", StepIntent(**self.intent))
        _nonnegative(self.effect_index, "effect_index")
        object.__setattr__(self, "status", EffectStatus(self.status))
        if self.resolution is not None:
            object.__setattr__(self, "resolution", EffectResolution(self.resolution))
        if (self.status == EffectStatus.RESOLVED) != (self.resolution is not None):
            raise ValueError("Exactly resolved effects must carry a resolution")

    @property
    def effect_id(self) -> str:
        """Return the deterministic identifier for this effect."""
        return derive_effect_id(self.execution_id, self.intent.step_key, self.effect_index)


class ReconcileResult:
    """Result of probing an uncertain effect: Applied(result), NotApplied or Unknown.

    Instantiate one of the three dataclass variants. Unknown means the run stays
    paused with pause_reason='effect_unknown'; it never authorizes re-dispatch.
    """

    def to_dict(self) -> dict[str, Any]:
        """Serialize a concrete variant, including its JSON discriminator."""
        if isinstance(self, (Applied, NotApplied, Unknown)):
            return asdict(self)
        raise TypeError("Use Applied, NotApplied or Unknown")

    @staticmethod
    def from_dict(data: dict[str, Any]) -> ReconcileResult:
        """Restore the concrete variant from its status discriminator."""
        status = data.get("status")
        if status == "applied":
            return Applied(**data)
        if status == "not_applied":
            return NotApplied(**data)
        if status == "unknown":
            return Unknown(**data)
        raise ValueError(f"Unknown reconcile result status: {status!r}")


@dataclass(frozen=True)
class Applied(ReconcileResult):
    """The effect happened; record result as the step outcome and continue."""

    result: Any
    status: Literal["applied"] = "applied"

    def __post_init__(self) -> None:
        if self.status != "applied":
            raise ValueError("Applied status must be applied")


@dataclass(frozen=True)
class NotApplied(ReconcileResult):
    """The effect did not happen; retry with the same key and a fresh gated approval."""

    status: Literal["not_applied"] = "not_applied"

    def __post_init__(self) -> None:
        if self.status != "not_applied":
            raise ValueError("NotApplied status must be not_applied")


@dataclass(frozen=True)
class Unknown(ReconcileResult):
    """The effect cannot be determined; pause with pause_reason='effect_unknown'."""

    status: Literal["unknown"] = "unknown"

    def __post_init__(self) -> None:
        if self.status != "unknown":
            raise ValueError("Unknown status must be unknown")


class LeaseLostError(NexuraError):
    """A stale owner cannot write or delete after losing the run's fenced lease."""


class DefinitionMismatchError(NexuraError):
    """Takeover found a different definition_digest and requires explicit migration."""
