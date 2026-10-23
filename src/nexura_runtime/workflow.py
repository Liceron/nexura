# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Executable workflow specification, module registry, and approval engines.

Provides deterministic graph execution, state transitions, human-in-the-loop
approval gates, durable SQLite and memory checkpointing, and cycle bounds.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import copy
import dataclasses
import functools
import hashlib
import importlib
import inspect
import json
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NoReturn, Protocol, runtime_checkable

from nexura_agents import tracing
from nexura_agents.contracts.canonical import canonical_digest, canonical_json
from nexura_agents.contracts.execution import (
    Applied,
    EffectClass,
    EffectResolution,
    EffectStatus,
    ExecutionId,
    LeaseLostError,
    NotApplied,
    StepIntent,
    StepKey,
    Unknown,
    default_idempotency_key,
    effect_class_from_policy,
)
from nexura_agents.errors import (
    ApprovalAlreadyClaimedError,
    ApprovalError,
    UnsupportedWorkflowVersionError,
    WorkflowConflictError,
    WorkflowError,
    WorkflowExecutionError,
    WorkflowMaxStepsError,
    WorkflowSpecMismatchError,
    WorkflowTerminalError,
    WorkflowTimeoutError,
    WorkflowValidationError,
)
from nexura_agents.events import RunEvent, RunEventType
from nexura_agents.usage import linkage as _linkage

from nexura_runtime._workflow_journal import TERMINAL as _JOURNAL_TERMINAL
from nexura_runtime._workflow_journal import WorkflowJournal, journal_capable

if TYPE_CHECKING:
    from nexura_agents.contracts.base import BaseModule
    from nexura_agents.contracts.execution import EffectRecord
    from nexura_agents.execution_store import ExecutionStore
from nexura_runtime.runtime.approvals.errors import (
    ApprovalExpiredError,
    MutatedArgumentsError,
    WrongActorError,
    WrongWorkspaceError,
)

WorkflowStatus = Literal["running", "paused", "completed", "failed", "rejected"]

# Set while WorkflowEngine.stream() runs; node progress is reported here. A context variable
# so concurrent runs on one engine, parallel branches and sub-workflows each see the right sink.
_PROGRESS: contextvars.ContextVar[Callable[[RunEventType, dict[str, Any]], None] | None] = (
    contextvars.ContextVar("nexura_workflow_progress", default=None)
)


def _progress(kind: RunEventType, **data: Any) -> None:
    sink = _PROGRESS.get()
    if sink is not None:
        sink(kind, data)


class WorkflowInterrupt(BaseException):  # noqa: N818 - control flow, like CancelledError
    """Raised by :func:`interrupt` to pause a node; the engine handles it.

    Derives from ``BaseException`` so a handler's ``except Exception`` does not swallow it.
    """

    def __init__(self, value: Any) -> None:
        super().__init__("Workflow interrupted for human input.")
        self.value = value


@dataclass
class _InterruptState:
    answers: list[Any]
    index: int = 0
    # Set where a pause cannot be resumed (parallel branches, mapped sub-workflows).
    blocked: str | None = None


_NO_PAUSE_IN_PARALLEL = (
    "interrupt() cannot be used inside parallel branches or map_over items, because "
    "they cannot be resumed one by one. Ask before or after the parallel step."
)


_INTERRUPT_STATE: contextvars.ContextVar[_InterruptState | None] = contextvars.ContextVar(
    "nexura_workflow_interrupt", default=None
)


# True only while the engine runs a node whose approval a human granted (set per node step).
_NODE_APPROVED: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "nexura_workflow_node_approved", default=False
)


def interrupt(value: Any = None) -> Any:
    """Pause the workflow inside a node and ask a human for input.

    The first time, the run stops with ``status="paused"`` and ``result.interrupt`` holds
    ``value`` (a question, a draft to review...). After
    ``engine.resume_interrupt(run_id, interrupt_id, answer)`` the node runs again from
    the start and this call returns ``answer``. A node may call ``interrupt`` several
    times; earlier answers are replayed in order. Code before the call runs again on
    resume, so keep side effects after it.
    """
    state = _INTERRUPT_STATE.get()
    if state is None:
        raise WorkflowError("interrupt() can only be called inside a running workflow node.")
    if state.blocked:
        raise WorkflowValidationError(state.blocked)
    if state.index < len(state.answers):
        answer = state.answers[state.index]
        state.index += 1
        return answer
    try:
        json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise WorkflowValidationError(
            f"interrupt() value must be JSON-serializable: {exc}"
        ) from exc
    raise WorkflowInterrupt(value)


#: Largest ``request_approval`` details (JSON characters) a node may attach to one approval.
MAX_NODE_APPROVAL_DETAILS_CHARS = 512_000


class _NodeApprovalRequested(WorkflowInterrupt):  # noqa: N818 - control flow
    """Raised by :func:`request_approval`; the engine pauses the run on the approval path."""

    def __init__(self, details: dict[str, Any]) -> None:
        super().__init__(None)
        self.details = details


# Set by ``resume`` when an approval is granted: the ``nexura.approval.decided`` event for the
# workflow span that continues the run (opt-in tracing). Consumed once.
_TRACE_DECISION: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "nexura_workflow_trace_decision", default=None
)
# Set by ``resume`` for the node it continues; the first node step consumes it.
_RESUME_GRANT: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "nexura_workflow_resume_grant", default=None
)
# Set by ``resume`` when an edit re-gates the node: the fresh approval request records
# which request it replaces and the inputs that request showed. Consumed once.
_REGATE_PROVENANCE: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "nexura_workflow_regate_provenance", default=None
)
# The ``request_approval`` details a human approved for the node step now running.
_NODE_GRANT: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "nexura_workflow_node_grant", default=None
)


def request_approval(details: dict[str, Any]) -> NoReturn:
    """Pause the running node until a human approves or rejects ``details``.

    Unlike :func:`interrupt`, this uses the approval path: the run stops with
    ``status="paused"`` and a ``pending_approval`` whose ``details`` are ``details`` plus
    ``"node_request": True``. It is decided like any approval gate
    (``engine.resume(run_id, approval_id, decision=...)``, or the service's
    ``POST /runs/{id}/decide``) with the same workspace, role and expiry rules. On approve
    the node runs again from its start and :func:`approved_request` returns the details; on
    reject the run ends ``rejected`` and the node does not run again. Code before the call
    runs again on resume, so keep side effects after it. ``details`` must be JSON and at
    most ``MAX_NODE_APPROVAL_DETAILS_CHARS`` characters; not allowed in parallel or mapped
    steps.
    """
    state = _INTERRUPT_STATE.get()
    if state is None:
        raise WorkflowError("request_approval() can only be called inside a running workflow node.")
    if state.blocked:
        raise WorkflowValidationError(state.blocked)
    if not isinstance(details, dict):
        raise WorkflowValidationError("request_approval() details must be a JSON object.")
    try:
        size = len(json.dumps(details, ensure_ascii=False))
    except (TypeError, ValueError) as exc:
        raise WorkflowValidationError(
            f"request_approval() details must be JSON-serializable: {exc}"
        ) from exc
    if size > MAX_NODE_APPROVAL_DETAILS_CHARS:
        raise WorkflowValidationError(
            f"request_approval() details are {size} characters; the limit is "
            f"{MAX_NODE_APPROVAL_DETAILS_CHARS}."
        )
    raise _NodeApprovalRequested({**copy.deepcopy(details), "node_request": True})


def approved_request() -> dict[str, Any] | None:
    """The details a human approved for this node with :func:`request_approval`, else None.

    Only the node step that runs right after the approval sees them; later nodes, other
    runs and a fresh run of the same node get ``None``.
    """
    granted = _NODE_GRANT.get()
    return copy.deepcopy(granted) if granted is not None else None


@dataclass(frozen=True)
class _StepIdentity:
    """The journal step a handler call belongs to (Execution Contract v1)."""

    execution_id: ExecutionId
    step_key: StepKey
    fencing_token: int | None = None
    # The approval the step runs under (its parts' intents carry it too).
    approval_id: str | None = None

    def idempotency_key(self, index: int = 0) -> str:
        return default_idempotency_key(self.execution_id, self.step_key, index)

    def for_item(self, index: int) -> _StepIdentity:
        """A mapped item's own step (``node#step[.attempt][<index>]``)."""
        return dataclasses.replace(self, step_key=StepKey(f"{self.step_key}[{index}]"))

    def for_branch(self, name: str) -> _StepIdentity:
        """A parallel branch's own step (``node/branch#step[.attempt]``)."""
        node, _, rest = str(self.step_key).rpartition("#")
        return dataclasses.replace(self, step_key=StepKey(f"{node}/{name}#{rest}"))


_VISIT_KEY = re.compile(r"^(.*)#(\d+)(?:\.(\d+))?$", re.DOTALL)


def _part_family(container: StepKey, item: int | None, branch: str | None) -> tuple[Any, int]:
    """Key family of a composite step's part, and the attempt its keys start at.

    A mapped item of ``node#5`` is ``node#5[2]``, a parallel branch ``node/b#5``; a part a
    person resolved as ``not_applied`` is dispatched again as the next attempt
    (``node#5.2[2]``, ``node/b#5.2``). Parts start at the attempt of their step, so the keys
    of a step's first dispatch are the ones S6 handed out.
    """
    match = _VISIT_KEY.match(str(container))
    if match is None:  # pragma: no cover - container keys always come from StepKey.workflow
        raise WorkflowError(f"Unexpected step key '{container}'.")
    node_id, step, attempt = match.group(1), int(match.group(2)), int(match.group(3) or 1)
    if branch is not None:
        return (lambda a: StepKey.workflow(f"{node_id}/{branch}", step, a)), attempt
    return (lambda a: StepKey.workflow(node_id, step, a, item)), attempt


@dataclass
class _UncertainPart:
    """A branch, item or sub-workflow child whose outcome is unknown after a crash.

    Returned (not raised) by a part so the other parts of the step still finish and record
    their outcomes; the step then pauses on the first one (``effect_unknown``).
    """

    pending: dict[str, Any]
    keys: list[str] = field(default_factory=list)
    failed: str | None = None


class _PartUncertain(WorkflowInterrupt):  # noqa: N818 - control flow
    """A part of a composite step is in doubt (or was resolved as failed)."""

    def __init__(self, parts: list[_UncertainPart]) -> None:
        super().__init__(None)
        self.parts = parts

    @property
    def failed(self) -> str | None:
        return next((p.failed for p in self.parts if p.failed), None)

    @property
    def pending(self) -> dict[str, Any]:
        return next(p.pending for p in self.parts if not p.failed)

    @property
    def keys(self) -> list[str]:
        return [key for part in self.parts for key in part.keys]


def _raise_uncertain(results: list[Any]) -> list[dict[str, Any]]:
    """The parts' updates; raises ``_PartUncertain`` when any part is in doubt."""
    doubtful = [r for r in results if isinstance(r, _UncertainPart)]
    if doubtful:
        raise _PartUncertain(doubtful)
    return results


class _ChildLeaseBusy(BaseException):  # noqa: N818 - control flow
    """A sub-workflow child's run lease is still live: recover the parent later."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class _LeaseBusyError(WorkflowError):
    """``recover()``: another live worker holds the run lease."""


# recover(on_uncertain=...) for the run being recovered and its sub-workflow children.
_ON_UNCERTAIN: contextvars.ContextVar[str] = contextvars.ContextVar(
    "nexura_workflow_on_uncertain", default="pause"
)


# The step the current handler call belongs to; set by the engine around each node.
_STEP_IDENTITY: contextvars.ContextVar[_StepIdentity | None] = contextvars.ContextVar(
    "nexura_workflow_step_identity", default=None
)
# The journal of the top-level run being executed in this context (None for runs without
# a journal: stores without open_execution_store(), sub-workflow children).
_JOURNAL: contextvars.ContextVar[WorkflowJournal | None] = contextvars.ContextVar(
    "nexura_workflow_journal", default=None
)


def step_idempotency_key(index: int = 0) -> str | None:
    """The idempotency key of effect ``index`` of the step running now, for function handlers.

    The same as ``ExecutionContext.idempotency_key(index)`` for modules: ``nx-`` plus 32 hex
    characters derived from the workspace, run, step key (``<node>#<step>[.<attempt>]``) and
    ``index``. It stays the same when the step runs again after a crash or a takeover, and
    changes for a new graph visit or a re-dispatch a person authorised (``not_applied``).
    Mapped items and parallel branches get their own keys. ``None`` outside a workflow step.
    """
    identity = _STEP_IDENTITY.get()
    return identity.idempotency_key(index) if identity is not None else None


def step_fencing_token() -> int | None:
    """The run lease's fencing token for the step running now (``None`` without a journal)."""
    identity = _STEP_IDENTITY.get()
    return identity.fencing_token if identity is not None else None


_EFFECT_NAMES = {item.value: item for item in EffectClass}


def _coerce_effect(effect: Any, name: str) -> EffectClass | None:
    """An effect class from ``EffectClass``, its value, or a manifest ``effect_policy``."""
    if effect is None:
        return None
    if isinstance(effect, EffectClass):
        return effect
    if isinstance(effect, str):
        if effect in _EFFECT_NAMES:
            return _EFFECT_NAMES[effect]
        try:
            return effect_class_from_policy(effect)
        except ValueError:
            pass
    raise WorkflowValidationError(
        f"Handler '{name}' declares unknown effect {effect!r}; use one of "
        f"{sorted(_EFFECT_NAMES)} (or a manifest effect_policy)."
    )


def _combine_effects(classes: list[EffectClass]) -> EffectClass:
    """The recovery promise of several effects in one step: the weakest one wins."""
    if any(c in (EffectClass.NON_IDEMPOTENT, EffectClass.RECONCILABLE) for c in classes):
        return EffectClass.NON_IDEMPOTENT
    if any(c == EffectClass.IDEMPOTENT for c in classes):
        return EffectClass.IDEMPOTENT
    return EffectClass.PURE


@dataclass(frozen=True)
class NodeRetryPolicy:
    """Retry a failing node with exponential backoff (``WorkflowNode(retry={...})``).

    Keys: ``max_attempts`` (1-20, default 1 = no retry), ``initial_delay`` (s),
    ``max_delay`` (s), ``backoff`` (multiplier), ``retry_on`` (exception class names;
    empty retries any error). Timeouts and interrupts are never retried, and modules
    declaring a ``non_idempotent_write`` effect cannot be retried at all.
    """

    max_attempts: int = 1
    initial_delay: float = 0.5
    max_delay: float = 30.0
    backoff: float = 2.0
    retry_on: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any], node_id: str = "?") -> NodeRetryPolicy:
        """Build a policy from a dict; invalid keys or values raise ``WorkflowValidationError``."""
        unknown = set(data) - {"max_attempts", "initial_delay", "max_delay", "backoff", "retry_on"}
        if unknown:
            raise WorkflowValidationError(
                f"Node '{node_id}' retry has unknown keys: {sorted(unknown)}."
            )
        try:
            policy = cls(
                max_attempts=int(data.get("max_attempts", 1)),
                initial_delay=float(data.get("initial_delay", 0.5)),
                max_delay=float(data.get("max_delay", 30.0)),
                backoff=float(data.get("backoff", 2.0)),
                retry_on=tuple(str(n) for n in data.get("retry_on", ())),
            )
        except (TypeError, ValueError) as exc:
            raise WorkflowValidationError(f"Node '{node_id}' retry is invalid: {exc}") from exc
        if not 1 <= policy.max_attempts <= 20:
            raise WorkflowValidationError(
                f"Node '{node_id}' retry max_attempts must be between 1 and 20."
            )
        if min(policy.initial_delay, policy.max_delay) < 0 or policy.backoff < 1:
            raise WorkflowValidationError(
                f"Node '{node_id}' retry delays must be >= 0 and backoff >= 1."
            )
        return policy

    def delay(self, attempt: int) -> float:
        """Seconds to wait after failed attempt number ``attempt`` (1-based)."""
        return float(min(self.max_delay, self.initial_delay * self.backoff ** (attempt - 1)))

    def retries(self, error: BaseException) -> bool:
        """Whether ``error`` should be retried under this policy."""
        if isinstance(error, WorkflowValidationError):
            return False  # a spec or wiring mistake fails the same way every time
        if not self.retry_on:
            return True
        wanted = set(self.retry_on)
        # A failed sub-workflow / map item arrives wrapped in WorkflowExecutionError; match the
        # original error class(es) it carries too.
        seen: set[int] = set()
        current: BaseException | None = error
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            if {klass.__name__ for klass in type(current).__mro__} & wanted:
                return True
            current = getattr(current, "original_error", None) or current.__cause__
        return False


REDUCERS = ("append", "extend", "sum", "merge", "max", "min")


def _merge_branch_updates(
    node_id: str, updates: list[dict[str, Any]], reducers: dict[str, str], state: dict[str, Any]
) -> dict[str, Any]:
    """Combine the updates of parallel branches or mapped items, in declaration order.

    A reducer folds every value written to its key into the key's current value. A key
    without a reducer may be written by only one branch, so results are never lost to
    whichever branch finished last.
    """
    merged: dict[str, Any] = {}
    for key, how in reducers.items():
        current = copy.deepcopy(state.get(key))
        if current is None:
            current = {"append": [], "extend": [], "merge": {}, "sum": 0}.get(how)
        if current is not None:
            merged[key] = current
    written: set[str] = set()
    for index, update in enumerate(updates):
        for key, value in update.items():
            reducer = reducers.get(key)
            try:
                if reducer is None:
                    if key in written:
                        raise WorkflowValidationError(
                            f"Node '{node_id}': several parallel branches wrote '{key}'. Give "
                            f"it a reducer, e.g. reducers={{'{key}': 'append'}}, or have each "
                            "branch write its own key."
                        )
                    merged[key] = value
                elif reducer == "append":
                    _expect(merged[key], list)
                    merged[key].append(value)
                elif reducer == "extend":
                    _expect(merged[key], list)
                    _expect(value, list)
                    merged[key].extend(value)
                elif reducer == "merge":
                    _expect(merged[key], dict)
                    _expect(value, dict)
                    merged[key].update(value)
                elif reducer == "sum":
                    merged[key] = merged[key] + value
                elif reducer == "max":
                    merged[key] = value if key not in merged else max(merged[key], value)
                else:
                    merged[key] = value if key not in merged else min(merged[key], value)
            except TypeError as exc:
                raise WorkflowValidationError(
                    f"Node '{node_id}': reducer '{reducer}' for '{key}' failed on branch "
                    f"{index}: {exc}"
                ) from exc
            written.add(key)
    return merged


def _expect(value: Any, kind: type) -> None:
    if not isinstance(value, kind):
        raise TypeError(f"expected a {kind.__name__}, got {type(value).__name__}")


def _child_run_id(run_id: str, node_id: str, step: int, attempt: int, item: int | None) -> str:
    """Sub-workflow run ids: ``<parent>/<node>#<step>[.<attempt>][[<item>]]``."""
    child = f"{run_id}/{node_id}#{step}"
    if attempt > 1:
        child += f".{attempt}"
    if item is not None:
        child += f"[{item}]"
    return child


_CHILD_SEGMENT = re.compile(r"^(.*)#(\d+)(?:\.(\d+))?(?:\[(\d+)\])?$", re.DOTALL)


def _child_sort_key(run_id: str) -> list[tuple[str, int, int, int]]:
    """Natural child order per path segment: node, step, attempt, then numeric item index."""
    key: list[tuple[str, int, int, int]] = []
    for segment in run_id.split("/"):
        match = _CHILD_SEGMENT.match(segment)
        if match is None:
            key.append((segment, -1, 0, -1))
        else:
            name, step, attempt, item = match.groups()
            key.append((name, int(step), int(attempt or 1), int(item) if item else -1))
    return key


def _spec_has_gate(spec: WorkflowSpec) -> bool:
    return any(
        node.requires_approval or (node.subgraph is not None and _spec_has_gate(node.subgraph))
        for node in spec.nodes.values()
    )


def _map_inputs(mapping: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """``{target: state_key}`` copies a state value; a non-string target is a literal.

    ``{"$literal": value}`` is an explicit literal (a plain string would be a state key).
    """
    if not mapping:
        return copy.deepcopy(state)
    inputs: dict[str, Any] = {}
    for port_name, state_key in mapping.items():
        if isinstance(state_key, dict) and set(state_key) == {"$literal"}:
            inputs[port_name] = copy.deepcopy(state_key["$literal"])
        elif not isinstance(state_key, str):
            inputs[port_name] = copy.deepcopy(state_key)
        elif state_key in state:
            inputs[port_name] = copy.deepcopy(state[state_key])
    return inputs


class _SubgraphPaused(WorkflowInterrupt):  # noqa: N818 - control flow
    """A sub-workflow paused for approval or input; the parent pauses with it."""

    def __init__(self, result: WorkflowRunResult) -> None:
        super().__init__(None)
        self.result = result


@dataclass(frozen=True)
class HandlerOutput:
    """Explicit output from a workflow node handler."""

    state_update: dict[str, Any] = field(default_factory=dict)
    branch: str | None = None


def _take_regate_provenance(node_id: str) -> dict[str, Any]:
    """Provenance for the first re-gated request of ``node_id`` after an edit, used once."""
    provenance = _REGATE_PROVENANCE.get()
    if not provenance or provenance.get("node_id") != node_id:
        return {}
    _REGATE_PROVENANCE.set(None)
    return {k: v for k, v in provenance.items() if k != "node_id"}


@dataclass(frozen=True)
class PendingApproval:
    """An immutable pending approval request created before a gated node executes."""

    approval_id: str
    workflow_run_id: str
    node_id: str
    input_state: dict[str, Any]
    created_at: float = field(default_factory=time.time)
    workspace_id: str = "default"
    workflow_version: str = "1.0.0"
    spec_hash: str = ""
    required_role: str = "admin"
    expires_at: float | None = None
    policy_version: str = "1.0.0"
    payload_digest: str = ""
    actor: str | None = None
    status: str = "pending"
    decision: dict[str, Any] | None = None
    # What the approver is approving beyond the state: for a mapped write step the item
    # count, the resolved per-item inputs, their digest and the per-approval write cap.
    details: dict[str, Any] | None = None
    # The exact canonical JSON whose SHA-256 is payload_digest, so a reviewer sees the
    # bytes being approved (empty for rows recorded before this field existed).
    canonical_payload_json: str = ""
    # Set when this request replaces one whose inputs were edited on resume: the replaced
    # request's id, the reason ("inputs_edited") and the inputs that request showed.
    supersedes_approval_id: str | None = None
    regate_reason: str | None = None
    previous_input_state: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-compatible form of this approval request."""
        return {
            "approval_id": self.approval_id,
            "workflow_run_id": self.workflow_run_id,
            "node_id": self.node_id,
            "input_state": copy.deepcopy(self.input_state),
            "created_at": self.created_at,
            "workspace_id": self.workspace_id,
            "workflow_version": self.workflow_version,
            "spec_hash": self.spec_hash,
            "required_role": self.required_role,
            "expires_at": self.expires_at,
            "policy_version": self.policy_version,
            "payload_digest": self.payload_digest,
            "actor": self.actor,
            "status": self.status,
            "decision": copy.deepcopy(self.decision) if self.decision else None,
            "details": copy.deepcopy(self.details) if self.details else None,
            "canonical_payload_json": self.canonical_payload_json,
            "supersedes_approval_id": self.supersedes_approval_id,
            "regate_reason": self.regate_reason,
            "previous_input_state": copy.deepcopy(self.previous_input_state),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PendingApproval:
        """Rebuild a pending approval from its dict form, applying defaults for missing fields."""
        return cls(
            approval_id=data["approval_id"],
            workflow_run_id=data["workflow_run_id"],
            node_id=data["node_id"],
            input_state=copy.deepcopy(data.get("input_state", {})),
            # A row with no created_at has an unknown age: 0.0, so a row with no expires_at
            # either is past any approval_ttl (see WorkflowEngine._approval_deadline).
            created_at=float(data.get("created_at", 0.0)),
            workspace_id=str(data.get("workspace_id", "default")),
            workflow_version=str(data.get("workflow_version", "1.0.0")),
            spec_hash=str(data.get("spec_hash", "")),
            required_role=str(data.get("required_role", "admin")),
            expires_at=(float(data["expires_at"]) if data.get("expires_at") is not None else None),
            policy_version=str(data.get("policy_version", "1.0.0")),
            payload_digest=str(data.get("payload_digest", "")),
            actor=data.get("actor"),
            status=str(data.get("status", "pending")),
            decision=copy.deepcopy(data.get("decision")),
            details=copy.deepcopy(data.get("details")),
            canonical_payload_json=str(data.get("canonical_payload_json", "")),
            supersedes_approval_id=data.get("supersedes_approval_id"),
            regate_reason=data.get("regate_reason"),
            previous_input_state=copy.deepcopy(data.get("previous_input_state")),
        )


@dataclass
class WorkflowCheckpoint:
    """Point-in-time execution snapshot for workflow resumption."""

    workflow_run_id: str
    spec_version: int
    spec_hash: str
    current_node_id: str | None
    state: dict[str, Any]
    step_count: int
    status: WorkflowStatus
    pending_approval: PendingApproval | None = None
    error: str | None = None
    updated_at: float = field(default_factory=time.time)
    budget_state: dict[str, Any] | None = None
    # Set while paused by interrupt(): interrupt_id, node_id, value, earlier answers.
    interrupt: dict[str, Any] | None = None
    # Set on runs created by WorkflowEngine.fork(): source run, history index, step.
    forked_from: dict[str, Any] | None = None
    # Workspace that owns the run; None only for checkpoints saved before it was recorded.
    workspace_id: str | None = None
    # Set while paused with pause_reason "effect_unknown": the in-doubt effect (effect_id,
    # step_key, node_id, effect_class, idempotency_key, approval_id, payload_digest).
    pending_effect: dict[str, Any] | None = None
    # Stored revision. A store returns it from get(); saving a checkpoint whose revision
    # is set succeeds only if the stored revision still equals it (otherwise
    # WorkflowConflictError), and the store then sets it to the new revision. None saves
    # unconditionally (last writer wins), as before.
    revision: int | None = None
    workflow_version: str | int | None = None

    @property
    def definition_digest(self) -> str:
        """Executable definition identity (``spec_version`` is the schema version)."""
        return self.spec_hash

    @property
    def pause_reason(self) -> str | None:
        """Why a paused run waits: ``effect_unknown``, ``approval`` or ``interrupt``."""
        if self.status != "paused":
            return None
        if self.pending_effect is not None:
            return "effect_unknown"
        if self.pending_approval is not None:
            return "approval"
        return "interrupt" if self.interrupt is not None else None


@dataclass
class WorkflowRunResult:
    """Final or paused result of a workflow run."""

    workflow_run_id: str
    status: WorkflowStatus
    state: dict[str, Any]
    step_count: int
    current_node_id: str | None = None
    pending_approval: PendingApproval | None = None
    error: str | None = None
    budget_status: dict[str, Any] | None = None
    # Set when paused by interrupt(): {"interrupt_id", "node_id", "value"}.
    interrupt: dict[str, Any] | None = None
    # "effect_unknown" when paused because a dispatched step's outcome is unknown after a
    # crash (see WorkflowEngine.recover); pending_effect then describes that effect.
    pause_reason: str | None = None
    pending_effect: dict[str, Any] | None = None
    workflow_version: str | int | None = None
    definition_digest: str = ""


@dataclass
class WorkflowNode:
    """Defines a single node in a workflow specification."""

    id: str
    handler: str
    transitions: dict[str, str] = field(default_factory=dict)
    default_transition: str | None = None
    requires_approval: bool = False
    required_role: str = "admin"
    module_id: str | None = None
    module_version: str | None = None
    config: dict[str, Any] = field(default_factory=dict)
    input_mapping: dict[str, Any] = field(default_factory=dict)
    output_mapping: dict[str, Any] = field(default_factory=dict)
    presentation: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    # NodeRetryPolicy fields, e.g. {"max_attempts": 3, "initial_delay": 1.0}.
    retry: dict[str, Any] = field(default_factory=dict)
    # Composite steps. ``subgraph`` runs a nested workflow as this step; ``parallel``
    # runs several handlers at once on the same state; ``map_over`` runs the handler (or
    # subgraph) once per item of a state list. ``reducers`` combine their results.
    subgraph: WorkflowSpec | None = None
    parallel: list[str] = field(default_factory=list)
    map_over: str | None = None
    map_as: str = "item"
    reducers: dict[str, str] = field(default_factory=dict)
    max_concurrency: int = 10

    def to_dict(self, include_presentation: bool = True) -> dict[str, Any]:
        """JSON-compatible form; ``include_presentation=False`` drops editor-only data."""
        res: dict[str, Any] = {
            "id": self.id,
            "handler": self.handler,
            "transitions": dict(self.transitions),
            "default_transition": self.default_transition,
            "requires_approval": self.requires_approval,
        }
        if self.module_id is not None:
            res["module_id"] = self.module_id
        if self.module_version is not None:
            res["module_version"] = self.module_version
        if self.config:
            res["config"] = copy.deepcopy(self.config)
        if self.input_mapping:
            res["input_mapping"] = copy.deepcopy(self.input_mapping)
        if self.output_mapping:
            res["output_mapping"] = copy.deepcopy(self.output_mapping)
        if self.metadata:
            meta = copy.deepcopy(self.metadata)
            if not include_presentation:
                for k in ("presentation", "editor", "ui", "position", "viewport", "coordinates"):
                    meta.pop(k, None)
            if meta:
                res["metadata"] = meta
        if include_presentation and self.presentation:
            res["presentation"] = copy.deepcopy(self.presentation)
        if self.retry:
            res["retry"] = copy.deepcopy(self.retry)
        # Emitted only when used, so the hashes of existing specs do not change.
        if self.subgraph is not None:
            res["subgraph"] = self.subgraph.to_dict(include_presentation=include_presentation)
        if self.parallel:
            res["parallel"] = list(self.parallel)
        if self.map_over is not None:
            res["map_over"] = self.map_over
        if self.map_as != "item":
            res["map_as"] = self.map_as
        if self.reducers:
            res["reducers"] = dict(self.reducers)
        if self.max_concurrency != 10:
            res["max_concurrency"] = self.max_concurrency
        return res

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkflowNode:
        """Build a node from its dict form, defaulting optional fields."""
        metadata = dict(data.get("metadata", {}))
        presentation = dict(data.get("presentation", {}))
        if (
            not presentation
            and "presentation" in metadata
            and isinstance(metadata["presentation"], dict)
        ):
            presentation = dict(metadata["presentation"])
        return cls(
            id=data["id"],
            handler=data.get("handler") or data.get("module_id") or "",
            transitions=dict(data.get("transitions", {})),
            default_transition=data.get("default_transition"),
            requires_approval=bool(data.get("requires_approval", False)),
            module_id=data.get("module_id"),
            module_version=data.get("module_version"),
            config=dict(data.get("config", {})),
            input_mapping=dict(data.get("input_mapping", {})),
            output_mapping=dict(data.get("output_mapping", {})),
            presentation=presentation,
            metadata=metadata,
            retry=dict(data.get("retry", {})),
            subgraph=WorkflowSpec.from_dict(data["subgraph"]) if data.get("subgraph") else None,
            parallel=list(data.get("parallel", [])),
            map_over=data.get("map_over"),
            map_as=str(data.get("map_as", "item")),
            reducers=dict(data.get("reducers", {})),
            max_concurrency=_coerce_concurrency(data.get("max_concurrency", 10)),
        )


def _coerce_concurrency(value: Any) -> Any:
    """Normalise a saved max_concurrency; anything not cleanly integral stays raw so
    validation reports it instead of silently truncating (5.5 -> 5) or crashing."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return value


class ModuleRegistry:
    """Binds named identifiers to trusted Python handler functions or BaseModules."""

    def __init__(self, versioned_registry: Any | None = None) -> None:
        self._handlers: dict[str, Callable[..., Any] | BaseModule] = {}
        # Declared effect classes (and reconcile probes) by handler name.
        self._effects: dict[str, tuple[EffectClass, Callable[..., Any] | None]] = {}
        self._versioned_registry = versioned_registry
        # Per-registry replacements of shared versioned modules, keyed by (module_id, version).
        self._overrides: dict[tuple[str, str], BaseModule] = {}

    @property
    def versioned_registry(self) -> Any | None:
        if self._versioned_registry is None:
            try:
                from nexura_runtime.modules.registry import default_registry

                self._versioned_registry = default_registry
            except ImportError:
                self._versioned_registry = None
        return self._versioned_registry

    def register(
        self,
        name: str,
        handler: Callable[..., Any] | BaseModule,
        *,
        effect: EffectClass | str | None = None,
        reconcile: Callable[..., Any] | None = None,
    ) -> None:
        """Register a Python callable or BaseModule by name.

        ``effect`` declares what may happen when the step's outcome is unknown after a
        crash (Execution Contract v1): ``"pure"`` (runs again freely), ``"idempotent"``
        (runs again with the same ``step_idempotency_key()``), ``"non_idempotent"`` (never
        repeated silently: ``recover`` pauses with ``pause_reason="effect_unknown"``) or
        ``"reconcilable"`` with ``reconcile=fn``: ``fn(state, intent)`` (sync or async)
        returns ``Applied(output)``, ``NotApplied()`` or ``Unknown()``. Manifest spellings
        (``read``, ``idempotent_write``, ``non_idempotent_write``) are accepted too. A
        function handler that declares nothing is ``non_idempotent``; a module without a
        declaration here uses its manifest's ``effect_policy``.
        """
        if not isinstance(name, str) or not name.strip():
            raise WorkflowValidationError("Handler name must be a non-empty string.")
        if not callable(handler) and not hasattr(handler, "execute"):
            raise WorkflowValidationError(f"Handler '{name}' must be a callable or BaseModule.")
        declared = _coerce_effect(effect, name)
        if declared == EffectClass.RECONCILABLE and not callable(reconcile):
            raise WorkflowValidationError(
                f"Handler '{name}' declares effect='reconcilable' and needs reconcile=fn."
            )
        if reconcile is not None and declared != EffectClass.RECONCILABLE:
            raise WorkflowValidationError(
                f"Handler '{name}': reconcile= is only used with effect='reconcilable'."
            )
        key = name.strip()
        self._handlers[key] = handler
        if declared is None:
            self._effects.pop(key, None)
        else:
            self._effects[key] = (declared, reconcile)

    def register_handler(
        self,
        name: str,
        *,
        effect: EffectClass | str | None = None,
        reconcile: Callable[..., Any] | None = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Decorator to register a handler callable (``effect=`` as in :meth:`register`)."""

        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.register(name, fn, effect=effect, reconcile=reconcile)
            return fn

        return decorator

    def declared_effect(self, name: str) -> tuple[EffectClass, Callable[..., Any] | None] | None:
        """The effect class (and reconcile probe) registered for ``name``, if declared."""
        return self._effects.get(name)

    def override_module(self, module: BaseModule) -> None:
        """Use ``module`` instead of the shared versioned module with its id and version.

        Only this registry sees it, so one app can bind a module copy to its own resources
        (the service binds agent nodes to its skills store) without touching other apps.
        """
        manifest = module.manifest
        self._overrides[(manifest.module_id, manifest.version)] = module

    def _override(self, module_id: str, version: str | None) -> BaseModule | None:
        if version is not None:
            return self._overrides.get((module_id, version))
        v_reg = self.versioned_registry
        if v_reg is not None and v_reg.has(module_id, None):
            latest = v_reg.get(module_id, None).manifest.version
            return self._overrides.get((module_id, latest))
        return None

    def include(self, other: ModuleRegistry) -> None:
        """Add every handler from ``other``; a name registered in both is rejected."""
        clashes = sorted(set(self._handlers) & set(other._handlers))
        if clashes:
            raise WorkflowValidationError(f"Handler names already registered: {clashes}.")
        self._handlers.update(other._handlers)
        self._effects.update(other._effects)

    def include_from(self, target: str) -> None:
        """Import trusted handlers named by an operator as ``"package.module:attribute"``.

        The attribute must be a ``ModuleRegistry``, or a callable that takes this registry
        and registers handlers on it. Workflow JSON never chooses what is imported.
        """
        module_name, sep, attr = target.partition(":")
        if not sep or not module_name or not attr:
            raise WorkflowValidationError(
                f"Handler source '{target}' must look like 'package.module:attribute'."
            )
        try:
            source = getattr(importlib.import_module(module_name), attr)
        except (ImportError, AttributeError) as exc:
            raise WorkflowValidationError(f"Cannot load handlers from '{target}': {exc}") from exc
        if isinstance(source, ModuleRegistry):
            self.include(source)
        elif callable(source):
            source(self)
        else:
            raise WorkflowValidationError(
                f"'{target}' must be a ModuleRegistry or a callable taking one."
            )

    def get(self, name: str) -> Any:
        """Retrieve registered handler callable or BaseModule by name."""
        if name in self._handlers:
            return self._handlers[name]

        v_reg = self.versioned_registry
        if v_reg is not None:
            mod_id, sep, ver = name.partition("@")
            version = ver if sep else None
            if v_reg.has(mod_id, version):
                return self._override(mod_id, version) or v_reg.get(mod_id, version)

        raise WorkflowValidationError(
            f"Handler '{name}' is not registered in ModuleRegistry. "
            f"Available handlers: {list(self._handlers.keys())}"
        )

    def has_handler(self, name: str) -> bool:
        """True if the given handler name is registered."""
        if name in self._handlers:
            return True
        v_reg = self.versioned_registry
        if v_reg is not None:
            mod_id, sep, ver = name.partition("@")
            version = ver if sep else None
            return bool(v_reg.has(mod_id, version))
        return False

    def get_module(self, module_id: str, version: str | None = None) -> Any:
        """Retrieve BaseModule from versioned module registry."""
        v_reg = self.versioned_registry
        if v_reg is not None and v_reg.has(module_id, version):
            return self._override(module_id, version) or v_reg.get(module_id, version)
        raise WorkflowValidationError(f"Module '{module_id}' (version: {version}) not found.")

    def has_module(self, module_id: str, version: str | None = None) -> bool:
        """True if versioned module is registered."""
        v_reg = self.versioned_registry
        if v_reg is not None:
            return bool(v_reg.has(module_id, version))
        return False

    def describe_module(self, module_id: str, version: str | None = None) -> dict[str, Any] | None:
        """Describe module metadata if registered."""
        v_reg = self.versioned_registry
        if v_reg is not None and v_reg.has(module_id, version):
            desc = v_reg.describe(module_id, version)
            return dict(desc) if desc is not None else None
        return None


SUPPORTED_SPEC_VERSIONS: frozenset[int] = frozenset({1})


class WorkflowMigrationError(WorkflowSpecMismatchError):
    """A changed workflow cannot safely continue its recorded execution."""

    def __init__(self, expected_hash: str, actual_hash: str, reason: str) -> None:
        super().__init__(expected_hash=expected_hash, actual_hash=actual_hash)
        self.args = (f"Workflow definition changed: {reason}",)


def _mermaid_spec(spec: WorkflowSpec, lines: list[str], prefix: str, depth: int) -> None:
    from nexura_agents.diagrams import mermaid_label

    pad = "    " * depth
    ids = {nid: f"{prefix}_{i}" for i, nid in enumerate(spec.nodes)}
    for nid, node in spec.nodes.items():
        node_id = ids[nid]
        label = nid + (" (approval)" if node.requires_approval else "")
        if node.subgraph is not None:
            title = label + (f" - for each {node.map_over}" if node.map_over else "")
            lines.append(f"{pad}subgraph {node_id}[{mermaid_label(title)}]")
            _mermaid_spec(node.subgraph, lines, prefix=f"{node_id}s", depth=depth + 1)
            lines.append(f"{pad}end")
            continue
        if node.parallel:
            label += " - parallel: " + ", ".join(node.parallel)
        elif node.map_over:
            label += f" - for each {node.map_over}"
        lines.append(f"{pad}{node_id}[{mermaid_label(label)}]")
    start = f"{prefix}_start"
    lines.append(f"{pad}{start}((start)) --> {ids[spec.entry_node]}")
    ends: dict[str, str] = {}

    def target_id(target: str) -> str:
        if target in ids:
            return ids[target]
        if target not in ends:
            ends[target] = f"{prefix}_end{len(ends)}"
            lines.append(f"{pad}{ends[target]}(({mermaid_label(target)}))")
        return ends[target]

    for nid, node in spec.nodes.items():
        for branch, target in node.transitions.items():
            lines.append(f"{pad}{ids[nid]} -->|{mermaid_label(branch)}| {target_id(target)}")
        if node.default_transition:
            lines.append(f"{pad}{ids[nid]} -.->|default| {target_id(node.default_transition)}")
    for terminal in spec.terminal_nodes:
        if terminal in ids:
            lines.append(f"{pad}class {ids[terminal]} terminal")
    if depth == 1:
        lines.append(f"{pad}classDef terminal stroke-width:3px")


@dataclass
class WorkflowSpec:
    """JSON-serializable, versioned workflow specification."""

    name: str
    entry_node: str
    nodes: dict[str, WorkflowNode]
    spec_version: int = 1
    terminal_nodes: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    presentation: dict[str, Any] = field(default_factory=dict)
    version: str | int | None = None
    # Code registrations are deliberately excluded from serialized definitions/digests.
    migrations: dict[
        tuple[str | int, str | int],
        Callable[[str | int, str | int, dict[str, Any]], dict[str, Any]],
    ] = field(default_factory=dict, repr=False, compare=False)

    def register_migration(
        self,
        from_version: str | int,
        to_version: str | int,
        migrate: Callable[[str | int, str | int, dict[str, Any]], dict[str, Any]],
    ) -> None:
        """Register a pure state transformation; directed edges can form a chain.

        Functions receive a deep copy and must return a JSON object. Never perform I/O:
        an uncommitted calculation can repeat after a crash. Committed results do not.
        """
        for version in (from_version, to_version):
            if isinstance(version, bool) or not isinstance(version, (str, int)):
                raise WorkflowValidationError("Migration versions must be strings or integers.")
        if from_version == to_version or not callable(migrate):
            raise WorkflowValidationError("A migration needs distinct versions and a callable.")
        self.migrations[from_version, to_version] = migrate

    def to_mermaid(self) -> str:
        """A Mermaid ``flowchart`` of this workflow: steps, branch labels and composites.

        The entry is marked, approval steps say ``(approval)``, terminal targets are
        circles, ``default`` transitions are dotted, sub-workflows are drawn as nested
        boxes, and parallel/``map_over`` steps list their branches.
        """
        lines = ["flowchart TD"]
        _mermaid_spec(self, lines, prefix="w", depth=1)
        return "\n".join(lines) + "\n"

    def canonical_hash(self) -> str:
        """Compute deterministic SHA-256 digest of this executable specification.

        Presentation metadata (coordinates, viewport, editor hints) is explicitly
        excluded so cosmetic layout changes do not alter executable identity or approval hashes.
        """
        canonical_dict = self.to_dict(include_presentation=False)
        canonical_json = json.dumps(canonical_dict, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

    def to_dict(self, include_presentation: bool = True) -> dict[str, Any]:
        """Convert specification to a standard Python dictionary."""
        res: dict[str, Any] = {
            "spec_version": self.spec_version,
            "name": self.name,
            "entry_node": self.entry_node,
            "nodes": {
                nid: node.to_dict(include_presentation=include_presentation)
                for nid, node in self.nodes.items()
            },
            "terminal_nodes": sorted(self.terminal_nodes),
        }
        if self.version is not None:
            # ``version`` was historically an ignored extension in imported specs.
            # Use an unambiguous serialized identity so those imports keep their hash.
            res["workflow_version"] = self.version
        if self.metadata:
            meta = copy.deepcopy(self.metadata)
            if not include_presentation:
                for k in ("presentation", "editor", "ui", "position", "viewport", "coordinates"):
                    meta.pop(k, None)
            if meta:
                res["metadata"] = meta
        if include_presentation and self.presentation:
            res["presentation"] = copy.deepcopy(self.presentation)
        return res

    def to_json(self, indent: int | None = 2) -> str:
        """Serialize specification to formatted JSON string."""
        return json.dumps(
            self.to_dict(include_presentation=True), indent=indent, ensure_ascii=False
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkflowSpec:
        """Create specification from dictionary representation."""
        if not isinstance(data, dict):
            raise WorkflowValidationError("WorkflowSpec payload must be a JSON object.")
        if "name" not in data or not isinstance(data["name"], str):
            raise WorkflowValidationError("WorkflowSpec requires a string 'name'.")
        if "entry_node" not in data or not isinstance(data["entry_node"], str):
            raise WorkflowValidationError("WorkflowSpec requires a string 'entry_node'.")

        raw_nodes = data.get("nodes")
        if not isinstance(raw_nodes, (dict, list)):
            raise WorkflowValidationError("WorkflowSpec 'nodes' must be a dict or list.")

        raw_version = data.get("spec_version", 1)
        try:
            parsed_version = int(raw_version)
        except (ValueError, TypeError) as exc:
            raise UnsupportedWorkflowVersionError(raw_version, SUPPORTED_SPEC_VERSIONS) from exc

        if parsed_version not in SUPPORTED_SPEC_VERSIONS:
            raise UnsupportedWorkflowVersionError(parsed_version, SUPPORTED_SPEC_VERSIONS)

        nodes: dict[str, WorkflowNode] = {}
        if isinstance(raw_nodes, list):
            for raw_node in raw_nodes:
                if not isinstance(raw_node, dict) or "id" not in raw_node:
                    raise WorkflowValidationError("Each node in nodes list must have an 'id'.")
                nid = raw_node["id"]
                if nid in nodes:
                    raise WorkflowValidationError(f"Duplicate node ID in workflow spec: '{nid}'.")
                nodes[nid] = WorkflowNode.from_dict(raw_node)
        else:
            for nid, raw_node in raw_nodes.items():
                if not isinstance(raw_node, dict):
                    raise WorkflowValidationError(f"Node '{nid}' definition must be a dictionary.")
                if raw_node.get("id") and raw_node["id"] != nid:
                    raise WorkflowValidationError(
                        f"Node key '{nid}' does not match node id '{raw_node['id']}'."
                    )
                raw_node_copy = dict(raw_node)
                raw_node_copy["id"] = nid
                nodes[nid] = WorkflowNode.from_dict(raw_node_copy)

        metadata = dict(data.get("metadata", {}))
        presentation = dict(data.get("presentation", {}))
        if (
            not presentation
            and "presentation" in metadata
            and isinstance(metadata["presentation"], dict)
        ):
            presentation = dict(metadata["presentation"])

        return cls(
            name=data["name"],
            entry_node=data["entry_node"],
            nodes=nodes,
            spec_version=parsed_version,
            terminal_nodes=list(data.get("terminal_nodes", [])),
            metadata=metadata,
            presentation=presentation,
            version=data.get("workflow_version"),
        )

    @classmethod
    def from_json(cls, json_str: str) -> WorkflowSpec:
        """Deserialize specification from JSON string."""
        try:
            data = json.loads(json_str)
        except Exception as exc:
            raise WorkflowValidationError(f"Malformed JSON for WorkflowSpec: {exc}") from exc
        return cls.from_dict(data)

    def validate(
        self,
        registry: ModuleRegistry | None = None,
        approval_required_handlers: frozenset[str] | set[str] | None = None,
    ) -> None:
        """Validate specification graph completeness, entries, handler bindings, and policies."""
        if self.spec_version not in SUPPORTED_SPEC_VERSIONS:
            raise UnsupportedWorkflowVersionError(self.spec_version, SUPPORTED_SPEC_VERSIONS)
        if self.version is not None and (
            isinstance(self.version, bool) or not isinstance(self.version, (str, int))
        ):
            raise WorkflowValidationError("Workflow version must be a string or integer.")
        if not self.nodes:
            raise WorkflowValidationError("WorkflowSpec must have at least one node.")
        if self.entry_node not in self.nodes:
            raise WorkflowValidationError(
                f"Entry node '{self.entry_node}' not found in registered nodes: "
                f"{list(self.nodes.keys())}."
            )

        for node_id, node in self.nodes.items():
            composite = node.subgraph is not None or bool(node.parallel)
            _validate_composite(node_id, node, registry, approval_required_handlers)
            handler_name = node.handler or node.module_id or ""
            if not composite and (not handler_name or not isinstance(handler_name, str)):
                raise WorkflowValidationError(
                    f"Node '{node_id}' must specify a valid handler name or module_id."
                )

            # Check registry binding if provided
            if registry is not None and not composite:
                has_h = registry.has_handler(handler_name)
                has_m = bool(
                    node.module_id and registry.has_module(node.module_id, node.module_version)
                )
                if not has_h and not has_m:
                    raise WorkflowValidationError(
                        f"Node '{node_id}' references unknown handler '{handler_name}'."
                    )

                # If bound to a BaseModule, perform schema & mapping contract checks
                mod_obj = None
                if node.module_id and registry.has_module(node.module_id, node.module_version):
                    mod_obj = registry.get_module(node.module_id, node.module_version)
                elif has_h:
                    try:
                        resolved = registry.get(handler_name)
                        if hasattr(resolved, "manifest"):
                            mod_obj = resolved
                    except Exception:
                        pass

                if mod_obj is not None and hasattr(mod_obj, "manifest"):
                    manifest = mod_obj.manifest
                    effect = getattr(manifest, "effect_policy", None)
                    if (
                        node.retry
                        and int(node.retry.get("max_attempts", 1)) > 1
                        and (effect == "non_idempotent_write")
                    ):
                        raise WorkflowValidationError(
                            f"Node '{node_id}' retries module '{manifest.module_id}', whose "
                            "effects are non-idempotent; a retry could repeat them. Remove "
                            "the retry or make the module idempotent."
                        )
                    if node.config:
                        try:
                            manifest.validate_config(node.config)
                        except Exception as exc:
                            raise WorkflowValidationError(
                                f"Node '{node_id}' configuration invalid for module "
                                f"'{manifest.module_id}': {exc}"
                            ) from exc

                    if node.input_mapping:
                        allowed_inputs = set(manifest.input_schema.get("properties", {}).keys()) | {
                            p.name for p in manifest.named_ports
                        }
                        if allowed_inputs:
                            for port_name in node.input_mapping:
                                if port_name not in allowed_inputs:
                                    raise WorkflowValidationError(
                                        f"Node '{node_id}' input_mapping references unknown input "
                                        f"port '{port_name}' for module '{manifest.module_id}'. "
                                        f"Allowed ports: {sorted(allowed_inputs)}"
                                    )

                    if node.output_mapping:
                        allowed_outputs = set(
                            manifest.output_schema.get("properties", {}).keys()
                        ) | {p.name for p in manifest.named_ports}
                        if allowed_outputs:
                            for port_name in node.output_mapping:
                                if port_name not in allowed_outputs:
                                    raise WorkflowValidationError(
                                        f"Node '{node_id}' output_mapping references "
                                        f"unknown output port '{port_name}' for module "
                                        f"'{manifest.module_id}'. "
                                        f"Allowed ports: {sorted(allowed_outputs)}"
                                    )

            NodeRetryPolicy.from_dict(node.retry, node_id)

            # Enforce mandatory approval policy if configured
            if (
                approval_required_handlers is not None
                and (
                    node.handler in approval_required_handlers
                    or (node.module_id and node.module_id in approval_required_handlers)
                )
                and not node.requires_approval
            ):
                raise WorkflowValidationError(
                    f"Node '{node_id}' handler '{node.handler}' requires mandatory "
                    "approval; cannot be disabled."
                )

            for branch, target in node.transitions.items():
                if target not in self.nodes and target not in self.terminal_nodes:
                    raise WorkflowValidationError(
                        f"Node '{node_id}' transition '{branch}' targets unknown node '{target}'."
                    )
            if (
                node.default_transition
                and node.default_transition not in self.nodes
                and node.default_transition not in self.terminal_nodes
            ):
                raise WorkflowValidationError(
                    f"Node '{node_id}' default_transition targets unknown node "
                    f"'{node.default_transition}'."
                )


def _validate_composite(
    node_id: str,
    node: WorkflowNode,
    registry: ModuleRegistry | None,
    approval_required_handlers: frozenset[str] | set[str] | None,
) -> None:
    def fail(message: str) -> None:
        raise WorkflowValidationError(f"Node '{node_id}': {message}")

    # Retry fields first: later checks read max_attempts and must not crash on garbage.
    retry_attempts = NodeRetryPolicy.from_dict(node.retry, node_id).max_attempts
    if node.subgraph is not None and node.parallel:
        fail("use either subgraph or parallel, not both.")
    if (node.subgraph is not None or node.parallel) and (node.handler or node.module_id):
        fail("a subgraph or parallel step must not also set handler or module_id.")
    if node.parallel:
        if node.map_over is not None:
            fail("map_over cannot be combined with parallel.")
        if len(set(node.parallel)) != len(node.parallel) or not all(
            isinstance(name, str) and name for name in node.parallel
        ):
            fail("parallel must list distinct handler names.")
        if not node.requires_approval and approval_required_handlers:
            gated = [name for name in node.parallel if name in approval_required_handlers]
            if gated:
                fail(f"{gated} require approval; set requires_approval on this step.")
        for name in node.parallel:
            if registry is None:
                continue
            if not registry.has_handler(name):
                fail(f"parallel references unknown handler '{name}'.")
            resolved = registry.get(name)
            effect = getattr(getattr(resolved, "manifest", None), "effect_policy", None)
            if effect == "non_idempotent_write" and retry_attempts > 1:
                fail(f"retries would repeat '{name}', whose effects are non-idempotent.")
    if node.map_over is not None and (not isinstance(node.map_over, str) or not node.map_over):
        fail("map_over must name a list in the workflow state.")
    if not isinstance(node.map_as, str) or not node.map_as.isidentifier():
        fail("map_as must be a plain key name such as 'item'.")
    if node.reducers:
        if not node.parallel and node.map_over is None:
            fail("reducers apply only to parallel or map_over steps.")
        unknown = {how for how in node.reducers.values() if how not in REDUCERS}
        if unknown:
            fail(f"unknown reducers {sorted(unknown)}; choose from {list(REDUCERS)}.")
    if (
        isinstance(node.max_concurrency, bool)
        or not isinstance(node.max_concurrency, int)
        or not 1 <= node.max_concurrency <= 100
    ):
        fail("max_concurrency must be between 1 and 100.")
    if node.subgraph is not None:
        if registry is not None and retry_attempts > 1:
            unsafe = _non_idempotent_steps(node.subgraph, registry)
            if unsafe:
                fail(
                    f"retries re-run the whole sub-workflow, which would repeat the "
                    f"non-idempotent steps {unsafe}. Remove the retry or retry inside."
                )
        if node.map_over is not None and _spec_has_gate(node.subgraph):
            fail(
                "a sub-workflow run per item (map_over) cannot contain approval gates; "
                "approve before or after the mapped step."
            )
        try:
            node.subgraph.validate(registry, approval_required_handlers)
        except WorkflowValidationError as exc:
            raise WorkflowValidationError(f"Sub-workflow in node '{node_id}': {exc}") from exc


def _non_idempotent_steps(
    spec: WorkflowSpec, registry: ModuleRegistry, prefix: str = ""
) -> list[str]:
    """Paths of steps (at any depth) whose module declares a non-idempotent effect."""
    found: list[str] = []
    for node_id, node in spec.nodes.items():
        path = f"{prefix}{node_id}"
        candidates: list[Any] = []
        if node.module_id and registry.has_module(node.module_id, node.module_version):
            candidates.append(registry.get_module(node.module_id, node.module_version))
        for name in node.parallel or ([node.handler] if node.handler else []):
            if registry.has_handler(name):
                candidates.append(registry.get(name))
        if any(
            getattr(getattr(c, "manifest", None), "effect_policy", None) == "non_idempotent_write"
            for c in candidates
        ):
            found.append(path)
        if node.subgraph is not None:
            found.extend(_non_idempotent_steps(node.subgraph, registry, f"{path}/"))
    return found


@runtime_checkable
class WorkflowCheckpointStore(Protocol):
    """Protocol for persisting workflow execution checkpoints."""

    async def get(self, workflow_run_id: str) -> WorkflowCheckpoint | None:
        """Retrieve checkpoint for a workflow run."""
        ...

    async def save(self, checkpoint: WorkflowCheckpoint) -> None:
        """Save or update workflow checkpoint."""
        ...

    async def create(self, checkpoint: WorkflowCheckpoint) -> bool:
        """Atomically reserve a new run ID; return False if it already exists."""
        ...

    async def claim_approval(self, workflow_run_id: str, approval_id: str) -> bool:
        """Atomically claim a pending approval for execution.

        Returns:
            True if atomically claimed; False if already claimed or resolved.
        """
        ...

    async def close(self) -> None:
        """Release underlying storage resources."""
        ...


def checkpoint_to_dict(cp: WorkflowCheckpoint) -> dict[str, Any]:
    """JSON-compatible form of a checkpoint (used for history records)."""
    data = {
        "workflow_run_id": cp.workflow_run_id,
        "spec_version": cp.spec_version,
        "spec_hash": cp.spec_hash,
        "workflow_version": cp.workflow_version,
        "definition_digest": cp.spec_hash,
        "current_node_id": cp.current_node_id,
        "state": copy.deepcopy(cp.state),
        "step_count": cp.step_count,
        "status": cp.status,
        "pending_approval": cp.pending_approval.to_dict() if cp.pending_approval else None,
        "error": cp.error,
        "updated_at": cp.updated_at,
        "budget_state": copy.deepcopy(cp.budget_state),
        "interrupt": copy.deepcopy(cp.interrupt),
        "forked_from": copy.deepcopy(cp.forked_from),
        "workspace_id": cp.workspace_id,
    }
    if cp.pending_effect is not None:  # only on effect_unknown pauses
        data["pending_effect"] = copy.deepcopy(cp.pending_effect)
    return data


def checkpoint_from_dict(data: dict[str, Any]) -> WorkflowCheckpoint:
    pending = data.get("pending_approval")
    return WorkflowCheckpoint(
        workflow_run_id=data["workflow_run_id"],
        spec_version=int(data["spec_version"]),
        spec_hash=data["spec_hash"],
        workflow_version=data.get("workflow_version"),
        current_node_id=data.get("current_node_id"),
        state=copy.deepcopy(data.get("state", {})),
        step_count=int(data["step_count"]),
        status=data["status"],
        pending_approval=PendingApproval.from_dict(pending) if pending else None,
        error=data.get("error"),
        updated_at=float(data.get("updated_at", time.time())),
        budget_state=copy.deepcopy(data.get("budget_state")),
        interrupt=copy.deepcopy(data.get("interrupt")),
        forked_from=copy.deepcopy(data.get("forked_from")),
        workspace_id=data.get("workspace_id"),
        pending_effect=copy.deepcopy(data.get("pending_effect")),
    )


def _pending_id(cp: WorkflowCheckpoint) -> str | None:
    """The id a paused run is waiting on: an approval or an interrupt."""
    if cp.pending_approval is not None:
        return cp.pending_approval.approval_id
    if cp.interrupt is not None:
        return str(cp.interrupt.get("interrupt_id"))
    return None


def _is_start_reservation(cp: WorkflowCheckpoint) -> bool:
    # Reservations prevent duplicate dispatch, but do not add an executed step to history.
    return cp.status == "running" and cp.step_count == 0 and cp.forked_from is None


def _revision_conflict(run_id: str, expected: int, actual: int) -> WorkflowConflictError:
    error = WorkflowConflictError(run_id, expected, actual)
    error.args = (
        f"Workflow run '{run_id}' changed since it was read (checkpoint revision {expected}, "
        f"now {actual}); another worker owns it.",
    )
    return error


async def workflow_run_identities(store: Any, run_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Active (and, where the store knows it, starting) identity of each listed run.

    Returns ``{run_id: {"definition_digest", "workflow_version", "started_workflow_version"}}``
    for the runs that have a checkpoint; missing runs are left out. A store may provide
    ``async run_identities(run_ids)`` to read a whole page at once without loading
    checkpoint state (the built-in memory, SQLite and ``nexura-postgres`` stores do). Any
    other store is read run by run: ``get()`` for the active identity and, when the store
    has ``history()``, its first checkpoint for the starting version (otherwise
    ``started_workflow_version`` is left out and callers use the run's origin definition).
    """
    if not run_ids:
        return {}
    batch = getattr(store, "run_identities", None)
    if batch is not None:
        result: dict[str, dict[str, Any]] = await batch(run_ids)
        return result
    history = getattr(store, "history", None)
    identities: dict[str, dict[str, Any]] = {}
    for run_id in run_ids:
        cp = await store.get(run_id)
        if cp is None:
            continue
        identity: dict[str, Any] = {
            "definition_digest": cp.spec_hash,
            "workflow_version": cp.workflow_version,
        }
        if history is not None:
            saved = await history(run_id)
            if saved:
                identity["started_workflow_version"] = saved[0].workflow_version
        identities[run_id] = identity
    return identities


class MemoryWorkflowStore:
    """In-memory checkpoint store for transient workflow runs and tests.

    Its execution journal (leases, intents, effects) is a ``MemoryExecutionStore``, or the
    ``execution_store`` given.
    """

    def __init__(self, *, execution_store: ExecutionStore | None = None) -> None:
        self._checkpoints: dict[str, WorkflowCheckpoint] = {}
        self._history: dict[str, list[WorkflowCheckpoint]] = {}
        self._lock = asyncio.Lock()
        self._execution_store = execution_store

    async def open_execution_store(self) -> ExecutionStore:
        """The execution journal kept next to these checkpoints (created on first use)."""
        if self._execution_store is None:
            from nexura_agents.execution_store import MemoryExecutionStore

            self._execution_store = MemoryExecutionStore()
        return self._execution_store

    async def get(self, workflow_run_id: str) -> WorkflowCheckpoint | None:
        """The latest checkpoint of a run, or ``None`` if it does not exist."""
        async with self._lock:
            cp = self._checkpoints.get(workflow_run_id)
            if cp is None:
                return None
            return copy.deepcopy(cp)

    async def run_identities(self, run_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Read a page's checkpoint identities under one store lock."""
        async with self._lock:
            return {
                run_id: {
                    "definition_digest": self._checkpoints[run_id].spec_hash,
                    "workflow_version": self._checkpoints[run_id].workflow_version,
                    "started_workflow_version": self._history[run_id][0].workflow_version,
                }
                for run_id in run_ids
                if run_id in self._checkpoints
            }

    async def save(self, checkpoint: WorkflowCheckpoint) -> None:
        """Store a checkpoint as the run's latest and append it to its history.

        Fenced when ``checkpoint.revision`` is set: the stored revision must still equal
        it (``WorkflowConflictError`` otherwise); it is then set to the new revision.
        """
        async with self._lock:
            stored = copy.deepcopy(checkpoint)
            previous = self._checkpoints.get(checkpoint.workflow_run_id)
            expected = checkpoint.revision
            current = (previous.revision or 0) if previous is not None else None
            if expected is not None and current is not None and current != expected:
                raise _revision_conflict(checkpoint.workflow_run_id, expected, current)
            if previous is not None:
                if stored.forked_from is None:
                    stored.forked_from = copy.deepcopy(previous.forked_from)
                if stored.workspace_id is None:
                    stored.workspace_id = previous.workspace_id
            stored.revision = current + 1 if current is not None else (expected or 0) + 1
            stored.updated_at = time.time()
            self._checkpoints[checkpoint.workflow_run_id] = stored
            self._history.setdefault(checkpoint.workflow_run_id, []).append(copy.deepcopy(stored))
            if expected is not None:
                checkpoint.revision = stored.revision

    async def create(self, checkpoint: WorkflowCheckpoint) -> bool:
        """Save the first checkpoint of a new run; False if the run id is taken."""
        async with self._lock:
            if checkpoint.workflow_run_id in self._checkpoints:
                return False
            stored = copy.deepcopy(checkpoint)
            stored.updated_at = time.time()
            stored.revision = 1
            checkpoint.revision = 1
            self._checkpoints[checkpoint.workflow_run_id] = stored
            if not _is_start_reservation(stored):
                self._history.setdefault(checkpoint.workflow_run_id, []).append(
                    copy.deepcopy(stored)
                )
            return True

    async def history(self, workflow_run_id: str) -> list[WorkflowCheckpoint]:
        """Every checkpoint saved for the run, oldest first."""
        async with self._lock:
            return copy.deepcopy(self._history.get(workflow_run_id, []))

    async def list_children(self, parent_id: str) -> list[WorkflowCheckpoint]:
        """Latest checkpoint of every child run (ids starting ``<parent_id>/``), by id."""
        prefix = f"{parent_id}/"
        async with self._lock:
            return [
                copy.deepcopy(cp)
                for run_id, cp in sorted(
                    self._checkpoints.items(), key=lambda kv: _child_sort_key(kv[0])
                )
                if run_id.startswith(prefix)
            ]

    async def list_incomplete(
        self, limit: int = 100, *, workspace_id: str | None = None
    ) -> list[str]:
        """Ids of top-level runs whose latest checkpoint is ``running``, oldest update first.

        The candidates for ``WorkflowEngine.recover`` (a run is only taken over once its
        journal lease is free or expired). Sub-workflow children are left out: their
        parent's ``recover`` continues them. ``workspace_id`` narrows the list.
        """
        async with self._lock:
            found = sorted(
                (cp.updated_at, run_id)
                for run_id, cp in self._checkpoints.items()
                if cp.status == "running"
                and "/" not in run_id
                and (workspace_id is None or cp.workspace_id == workspace_id)
            )
        return [run_id for _, run_id in found[: max(int(limit), 0)]]

    async def claim_approval(self, workflow_run_id: str, approval_id: str) -> bool:
        """Atomically claim a pending approval or interrupt; ``False`` if already claimed."""
        async with self._lock:
            cp = self._checkpoints.get(workflow_run_id)
            if cp is not None and cp.status == "paused" and _pending_id(cp) == approval_id:
                pending = cp.pending_approval
                if (
                    pending is not None
                    and pending.expires_at is not None
                    and time.time() > pending.expires_at
                ):
                    return False  # expired: never claimed (checked under the same lock)
                cp.status = "running"
                cp.revision = (cp.revision or 0) + 1
                return True
            return False

    async def close(self) -> None:
        """Release resources held by the store."""
        pass


class SQLiteWorkflowStore:
    """Durable workflow checkpoint store backed by sqlite3.

    Its execution journal (leases, intents, effects) is a ``SQLiteExecutionStore`` in the
    same database file, opened on first use with its own connection.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).resolve()
        self._lock = threading.Lock()
        self._closed = False
        self._journal_lock = threading.Lock()
        self._journal: Any = None
        self._journal_conn: sqlite3.Connection | None = None
        self._journal_ready = False

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            timeout=10.0,
        )
        with self._lock, self._conn:
            self._conn.execute("PRAGMA journal_mode = WAL;")
            self._conn.execute("PRAGMA busy_timeout = 5000;")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS nexura_workflow_checkpoints (
                    workflow_run_id TEXT PRIMARY KEY,
                    spec_version INTEGER NOT NULL,
                    spec_hash TEXT NOT NULL,
                    current_node_id TEXT,
                    state_json TEXT NOT NULL,
                    step_count INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    approval_id TEXT,
                    approval_json TEXT,
                    error TEXT,
                    updated_at REAL NOT NULL,
                    budget_json TEXT
                );
                """
            )
            for column in (
                "budget_json",
                "interrupt_json",
                "meta_json",
                "workspace_id",
                "effect_json",
                "workflow_version_json",
            ):
                with contextlib.suppress(sqlite3.OperationalError):
                    self._conn.execute(
                        f"ALTER TABLE nexura_workflow_checkpoints ADD COLUMN {column} TEXT;"
                    )
            with contextlib.suppress(sqlite3.OperationalError):
                self._conn.execute(
                    "ALTER TABLE nexura_workflow_checkpoints "
                    "ADD COLUMN revision INTEGER NOT NULL DEFAULT 0;"
                )
            # Every saved checkpoint, append-only, for inspection, replay and forks.
            had_history = self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'nexura_workflow_checkpoint_history'"
            ).fetchone()
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS nexura_workflow_checkpoint_history (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    workflow_run_id TEXT NOT NULL,
                    checkpoint_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_nexura_wf_history_run "
                "ON nexura_workflow_checkpoint_history (workflow_run_id, seq);"
            )
            if not had_history:
                # Databases from before history existed: start each run's history with
                # its current checkpoint, so old runs can still be inspected and forked.
                run_ids = self._conn.execute(
                    "SELECT workflow_run_id FROM nexura_workflow_checkpoints"
                ).fetchall()
                for (run_id,) in run_ids:
                    cp = self._read_checkpoint(self._conn, run_id)
                    if cp is not None:
                        self._conn.execute(
                            "INSERT INTO nexura_workflow_checkpoint_history "
                            "(workflow_run_id, checkpoint_json, created_at) VALUES (?, ?, ?)",
                            (run_id, json.dumps(checkpoint_to_dict(cp)), cp.updated_at),
                        )
                self._conn.commit()

    def _ensure_open(self) -> sqlite3.Connection:
        if self._closed or self._conn is None:
            raise WorkflowError("SQLiteWorkflowStore connection is closed.")
        return self._conn

    async def open_execution_store(self) -> ExecutionStore:
        """The execution journal in the same database file (created on first use)."""
        from nexura_agents.execution_store import SQLiteExecutionStore

        with self._journal_lock:
            with self._lock:
                self._ensure_open()
            if self._journal is None:
                conn = sqlite3.connect(
                    str(self.db_path), timeout=10.0, isolation_level=None, check_same_thread=False
                )
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=FULL")
                self._journal_conn = conn
                self._journal = SQLiteExecutionStore.from_connection(conn)
            journal = self._journal
        if not self._journal_ready:
            await journal.init_db()
            self._journal_ready = True
        result: ExecutionStore = journal
        return result

    def _get_sync(self, workflow_run_id: str) -> WorkflowCheckpoint | None:
        with self._lock:
            return self._read_checkpoint(self._ensure_open(), workflow_run_id)

    @staticmethod
    def _read_checkpoint(
        conn: sqlite3.Connection, workflow_run_id: str
    ) -> WorkflowCheckpoint | None:
        cursor = conn.execute(
            """
            SELECT spec_version, spec_hash, current_node_id, state_json,
                   step_count, status, approval_json, error, updated_at,
                   budget_json, interrupt_json, meta_json, workspace_id,
                   effect_json, revision, workflow_version_json
            FROM nexura_workflow_checkpoints
            WHERE workflow_run_id = ?
            """,
            (workflow_run_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None

        (
            spec_version,
            spec_hash,
            current_node_id,
            state_json,
            step_count,
            status,
            approval_json,
            error,
            updated_at,
            budget_json,
            interrupt_json,
            meta_json,
            owner,
            effect_json,
            revision,
            workflow_version_json,
        ) = row

        state = json.loads(state_json)
        pending = (
            PendingApproval.from_dict(json.loads(approval_json))
            if approval_json is not None
            else None
        )
        budget_state = json.loads(budget_json) if budget_json is not None else None

        return WorkflowCheckpoint(
            workflow_run_id=workflow_run_id,
            spec_version=spec_version,
            spec_hash=spec_hash,
            current_node_id=current_node_id,
            state=state,
            step_count=step_count,
            status=status,
            pending_approval=pending,
            error=error,
            updated_at=updated_at,
            budget_state=budget_state,
            interrupt=json.loads(interrupt_json) if interrupt_json else None,
            forked_from=json.loads(meta_json) if meta_json else None,
            workspace_id=owner,
            pending_effect=json.loads(effect_json) if effect_json else None,
            revision=int(revision or 0),
            workflow_version=json.loads(workflow_version_json) if workflow_version_json else None,
        )

    def _list_children_sync(self, parent_id: str) -> list[WorkflowCheckpoint]:
        escaped = parent_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self._lock:
            conn = self._ensure_open()
            ids = [
                row[0]
                for row in conn.execute(
                    "SELECT workflow_run_id FROM nexura_workflow_checkpoints "
                    "WHERE workflow_run_id LIKE ? ESCAPE '\\'",
                    (f"{escaped}/%",),
                ).fetchall()
                if row[0].startswith(f"{parent_id}/")  # LIKE ignores ASCII case
            ]
            ids.sort(key=_child_sort_key)
            children = [self._read_checkpoint(conn, run_id) for run_id in ids]
        return [cp for cp in children if cp is not None]

    def _history_sync(self, workflow_run_id: str) -> list[WorkflowCheckpoint]:
        with self._lock:
            conn = self._ensure_open()
            rows = conn.execute(
                "SELECT checkpoint_json FROM nexura_workflow_checkpoint_history "
                "WHERE workflow_run_id = ? ORDER BY seq",
                (workflow_run_id,),
            ).fetchall()
        return [checkpoint_from_dict(json.loads(row[0])) for row in rows]

    def _save_sync(self, checkpoint: WorkflowCheckpoint, create: bool = False) -> bool:
        with self._lock:
            conn = self._ensure_open()
            state_json = json.dumps(checkpoint.state, ensure_ascii=False)
            approval_id = _pending_id(checkpoint)
            interrupt_json = (
                json.dumps(checkpoint.interrupt, ensure_ascii=False)
                if checkpoint.interrupt is not None
                else None
            )
            forked_from = checkpoint.forked_from
            owner = checkpoint.workspace_id
            if forked_from is None or owner is None:
                row = conn.execute(
                    "SELECT meta_json, workspace_id FROM nexura_workflow_checkpoints "
                    "WHERE workflow_run_id = ?",
                    (checkpoint.workflow_run_id,),
                ).fetchone()
                if forked_from is None:
                    forked_from = json.loads(row[0]) if row and row[0] else None
                if owner is None:
                    owner = row[1] if row else None
            meta_json = json.dumps(forked_from, ensure_ascii=False) if forked_from else None
            history_cp = copy.copy(checkpoint)
            history_cp.forked_from = forked_from
            history_cp.workspace_id = owner
            history_cp.updated_at = time.time()
            approval_json = (
                json.dumps(checkpoint.pending_approval.to_dict(), ensure_ascii=False)
                if checkpoint.pending_approval is not None
                else None
            )
            budget_json = (
                json.dumps(checkpoint.budget_state, ensure_ascii=False)
                if checkpoint.budget_state is not None
                else None
            )
            effect_json = (
                json.dumps(checkpoint.pending_effect, ensure_ascii=False)
                if checkpoint.pending_effect is not None
                else None
            )
            expected = None if create else checkpoint.revision
            insert = """
                    INSERT INTO nexura_workflow_checkpoints (
                        workflow_run_id, spec_version, spec_hash, current_node_id,
                        state_json, step_count, status, approval_id, approval_json,
                        error, updated_at, budget_json, interrupt_json, meta_json,
                        workspace_id, effect_json, revision, workflow_version_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
            try:
                with conn:
                    written = conn.execute(
                        insert
                        if create
                        else insert
                        + """
                    ON CONFLICT(workflow_run_id) DO UPDATE SET
                        spec_version = excluded.spec_version,
                        spec_hash = excluded.spec_hash,
                        current_node_id = excluded.current_node_id,
                        state_json = excluded.state_json,
                        step_count = excluded.step_count,
                        status = excluded.status,
                        approval_id = excluded.approval_id,
                        approval_json = excluded.approval_json,
                        error = excluded.error,
                        updated_at = excluded.updated_at,
                        budget_json = excluded.budget_json,
                        interrupt_json = excluded.interrupt_json,
                        meta_json = excluded.meta_json,
                        workspace_id = excluded.workspace_id,
                        effect_json = excluded.effect_json,
                        workflow_version_json = excluded.workflow_version_json,
                        revision = nexura_workflow_checkpoints.revision + 1
                    WHERE ? IS NULL OR nexura_workflow_checkpoints.revision = ?
                    """,
                        (
                            checkpoint.workflow_run_id,
                            checkpoint.spec_version,
                            checkpoint.spec_hash,
                            checkpoint.current_node_id,
                            state_json,
                            checkpoint.step_count,
                            checkpoint.status,
                            approval_id,
                            approval_json,
                            checkpoint.error,
                            history_cp.updated_at,
                            budget_json,
                            interrupt_json,
                            meta_json,
                            owner,
                            effect_json,
                            1 if expected is None else expected + 1,
                            json.dumps(checkpoint.workflow_version),
                        )
                        + (() if create else (expected, expected)),
                    )
                    if written.rowcount != 1:
                        row = conn.execute(
                            "SELECT revision FROM nexura_workflow_checkpoints "
                            "WHERE workflow_run_id = ?",
                            (checkpoint.workflow_run_id,),
                        ).fetchone()
                        raise _revision_conflict(
                            checkpoint.workflow_run_id,
                            int(expected or 0),
                            int(row[0] if row else 0),
                        )
                    new_revision = int(
                        conn.execute(
                            "SELECT revision FROM nexura_workflow_checkpoints "
                            "WHERE workflow_run_id = ?",
                            (checkpoint.workflow_run_id,),
                        ).fetchone()[0]
                    )
                    if not (create and _is_start_reservation(history_cp)):
                        conn.execute(
                            "INSERT INTO nexura_workflow_checkpoint_history "
                            "(workflow_run_id, checkpoint_json, created_at) VALUES (?, ?, ?)",
                            (
                                checkpoint.workflow_run_id,
                                json.dumps(checkpoint_to_dict(history_cp), ensure_ascii=False),
                                history_cp.updated_at,
                            ),
                        )
            except sqlite3.IntegrityError:
                if create:
                    return False  # another writer created this run first
                raise
            if create or expected is not None:
                checkpoint.revision = new_revision
            return True

    def _list_incomplete_sync(self, limit: int, workspace_id: str | None) -> list[str]:
        with self._lock:
            rows = (
                self._ensure_open()
                .execute(
                    "SELECT workflow_run_id FROM nexura_workflow_checkpoints "
                    "WHERE status = 'running' AND instr(workflow_run_id, '/') = 0 "
                    "AND (? IS NULL OR workspace_id = ?) "
                    "ORDER BY updated_at, workflow_run_id LIMIT ?",
                    (workspace_id, workspace_id, max(int(limit), 0)),
                )
                .fetchall()
            )
        return [str(row[0]) for row in rows]

    def _claim_approval_sync(self, workflow_run_id: str, approval_id: str) -> bool:
        with self._lock:
            conn = self._ensure_open()
            with conn:
                now = time.time()
                # One UPDATE: a pending approval past its stored expires_at is never claimed.
                cursor = conn.execute(
                    """
                    UPDATE nexura_workflow_checkpoints
                    SET status = 'running', updated_at = ?, revision = revision + 1
                    WHERE workflow_run_id = ? AND status = 'paused' AND approval_id = ?
                      AND (
                        approval_json IS NULL
                        OR json_extract(approval_json, '$.expires_at') IS NULL
                        OR json_extract(approval_json, '$.expires_at') >= ?
                      )
                    """,
                    (now, workflow_run_id, approval_id, now),
                )
                return cursor.rowcount == 1

    def _close_sync(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                if self._conn is not None:
                    self._conn.close()
                    self._conn = None
        with self._journal_lock:
            conn, self._journal_conn, self._journal = self._journal_conn, None, None
            if conn is not None:
                conn.close()

    async def get(self, workflow_run_id: str) -> WorkflowCheckpoint | None:
        """The latest checkpoint of a run, or ``None`` if it does not exist."""
        return await asyncio.to_thread(self._get_sync, workflow_run_id)

    async def run_identities(self, run_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Read a page's active and starting checkpoint identities in one query."""
        if not run_ids:
            return {}

        def read() -> dict[str, dict[str, Any]]:
            with self._lock:
                rows = (
                    self._ensure_open()
                    .execute(
                        "SELECT c.workflow_run_id, c.spec_hash, c.workflow_version_json, "
                        "(SELECT json_extract(h.checkpoint_json, '$.workflow_version') "
                        "FROM nexura_workflow_checkpoint_history h "
                        "WHERE h.workflow_run_id = c.workflow_run_id ORDER BY h.seq LIMIT 1) "
                        "FROM nexura_workflow_checkpoints c WHERE c.workflow_run_id IN ("
                        + ",".join("?" for _ in run_ids)
                        + ")",
                        run_ids,
                    )
                    .fetchall()
                )
            return {
                run_id: {
                    "definition_digest": digest,
                    "workflow_version": json.loads(version) if version else None,
                    "started_workflow_version": started,
                }
                for run_id, digest, version, started in rows
            }

        return await asyncio.to_thread(read)

    async def save(self, checkpoint: WorkflowCheckpoint) -> None:
        """Store a checkpoint as the run's latest and append it to its history."""
        await asyncio.to_thread(self._save_sync, checkpoint)

    async def create(self, checkpoint: WorkflowCheckpoint) -> bool:
        """Save the first checkpoint of a new run; False if the run id is taken."""
        return await asyncio.to_thread(self._save_sync, checkpoint, True)

    async def history(self, workflow_run_id: str) -> list[WorkflowCheckpoint]:
        """Every checkpoint saved for the run, oldest first."""
        return await asyncio.to_thread(self._history_sync, workflow_run_id)

    async def list_children(self, parent_id: str) -> list[WorkflowCheckpoint]:
        """Latest checkpoint of every child run (ids starting ``<parent_id>/``), by id."""
        return await asyncio.to_thread(self._list_children_sync, parent_id)

    async def list_incomplete(
        self, limit: int = 100, *, workspace_id: str | None = None
    ) -> list[str]:
        """Ids of top-level runs whose latest checkpoint is ``running``, oldest update first.

        The candidates for ``WorkflowEngine.recover`` (a run is only taken over once its
        journal lease is free or expired). Sub-workflow children are left out: their
        parent's ``recover`` continues them. ``workspace_id`` narrows the list.
        """
        return await asyncio.to_thread(self._list_incomplete_sync, limit, workspace_id)

    async def claim_approval(self, workflow_run_id: str, approval_id: str) -> bool:
        """Atomically claim a pending approval or interrupt; ``False`` if already claimed."""
        return await asyncio.to_thread(self._claim_approval_sync, workflow_run_id, approval_id)

    async def close(self) -> None:
        """Release resources held by the store."""
        await asyncio.to_thread(self._close_sync)

    def close_sync(self) -> None:
        """Close the store from synchronous code."""
        self._close_sync()

    async def __aenter__(self) -> SQLiteWorkflowStore:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    def __enter__(self) -> SQLiteWorkflowStore:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close_sync()


def _parse_handler_output(raw: Any) -> tuple[dict[str, Any], str | None]:
    if isinstance(raw, HandlerOutput):
        return raw.state_update, raw.branch
    if isinstance(raw, dict):
        update = dict(raw)
        branch = update.pop("_branch", None)
        return update, branch
    if isinstance(raw, tuple) and len(raw) == 2 and isinstance(raw[0], dict):
        return raw[0], raw[1]
    if raw is None:
        return {}, None
    raise WorkflowExecutionError(
        node_id="unknown",
        message=f"Handler returned invalid output type: {type(raw).__name__} (expected dict).",
    )


def _validate_json_serializable(data: Any, node_id: str) -> None:
    try:
        json.dumps(data, ensure_ascii=False)
    except Exception as exc:
        raise WorkflowExecutionError(
            node_id=node_id,
            message=f"State update from node '{node_id}' is not JSON-serializable: {exc}",
            original_error=exc,
        ) from exc


def _key_count(value: Any) -> int:
    if isinstance(value, dict):
        return len(value) + sum(_key_count(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return sum(_key_count(v) for v in value)
    return 0


def _as_json_update(update: dict[str, Any], node_id: str) -> dict[str, Any]:
    """A ``resume(state_update=...)`` exactly as JSON would carry it (as over HTTP).

    Non-string keys become strings, the same as a stored checkpoint would make them, so the
    approval binding compares what the node will actually read. Keys that collide once
    converted (``{1: x, "1": y}``) are refused. Runs before the approval is claimed.
    """
    _validate_json_serializable(update, node_id)
    normalized: dict[str, Any] = json.loads(json.dumps(update, ensure_ascii=False))
    if _key_count(normalized) != _key_count(update):
        raise WorkflowValidationError(
            f"state_update for node '{node_id}' has keys that collide as JSON strings "
            '(for example 1 and "1"); use string keys.'
        )
    return normalized


def _redacted(value: Any) -> Any:
    from nexura_runtime.observability.run_diagnostics import redact_run_content

    return redact_run_content(value)


def _node_span(node: WorkflowNode, step: int) -> Any:
    return tracing.start(
        f"workflow.node {node.id}",
        {
            "nexura.workflow.node.id": node.id,
            "nexura.workflow.node.step": step,
            "nexura.workflow.node.kind": (
                "subgraph"
                if node.subgraph is not None
                else "parallel"
                if node.parallel
                else "map"
                if node.map_over is not None
                else "step"
            ),
        },
    )


@contextlib.contextmanager
def _child_span(name: str, attributes: dict[str, Any]) -> Any:
    """A span for one parallel branch or map item, current for the block (opt-in tracing)."""
    span = tracing.start(name, attributes)
    if span is tracing.NOOP:
        yield span
        return
    span.attach()
    try:
        yield span
    except Exception as exc:
        span.fail(exc)
        raise
    finally:
        span.end()


async def _gather_limited(factories: list[Callable[[], Any]], limit: int) -> list[dict[str, Any]]:
    """Run coroutine factories concurrently (at most ``limit`` at once), in order.

    The first failure cancels the rest and is raised as itself, not as a group. A factory
    still waiting for its turn when another one failed does not start.
    """
    gate = asyncio.Semaphore(limit)
    stopped = False

    async def guarded(factory: Callable[[], Any]) -> dict[str, Any]:
        nonlocal stopped
        async with gate:
            if stopped:
                raise asyncio.CancelledError
            try:
                result: dict[str, Any] = await factory()
            except BaseException:
                stopped = True
                raise
            return result

    try:
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(guarded(factory)) for factory in factories]
    except BaseExceptionGroup as failed:
        raise failed.exceptions[0] from None
    return [task.result() for task in tasks]


class WorkflowEngine:
    """Executes workflows deterministically with checkpoints and approval gates."""

    def __init__(
        self,
        spec: WorkflowSpec,
        registry: ModuleRegistry,
        *,
        store: WorkflowCheckpointStore | None = None,
        max_steps: int = 50,
        timeout: float | None = None,
        workspace_id: str = "default",
        budget: Any | None = None,
        event_sink: Any | None = None,
        artifact_store: Any | None = None,
        credential_resolver: Any | None = None,
        approval_required_handlers: frozenset[str] | set[str] | None = None,
        approval_ttl: float = 3600.0,
        max_map_writes_per_approval: int = 50,
        policy_version: str = "1.0.0",
        lease_ttl: float = 30.0,
    ) -> None:
        self.approval_required_handlers = (
            frozenset(approval_required_handlers)
            if approval_required_handlers is not None
            else frozenset()
        )
        spec.validate(registry, approval_required_handlers=self.approval_required_handlers)
        if not isinstance(max_steps, int) or max_steps < 1:
            raise WorkflowValidationError(f"max_steps must be an integer >= 1, got {max_steps!r}.")
        if timeout is not None and (not isinstance(timeout, (int, float)) or not (timeout > 0)):
            raise WorkflowValidationError(f"timeout must be a positive number, got {timeout!r}.")
        if (
            isinstance(lease_ttl, bool)
            or not isinstance(lease_ttl, (int, float))
            or not (0 < lease_ttl < float("inf"))
        ):
            raise WorkflowValidationError(
                f"lease_ttl must be a positive number of seconds, got {lease_ttl!r}."
            )

        self.spec = spec
        self.registry = registry
        self.store = store
        self.max_steps = max_steps
        self.timeout = float(timeout) if timeout is not None else None
        self.workspace_id = workspace_id
        self.budget = budget
        self.event_sink = event_sink
        self.artifact_store = artifact_store
        self.credential_resolver = credential_resolver
        self.approval_ttl = float(approval_ttl) if approval_ttl is not None else 3600.0
        self.max_map_writes_per_approval = int(max_map_writes_per_approval)
        self.policy_version = policy_version
        # Run lease of a journaled run, renewed every lease_ttl / 3 (Execution Contract v1).
        self.lease_ttl = float(lease_ttl)
        # Journal runs on stores that offer one; a sub-workflow child keeps its own journal
        # (A6 S7) with ``parent_run_id`` set to the run that started it.
        self._journal_enabled = True
        self._parent_run_id: str | None = None
        # Set on engines running a sub-workflow per map_over item: those cannot pause.
        self._pause_forbidden: str | None = None
        # State keys written by steps this engine ran (sub-workflow outputs).
        self._written: set[str] = set()

    async def _save(self, checkpoint: WorkflowCheckpoint) -> None:
        checkpoint.workflow_version = self.spec.version
        if checkpoint.workspace_id is None:
            checkpoint.workspace_id = self.workspace_id
        assert self.store is not None
        journal = self._journal_for(checkpoint.workflow_run_id)
        if checkpoint.status in ("completed", "failed", "rejected"):
            # Every step is finished: end open reservations before the terminal commit, so
            # a crash between the two cannot leak them (A6 S11 review m1).
            await self._settle_reservations(checkpoint.workflow_run_id, journal, "terminal")
        if journal is None:
            await self.store.save(checkpoint)
            return
        # The journal moves first (fenced by the lease token), then the checkpoint (fenced
        # by its revision): a stale owner is refused before it can write either.
        await self._journal_sync(journal, checkpoint)
        await self._settle_deferred(checkpoint.workflow_run_id, journal)
        checkpoint.revision = journal.cp_revision
        await self.store.save(checkpoint)
        journal.cp_revision = checkpoint.revision

    # ------------------------------------------------------------------ execution journal

    def _journal_for(self, workflow_run_id: str) -> WorkflowJournal | None:
        journal = _JOURNAL.get()
        return journal if journal is not None and journal.run_id == workflow_run_id else None

    def _journaled_store(self) -> bool:
        return self._journal_enabled and journal_capable(self.store)

    async def _open_journal(self, workflow_run_id: str) -> WorkflowJournal:
        return await WorkflowJournal.open(
            self.store,
            self.workspace_id or "default",
            workflow_run_id,
            self.spec.canonical_hash(),
            self.lease_ttl,
            self._parent_run_id,
        )

    async def _journal_sync(self, journal: WorkflowJournal, checkpoint: WorkflowCheckpoint) -> None:
        """Mirror a paused or finished checkpoint's status into the journal first."""
        if journal.status in _JOURNAL_TERMINAL:
            return
        cursor = checkpoint.current_node_id
        if checkpoint.status == "paused":
            if journal.status != "paused":
                await journal.pause(checkpoint.pause_reason or "approval", cursor)
        elif checkpoint.status in ("failed", "rejected"):
            await journal.finish("failed", cursor, reason=checkpoint.error)
        elif checkpoint.status == "completed":
            await journal.finish("completed", cursor)

    async def _journaled(
        self,
        journal: WorkflowJournal | None,
        run: Callable[[], Awaitable[WorkflowRunResult]],
        *,
        run_id: str | None = None,
        takeover: bool = False,
    ) -> WorkflowRunResult:
        """Run ``run`` with ``journal`` as this run's journal; give its lease back after.

        A6 S11: on a ``takeover`` (resume, recover) the run's budget policy is restored and
        its open reservations are ended before any step runs; when the run reaches a
        terminal status its open reservations are ended too (design §2.7).
        """
        if journal is None:
            if self.store is not None:
                result = await run()  # terminal saves end open reservations (_save)
                result.workflow_version = self.spec.version
                result.definition_digest = self.spec.canonical_hash()
                return result
            try:
                result = await run()
            except Exception:
                await self._settle_reservations(run_id, None, "terminal")
                raise
            if result.status in ("completed", "failed", "rejected"):
                await self._settle_reservations(run_id, None, "terminal")
            result.workflow_version = self.spec.version
            result.definition_digest = self.spec.canonical_hash()
            return result
        token = _JOURNAL.set(journal)
        journal.start_heartbeat()
        try:
            if takeover and journal.held:
                self._restore_budget(journal)
                await self._settle_reservations(journal.run_id, journal, "takeover")
            try:
                result = await run()
                result.workflow_version = self.spec.version
                result.definition_digest = self.spec.canonical_hash()
                return result
            except Exception:
                if journal.status in _JOURNAL_TERMINAL and journal.lost is None:
                    # A terminal commit made outside a checkpoint save (none today): end them.
                    await self._settle_reservations(journal.run_id, journal, "terminal")
                raise
        finally:
            _JOURNAL.reset(token)
            await journal.close()

    def _restore_budget(self, journal: WorkflowJournal) -> None:
        """Attach the run's persisted budget policy before any step runs (A6 S11)."""
        self._resolve_budget(journal.run_id, journal.state.get("budget_scope"))

    async def _precheck_budget(self, workflow_run_id: str) -> None:
        """Resume paths: refuse a missing or unknown budget before the approval is claimed.

        Reads the run's journal without taking it, so a refusal consumes nothing.
        """
        if not self._journaled_store():
            return
        journal = await self._open_journal(workflow_run_id)
        snapshot = await journal.snapshot()
        if snapshot is not None:
            self._resolve_budget(workflow_run_id, snapshot.run.state.get("budget_scope"))

    def _resolve_budget(self, workflow_run_id: str, recorded: Any) -> None:
        """The run continues under the budget scope recorded in its journal.

        - No budget attached: refused (``WorkflowError``), fail closed.
        - A ``RunBudget`` of the recorded scope: replaced by the persisted policy
          (``manager.get_run_budget(scope)``).
        - A ``RunBudget`` of another scope: the recorded scope's persisted policy from the
          same ledger is used instead, with a ``RuntimeWarning``; refused when that ledger
          has no policy for the recorded scope.
        - A combined budget containing the recorded scope: kept. One without it: the
          recorded scope's persisted policy from the ledger of one of its ``RunBudget`` objects
          is enforced alongside it (``combine_budgets``), with a ``RuntimeWarning``; when
          no such policy is found, a ``RuntimeWarning`` says the recorded policy is not
          enforced.
        - A custom budget object: kept, with that ``RuntimeWarning``.
        """
        from nexura_agents.usage.manager import RunBudget
        from nexura_agents.usage.recovery import restore_run_budget

        if not isinstance(recorded, dict):
            self.budget = restore_run_budget(self.budget)
            return
        scope = f"'{recorded.get('run_id')}' (workspace '{recorded.get('workspace_id')}')"
        if self.budget is None:
            raise WorkflowError(
                f"Workflow run '{workflow_run_id}' was started under budget scope {scope}; "
                "attach that budget, WorkflowEngine(budget=manager.get_run_budget("
                f"'{recorded.get('run_id')}')), and continue it again. Nothing was claimed "
                "or run."
            )
        if not isinstance(self.budget, RunBudget):
            self._combine_recorded_budget(workflow_run_id, recorded, scope)
            return
        mine = {"workspace_id": self.budget.workspace_id, "run_id": self.budget.run_id}
        if mine == recorded:
            self.budget = restore_run_budget(self.budget)
            return
        restored = None
        with contextlib.suppress(Exception):
            restored = self.budget.manager.get_run_budget(str(recorded.get("run_id")))
        if not isinstance(restored, RunBudget) or restored.workspace_id != recorded.get(
            "workspace_id"
        ):
            raise WorkflowError(
                f"Workflow run '{workflow_run_id}' was started under budget scope {scope}, "
                f"but the engine's budget is scope '{mine['run_id']}' and its ledger has no "
                "policy for the recorded scope. Attach the recorded scope's budget. Nothing "
                "was claimed or run."
            )
        import warnings

        warnings.warn(
            f"Workflow run '{workflow_run_id}' was started under budget scope {scope}; it "
            f"continues under that scope's persisted policy, not the engine's scope "
            f"'{mine['run_id']}'.",
            RuntimeWarning,
            stacklevel=3,
        )
        self.budget = restored

    def _combine_recorded_budget(
        self, workflow_run_id: str, recorded: dict[str, Any], scope: str
    ) -> None:
        """A combined or custom budget without the recorded scope (round 2, minor 1)."""
        import warnings

        from nexura_agents.usage import admission
        from nexura_agents.usage.manager import RunBudget
        from nexura_agents.usage.recovery import budget_scope

        inner = admission._budgets(self.budget)
        if any(budget_scope(b) == recorded for b in inner):
            return
        restored = None
        for candidate in inner:
            if not isinstance(candidate, RunBudget):
                continue
            with contextlib.suppress(Exception):
                found = candidate.manager.get_run_budget(str(recorded.get("run_id")))
                if isinstance(found, RunBudget) and found.workspace_id == recorded.get(
                    "workspace_id"
                ):
                    restored = found
                    break
        if restored is not None:
            self.budget = admission.combine_budgets(self.budget, restored)
            warnings.warn(
                f"Workflow run '{workflow_run_id}' was started under budget scope {scope}; "
                "the attached budget does not contain it, so that scope's persisted policy "
                "is enforced alongside it.",
                RuntimeWarning,
                stacklevel=4,
            )
            return
        warnings.warn(
            f"Workflow run '{workflow_run_id}' was started under budget scope {scope}; the "
            "attached budget does not contain it and its policy was not found, so it is not "
            "enforced.",
            RuntimeWarning,
            stacklevel=4,
        )

    async def _settle_deferred(self, run_id: str, journal: WorkflowJournal) -> None:
        """A deferred terminal sweep, once the fenced terminal commit has succeeded.

        If that commit failed, the journal is not terminal: recovery ends the reservations
        (round 4, F4-m1).
        """
        deferred, journal.deferred_sweep = journal.deferred_sweep, None
        if deferred is not None and journal.status in _JOURNAL_TERMINAL:
            await self._settle_reservations(run_id, journal, "terminal", proven=deferred)

    async def _settle_reservations(
        self,
        run_id: str | None,
        journal: WorkflowJournal | None,
        context: str,
        *,
        proven: tuple[int | None, float] | None = None,
    ) -> None:
        """End the run's open budget reservations: settle as unknown or release (§2.7).

        Errors propagate on a takeover (nothing runs while the ledger cannot be read); at
        a terminal status they are reported as a warning.

        A terminal sweep of a journaled run is fenced (round 2, R2-M1): the lease is
        renewed first (a stale owner stops here), and only reservations made under a
        lease token no newer than this owner's are ended. A takeover ends them all.
        """
        if self.budget is None or run_id is None:
            return
        from nexura_agents.usage.recovery import budget_scope, settle_run_reservations_async

        workspace = self.workspace_id or "default"
        own_scope = budget_scope(self.budget) == {"workspace_id": workspace, "run_id": run_id}
        max_token: int | None = None
        confirmed_at: float | None = None
        if proven is not None:
            # After a successful fenced terminal commit (round 4, F4-m1): it proved this
            # owner held the run under this token up to that moment.
            max_token, confirmed_at = proven
        elif context == "terminal" and journal is not None:
            if journal.lease is None:
                return  # not ours (any more): its owner or a takeover settles them
            confirmed = False
            for attempt in (1, 2):  # a transient store error is retried once (F4-m1)
                try:
                    await journal.renew()
                    confirmed = True
                    break
                except LeaseLostError:
                    return
                except Exception as exc:
                    # A transient store error must not fail a finished run (round 3).
                    import warnings

                    warnings.warn(
                        f"Workflow run '{run_id}' finished, but its run lease could not be "
                        f"confirmed ({exc}); "
                        + (
                            "retrying once."
                            if attempt == 1
                            else "its open budget reservations are settled after the "
                            "terminal commit, or left for recovery if that fails."
                        ),
                        RuntimeWarning,
                        stacklevel=2,
                    )
            if not confirmed:
                journal.deferred_sweep = (journal.fencing_token, time.time())
                return
            max_token = journal.fencing_token
            # Unlinked reservations older than this are ours: a newer owner can reserve
            # only after this lease expires (residual: an owner frozen here longer than its
            # ttl together with host clock skew larger than the ttl).
            confirmed_at = time.time()
        try:
            await settle_run_reservations_async(
                [self.budget],
                workspace_id=workspace,
                run_id=run_id,
                steps=journal.steps if journal is not None else None,
                # The service's convention: the run's budget scope is its run id, so an
                # unlinked reservation of that scope is this run's.
                include_unlinked=own_scope,
                context=context,
                max_fencing_token=max_token,
                unlinked_before=confirmed_at,
            )
        except Exception as exc:
            if context != "terminal":
                raise
            import warnings

            warnings.warn(
                f"Workflow run '{run_id}' finished, but its open budget reservations could "
                f"not be settled: {exc}",
                RuntimeWarning,
                stacklevel=2,
            )

    def _dispatch_scope(self, workflow_run_id: str, key: StepKey) -> Any:
        """Link budget reservations made by this step to it (``tracked``: intent written)."""
        journal = self._journal_for(workflow_run_id)
        step = journal.steps.get(str(key)) if journal is not None else None
        return _linkage.enter_dispatch_scope(
            self.workspace_id or "default",
            workflow_run_id,
            str(key),
            tracked=step is not None and step.intent is not None,
            fencing_token=journal.fencing_token if journal is not None else None,
        )

    async def _take_journal(self, workflow_run_id: str) -> WorkflowJournal | None:
        """Claim a stored run's lease (resume, recover); None for stores without a journal.

        The returned journal has ``owner`` unset while another live worker holds the lease.
        """
        if not self._journaled_store():
            return None
        from nexura_agents.durable import new_lease_owner

        journal = await self._open_journal(workflow_run_id)
        snapshot = await journal.snapshot()
        if snapshot is not None:
            active = snapshot.run.state.get("definition_digest", snapshot.run.definition_digest)
            if active != self.spec.canonical_hash():
                raise WorkflowSpecMismatchError(
                    expected_hash=str(active), actual_hash=self.spec.canonical_hash()
                )
            # The store row pins the original definition; active identity is fenced state.
            journal.digest = snapshot.run.definition_digest
        if not await journal.take(new_lease_owner()):
            journal.owner = None
        else:
            # take() reloads state under the lease. The preflight snapshot may have
            # preceded a migration; never dispatch using that stale definition.
            active = journal.state.get("definition_digest", journal.digest)
            if active != self.spec.canonical_hash():
                await journal.close()
                raise WorkflowSpecMismatchError(str(active), self.spec.canonical_hash())
        return journal

    async def _take_claimed_journal(self, workflow_run_id: str) -> WorkflowJournal | None:
        """The lease of a run whose approval (or interrupt) this caller just claimed.

        Nobody else should hold a paused run's lease except briefly (an effect being
        resolved), so a busy lease is retried for a few seconds. If it stays busy, the run
        is left claimed but not dispatched, which ``recover()`` continues.
        """
        for _ in range(60):
            journal = await self._take_journal(workflow_run_id)
            if journal is None or journal.owner is not None:
                return journal
            await journal.close()
            if journal.status in _JOURNAL_TERMINAL:
                break
            await asyncio.sleep(0.05)
        raise WorkflowError(
            f"Workflow run '{workflow_run_id}' was claimed but another worker holds its run "
            "lease; nothing was dispatched. Call recover() once that lease is free."
        )

    def _node_effect(
        self, node: WorkflowNode, state: dict[str, Any]
    ) -> tuple[EffectClass, Callable[..., Any] | None]:
        """The recovery promise of a node visit (and its reconcile probe, if any).

        Function handlers: the class declared with ``register(..., effect=)``, else
        ``non_idempotent`` (founder decision 1). Modules: a declaration here, else the
        manifest's ``effect_policy``; a module whose manifest leaves it unset but declares
        write operations counts as ``non_idempotent`` when this call writes. For a
        composite step (parallel, map, sub-workflow) this is the weakest promise of its
        parts; each part is journaled on its own (A6 S7), see :meth:`_before_dispatch`.
        """
        if node.subgraph is not None:
            return _combine_effects(self._spec_effects(node.subgraph)), None
        if node.parallel:
            classes = [self._handler_effect(node, name, None, state)[0] for name in node.parallel]
            return _combine_effects(classes), None
        effect, reconcile = self._handler_effect(node, node.handler, node.module_id, state)
        if node.map_over is not None and effect == EffectClass.RECONCILABLE:
            return EffectClass.NON_IDEMPOTENT, None  # each item is probed on its own
        return effect, reconcile

    @staticmethod
    def _is_composite(node: WorkflowNode) -> bool:
        return bool(node.parallel) or node.map_over is not None or node.subgraph is not None

    def _spec_effects(self, spec: WorkflowSpec) -> list[EffectClass]:
        classes: list[EffectClass] = []
        for node in spec.nodes.values():
            if node.subgraph is not None:
                classes.extend(self._spec_effects(node.subgraph))
            else:
                for name in node.parallel or [node.handler]:
                    module_id = None if node.parallel else node.module_id
                    classes.append(self._handler_effect(node, name, module_id, {})[0])
        return classes

    def _handler_effect(
        self, node: WorkflowNode, name: str, module_id: str | None, state: dict[str, Any]
    ) -> tuple[EffectClass, Callable[..., Any] | None]:
        declared = self.registry.declared_effect(name)
        if declared is not None:
            return declared
        try:
            obj = self._resolve_handler(name, module_id, node.module_version)
        except Exception:
            return EffectClass.NON_IDEMPOTENT, None
        manifest = getattr(obj, "manifest", None)
        if manifest is not None:
            fields_set = getattr(manifest, "model_fields_set", None)
            writes = bool(
                getattr(manifest, "write_operations", None) or getattr(manifest, "always_write", 0)
            )
            if fields_set is not None and "effect_policy" not in fields_set and writes:
                gated = self._write_operation_gated(node, state)
                return (EffectClass.NON_IDEMPOTENT if gated else EffectClass.PURE), None
            try:
                return effect_class_from_policy(getattr(manifest, "effect_policy", None)), None
            except ValueError:
                return EffectClass.NON_IDEMPOTENT, None
        attribute = getattr(obj, "effect_policy", None)
        if isinstance(attribute, str):
            with contextlib.suppress(WorkflowValidationError):
                effect = _coerce_effect(attribute, name)
                if effect is not None and effect != EffectClass.RECONCILABLE:
                    return effect, None
        return EffectClass.NON_IDEMPOTENT, None

    async def _before_dispatch(
        self,
        workflow_run_id: str,
        node: WorkflowNode,
        state: dict[str, Any],
        step_count: int,
        approval_id: str | None,
    ) -> _StepIdentity:
        """The step identity for a node visit; for a journaled non-pure node, its intent.

        The intent, its ``pending`` effect and the approval it runs under (recorded as
        consumed) are one fenced journal transaction, written just before the handler
        runs ("claim for dispatch"). A pure node writes no intent; the lease is checked.

        A composite step with a non-pure part writes a container intent of class ``pure``
        (running the step again is safe: each branch, item or sub-workflow child records
        its own intent and outcome and decides for itself after a crash); it carries the
        approval the step runs under.
        """
        journal = self._journal_for(workflow_run_id)
        if journal is None:
            return _StepIdentity(
                ExecutionId(self.workspace_id or "default", workflow_run_id),
                StepKey.workflow(node.id, step_count),
                approval_id=approval_id,
            )
        key = journal.dispatch_key(node.id, step_count)
        effect, _ = self._node_effect(node, state)
        if self._is_composite(node) and effect != EffectClass.PURE:
            effect = EffectClass.PURE
            await journal.record_intent(
                StepIntent(
                    key,
                    self._binding_digest(node, state) or "",
                    default_idempotency_key(journal.eid, key),
                    effect,
                    approval_id=approval_id,
                ),
                node.id,
            )
        elif effect != EffectClass.PURE:
            await journal.record_intent(
                StepIntent(
                    key,
                    self._binding_digest(node, state) or "",
                    default_idempotency_key(journal.eid, key),
                    effect,
                    approval_id=approval_id,
                ),
                node.id,
            )
        else:
            journal.check()
        return _StepIdentity(journal.eid, key, journal.fencing_token, approval_id)

    def _check_owner(self, checkpoint: WorkflowCheckpoint) -> None:
        owner = checkpoint.workspace_id
        if owner is not None and owner != self.workspace_id:
            from nexura_runtime.service.auth.errors import CrossWorkspaceAccessError

            raise CrossWorkspaceAccessError(
                f"Workflow run '{checkpoint.workflow_run_id}' belongs to another workspace."
            )

    def _progress(self, kind: RunEventType, **data: Any) -> None:
        _progress(kind, workflow=self.spec.name, **data)

    def stream(
        self,
        initial_state: dict[str, Any] | None = None,
        *,
        workflow_run_id: str | None = None,
    ) -> AsyncGenerator[RunEvent, None]:
        """Run the workflow and yield progress events as it goes.

        Yields ``WORKFLOW_STARTED``, then ``NODE_STARTED`` and ``NODE_COMPLETED`` (or
        ``NODE_PAUSED`` / ``NODE_FAILED``) for each node, and last ``WORKFLOW_COMPLETED`` or
        ``WORKFLOW_PAUSED`` with the ``WorkflowRunResult`` that :meth:`run` returns in
        ``data["result"]``. If the run fails, ``WORKFLOW_FAILED`` is yielded and the error is
        raised. Nodes of sub-workflows report too, tagged with their own ``data["workflow"]``.
        Closing the stream early cancels the run.
        """
        run_id = workflow_run_id or str(uuid.uuid4())
        return self._stream(lambda: self.run(initial_state, workflow_run_id=run_id), run_id)

    def resume_stream(
        self, workflow_run_id: str, approval_id: str, **kwargs: Any
    ) -> AsyncGenerator[RunEvent, None]:
        """Event-stream form of :meth:`resume`; takes the same arguments."""
        return self._stream(
            lambda: self.resume(workflow_run_id, approval_id, **kwargs), workflow_run_id
        )

    async def _stream(
        self, start: Callable[[], Any], run_id: str
    ) -> AsyncGenerator[RunEvent, None]:
        queue: asyncio.Queue[RunEvent | None] = asyncio.Queue()
        stream_id = run_id
        tags = {"workflow": self.spec.name}

        def sink(kind: RunEventType, data: dict[str, Any]) -> None:
            queue.put_nowait(RunEvent(kind, stream_id, data={**tags, **data}))

        async def drive() -> WorkflowRunResult:
            _PROGRESS.set(sink)
            sink(RunEventType.WORKFLOW_STARTED, {"workflow_run_id": run_id})
            try:
                result: WorkflowRunResult = await start()
            except Exception as exc:
                sink(
                    RunEventType.WORKFLOW_FAILED,
                    {
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                        "workflow_run_id": run_id,
                    },
                )
                raise
            done = (
                RunEventType.WORKFLOW_PAUSED
                if result.status == "paused"
                else RunEventType.WORKFLOW_COMPLETED
            )
            sink(done, {"result": result, "workflow_run_id": result.workflow_run_id})
            return result

        task = asyncio.ensure_future(drive())
        task.add_done_callback(lambda _: queue.put_nowait(None))
        try:
            while (event := await queue.get()) is not None:
                yield event
            await task  # raises the run's error after WORKFLOW_FAILED was yielded
        finally:
            if not task.done():
                task.cancel()
            # Always collect the task, so a failure nobody read is not reported as unretrieved.
            await asyncio.gather(task, return_exceptions=True)

    async def run(
        self,
        initial_state: dict[str, Any] | None = None,
        *,
        workflow_run_id: str | None = None,
    ) -> WorkflowRunResult:
        """Start a new workflow execution run."""
        state = copy.deepcopy(initial_state or {})
        _validate_json_serializable(state, "entry")

        run_id = workflow_run_id or str(uuid.uuid4())
        if self.store is not None:
            first = WorkflowCheckpoint(
                workflow_run_id=run_id,
                spec_version=self.spec.spec_version,
                spec_hash=self.spec.canonical_hash(),
                current_node_id=self.spec.entry_node,
                state=copy.deepcopy(state),
                step_count=0,
                status="running",
                workspace_id=self.workspace_id,
                workflow_version=self.spec.version,
            )
            create = getattr(self.store, "create", None)
            if create is None:
                raise WorkflowError("Starting a run requires a store with atomic create().")
            if not await create(first):
                existing_cp = await self.store.get(run_id)
                assert existing_cp is not None
                if existing_cp.status in ("completed", "failed", "rejected"):
                    raise WorkflowTerminalError(
                        f"Workflow run '{run_id}' is already in terminal state "
                        f"'{existing_cp.status}'."
                    )
                raise WorkflowError(
                    f"Workflow run '{run_id}' already exists with status '{existing_cp.status}'."
                )
            journal = await self._new_journal(run_id, first)
        else:
            journal = None

        return await self._journaled(
            journal,
            lambda: self._execute_loop(
                workflow_run_id=run_id,
                cursor=self.spec.entry_node,
                current_state=state,
                step_count=0,
                node_already_approved=False,
                operation="run",
            ),
            run_id=run_id,
        )

    async def _new_journal(
        self, workflow_run_id: str, first: WorkflowCheckpoint
    ) -> WorkflowJournal | None:
        """Journal a run just created in the store, leased by this caller from the start."""
        if not self._journaled_store():
            return None
        from nexura_agents.durable import new_lease_owner
        from nexura_agents.execution_store import ExecutionConflictError
        from nexura_agents.usage.recovery import budget_scope

        journal = await self._open_journal(workflow_run_id)
        try:
            await journal.create(
                new_lease_owner(),
                budget_scope=budget_scope(self.budget),
                definition=self.spec.to_dict(),
            )
        except ExecutionConflictError as exc:
            raise WorkflowError(
                f"Workflow run '{workflow_run_id}' already has an execution journal; "
                "use a new run id."
            ) from exc
        journal.cp_revision = first.revision
        return journal

    async def resume(
        self,
        workflow_run_id: str,
        approval_id: str,
        *,
        decision: Literal["approve", "reject"] = "approve",
        state_update: dict[str, Any] | None = None,
        workspace_id: str | None = None,
        actor: str | None = None,
        actor_role: str | None = None,
        expected_payload_digest: str | None = None,
        continue_on_old_definition: bool = False,
    ) -> WorkflowRunResult:
        """Resume an approval-gated workflow run with exact-action bindings."""
        selected = await self._version_engine(workflow_run_id, continue_on_old_definition)
        if selected is not self:
            return await selected.resume(
                workflow_run_id,
                approval_id,
                decision=decision,
                state_update=state_update,
                workspace_id=workspace_id,
                actor=actor,
                actor_role=actor_role,
                expected_payload_digest=expected_payload_digest,
            )
        if self.store is None:
            raise WorkflowError("Cannot resume workflow without a configured checkpoint store.")

        checkpoint = await self.store.get(workflow_run_id)
        if checkpoint is None:
            raise WorkflowExecutionError(
                node_id="unknown",
                message=f"Workflow run '{workflow_run_id}' not found.",
            )

        # Enforce terminal immutability
        if checkpoint.status in ("completed", "failed", "rejected"):
            raise WorkflowTerminalError(
                f"Cannot resume workflow run '{workflow_run_id}' in terminal state "
                f"'{checkpoint.status}'."
            )

        # Enforce canonical spec binding
        current_spec_hash = self.spec.canonical_hash()
        if checkpoint.spec_hash != current_spec_hash:
            raise WorkflowSpecMismatchError(
                expected_hash=checkpoint.spec_hash,
                actual_hash=current_spec_hash,
            )

        # Verify pending approval ID
        if (
            checkpoint.status == "running"
            and checkpoint.pending_approval is not None
            and checkpoint.pending_approval.approval_id == approval_id
        ):
            # Another resume claimed this approval a moment ago and is running it.
            raise ApprovalAlreadyClaimedError(
                f"Approval '{approval_id}' for workflow '{workflow_run_id}' was already claimed."
            )
        if (
            checkpoint.status != "paused"
            or checkpoint.pending_approval is None
            or checkpoint.pending_approval.approval_id != approval_id
        ):
            raise ApprovalError(
                f"Workflow '{workflow_run_id}' is not paused for approval ID '{approval_id}'."
            )

        pending = checkpoint.pending_approval

        # 1. Enforce Workspace Binding
        effective_workspace = workspace_id or self.workspace_id
        if pending.workspace_id and pending.workspace_id != effective_workspace:
            raise WrongWorkspaceError(
                f"Approval '{approval_id}' belongs to workspace '{pending.workspace_id}', "
                f"not '{effective_workspace}'.",
                request_workspace=pending.workspace_id,
                decision_workspace=effective_workspace,
                approval_id=approval_id,
            )

        # 2. Enforce Expiration Policy. A row stored without expires_at expires
        # approval_ttl after created_at. Checked here, enforced again by the store's claim
        # and re-checked right before the node runs.
        deadline = self._approval_deadline(pending)
        if deadline is not None and time.time() > deadline:
            await self._fail_expired(checkpoint, approval_id, deadline)

        # 3. Enforce Canonical Payload Digest Binding
        if (
            expected_payload_digest is not None
            and pending.payload_digest
            and pending.payload_digest != expected_payload_digest
        ):
            raise MutatedArgumentsError(
                f"Action arguments for '{pending.node_id}' have been modified since "
                f"approval request. Expected digest '{expected_payload_digest}', "
                f"got '{pending.payload_digest}'.",
                expected_digest=expected_payload_digest,
                actual_digest=pending.payload_digest,
                approval_id=approval_id,
            )

        # 4. Enforce Actor / Role Policy
        if actor_role is not None and pending.required_role == "admin" and actor_role != "admin":
            raise WrongActorError(
                f"Approval requires role '{pending.required_role}', "
                f"but actor has role '{actor_role}'.",
                deciding_role=actor_role,
                required_role=pending.required_role,
                principal_id=actor or "unknown",
            )

        cursor = checkpoint.current_node_id
        state = copy.deepcopy(checkpoint.state)
        step_count = checkpoint.step_count

        # 5. Everything that can refuse the resume runs before the claim, so a refusal never
        # uses up the approval: the state update is normalised as JSON and the node's
        # binding (what the approval covers) is compared before and after it.
        gate_node = self.spec.nodes.get(cursor or "")
        paused_child: tuple[WorkflowEngine, str] | None = None
        if gate_node is not None and self._waits_on_subgraph(gate_node, pending.node_id):
            paused_child = await self._paused_child(
                workflow_run_id, gate_node, step_count + 1, approval_id
            )
            child, child_id = paused_child
            paused_child = (await child._version_engine(child_id, False), child_id)
        # A decision about an older definition never authorizes the deployed definition.
        # Keep its recorded digest intact; the execution loop requests a fresh approval.
        approval_stands = paused_child is not None or pending.spec_hash == current_spec_hash
        updated_state: dict[str, Any] | None = None
        if state_update:
            state_update = _as_json_update(state_update, cursor or "approval")
            if paused_child is None:
                updated_state = {**copy.deepcopy(state), **copy.deepcopy(state_update)}
                if gate_node is not None:
                    before = canonical_digest(self._gate_binding(gate_node, state))
                    after = canonical_digest(self._gate_binding(gate_node, updated_state))
                    approval_stands = approval_stands and before == after

        # 6. Atomically claim the approval (the store refuses one past its stored
        # expires_at; the first claimer wins), then take the run's lease (journaled
        # stores). The intent that consumes the approval is written just before the node
        # dispatches; recover() handles a stop anywhere in between.
        assert self.store is not None
        # A6 S11: the run's budget is resolved before the approval is claimed (fail closed).
        await self._precheck_budget(workflow_run_id)
        if not await self.store.claim_approval(workflow_run_id, approval_id):
            if deadline is not None and time.time() > deadline:
                raise ApprovalExpiredError(
                    f"Approval request '{approval_id}' expired before it was claimed. "
                    "Zero side effects dispatched.",
                    approval_id=approval_id,
                    expired_at=deadline,
                )
            raise ApprovalAlreadyClaimedError(
                f"Approval '{approval_id}' for workflow run '{workflow_run_id}' "
                "has already been claimed or resolved."
            )
        journal = await self._take_claimed_journal(workflow_run_id)

        async def claimed() -> WorkflowRunResult:
            nonlocal state, state_update
            assert self.store is not None
            fresh = await self.store.get(workflow_run_id)
            assert fresh is not None
            if fresh.spec_hash != current_spec_hash:
                raise WorkflowSpecMismatchError(fresh.spec_hash, current_spec_hash)
            # The claim moved the stored revision on by one; saves continue from there.
            if checkpoint.revision is not None:
                checkpoint.revision += 1
            journal = self._journal_for(workflow_run_id)
            if journal is not None:
                journal.cp_revision = checkpoint.revision
            if decision != "reject" and deadline is not None and time.time() > deadline:
                # Claimed (this caller owns the run now) but past expiry: never dispatch.
                await self._fail_expired(checkpoint, approval_id, deadline)

            if paused_child is not None:
                # The gate is inside the sub-workflow: decide it there, then re-attach.
                child, child_id = paused_child
                with contextlib.suppress(WorkflowExecutionError, WorkflowTimeoutError):
                    await child.resume(
                        child_id,
                        approval_id,
                        decision=decision,
                        state_update=state_update,
                        workspace_id=workspace_id,
                        actor=actor,
                        actor_role=actor_role,
                        expected_payload_digest=expected_payload_digest,
                    )
                state_update = None  # it applied to the sub-workflow's state

            if decision == "reject":
                # Rejection executes zero gated effects and becomes terminal
                checkpoint.status = "rejected"
                checkpoint.pending_approval = None
                checkpoint.error = f"Approval rejected for node '{cursor}'."
                await self._save(checkpoint)
                budget_status = (
                    self.budget.to_dict().get("status")
                    if self.budget is not None and hasattr(self.budget, "to_dict")
                    else None
                )
                rejected_span = self._start_workflow_span(workflow_run_id, "resume")
                rejected_span.skip_spec_duration()  # nothing ran: no invoke_workflow.duration
                tracing.approval_decided(
                    rejected_span, approval_id, kind="workflow_node", decision="reject"
                )
                rejected_span.set("nexura.workflow.status", "rejected")
                rejected_span.set("nexura.workflow.step_count", step_count)
                rejected_span.set("nexura.workflow.pause_reason", "approval")
                rejected_span.end()
                return WorkflowRunResult(
                    workflow_run_id=workflow_run_id,
                    status="rejected",
                    state=state,
                    step_count=step_count,
                    current_node_id=cursor,
                    error=checkpoint.error,
                    budget_status=budget_status,
                )

            # Approval granted: apply caller state update if provided
            # An approval is bound to the inputs the approver saw: if the update changes what
            # the gated node reads (module inputs, a function's state, a sub-workflow's inputs),
            # the approval does not carry over and the node is gated again (computed above).
            if updated_state is not None:
                state = updated_state
            if journal is not None:
                await journal.ensure_running(
                    cursor, {"type": "approval_claimed", "approval_id": approval_id}
                )
                if updated_state is not None:
                    # Keep the approver's correction if this process stops before the node
                    # runs: recover() re-checks the approval against the stored state.
                    checkpoint.state = copy.deepcopy(state)
                    checkpoint.status = "running"
                    await self._save(checkpoint)

            # A node that asked with request_approval() gets the approved details back on its
            # next step (only that node, only when the approval still stands).
            grant = (
                copy.deepcopy(pending.details)
                if approval_stands
                and pending.node_id == cursor
                and isinstance(pending.details, dict)
                and pending.details.get("node_request") is True
                else None
            )
            # Re-check expiry right before the approved node runs.
            if approval_stands and deadline is not None and time.time() > deadline:
                await self._fail_expired(checkpoint, approval_id, deadline)
            grant_token = _RESUME_GRANT.set(grant)
            decision_token = _TRACE_DECISION.set(
                {
                    "approval_id": approval_id,
                    "kind": "workflow_node",
                    "decision": "approve",
                    "inputs_edited": not approval_stands,
                }
            )
            provenance_token = _REGATE_PROVENANCE.set(
                {
                    "node_id": cursor,
                    "supersedes_approval_id": approval_id,
                    "regate_reason": (
                        "definition_migrated"
                        if pending.spec_hash != current_spec_hash
                        else "inputs_edited"
                    ),
                    "previous_input_state": copy.deepcopy(pending.input_state),
                }
                if not approval_stands
                else None
            )
            try:
                # Continue execution loop with approval granted for this node
                return await self._execute_loop(
                    workflow_run_id=workflow_run_id,
                    cursor=cursor,
                    current_state=state,
                    step_count=step_count,
                    node_already_approved=approval_stands,
                    operation="resume",
                    approval_id=approval_id if approval_stands else None,
                )
            finally:
                _RESUME_GRANT.reset(grant_token)
                _TRACE_DECISION.reset(decision_token)
                _REGATE_PROVENANCE.reset(provenance_token)

        return await self._journaled(journal, claimed, run_id=workflow_run_id, takeover=True)

    async def resume_interrupt(
        self,
        workflow_run_id: str,
        interrupt_id: str,
        value: Any,
        *,
        workspace_id: str | None = None,
        continue_on_old_definition: bool = False,
    ) -> WorkflowRunResult:
        """Answer an :func:`interrupt` and continue; the paused node runs again with it."""
        selected = await self._version_engine(workflow_run_id, continue_on_old_definition)
        if selected is not self:
            return await selected.resume_interrupt(
                workflow_run_id, interrupt_id, value, workspace_id=workspace_id
            )
        checkpoint = await self._paused_checkpoint(workflow_run_id)
        pending = checkpoint.interrupt
        if pending is None or pending.get("interrupt_id") != interrupt_id:
            raise ApprovalError(
                f"Workflow '{workflow_run_id}' is not waiting on interrupt '{interrupt_id}'."
            )
        owner = pending.get("workspace_id")
        effective_workspace = workspace_id or self.workspace_id
        if owner and owner != effective_workspace:
            raise WrongWorkspaceError(
                f"Interrupt '{interrupt_id}' belongs to workspace '{owner}', "
                f"not '{effective_workspace}'.",
                request_workspace=owner,
                decision_workspace=effective_workspace,
                approval_id=interrupt_id,
            )
        _validate_json_serializable(value, str(pending.get("node_id")))
        paused_child = None
        asking = self.spec.nodes.get(checkpoint.current_node_id or "")
        if asking is not None and self._waits_on_subgraph(asking, str(pending.get("node_id"))):
            child, child_id = await self._paused_child(
                workflow_run_id, asking, checkpoint.step_count + 1, interrupt_id
            )
            paused_child = (await child._version_engine(child_id, False), child_id)
        assert self.store is not None
        await self._precheck_budget(workflow_run_id)  # before the answer is claimed (S11)
        if not await self.store.claim_approval(workflow_run_id, interrupt_id):
            raise ApprovalAlreadyClaimedError(
                f"Interrupt '{interrupt_id}' for workflow run '{workflow_run_id}' "
                "has already been answered."
            )
        journal = await self._take_claimed_journal(workflow_run_id)

        async def answered() -> WorkflowRunResult:
            assert self.store is not None
            fresh = await self.store.get(workflow_run_id)
            assert fresh is not None
            if fresh.spec_hash != self.spec.canonical_hash():
                raise WorkflowSpecMismatchError(fresh.spec_hash, self.spec.canonical_hash())
            journal = self._journal_for(workflow_run_id)
            if journal is not None:
                journal.cp_revision = (checkpoint.revision or 0) + 1  # the claim's write
                await journal.ensure_running(
                    checkpoint.current_node_id,
                    {"type": "interrupt_answered", "interrupt_id": interrupt_id},
                )
            answers: list[Any] | None = [*pending.get("answers", []), copy.deepcopy(value)]
            asking = self.spec.nodes.get(checkpoint.current_node_id or "")
            # The node runs again approved only if it was running with an approval granted for
            # these same inputs when it asked; a node that was never gated is not approved.
            approved_binding = pending.get("approved_binding")
            node_approved = (
                asking is not None
                and isinstance(approved_binding, str)
                and approved_binding == self._binding_digest(asking, checkpoint.state)
            )
            if paused_child is not None:
                # The question came from inside the sub-workflow: answer it there.
                child, child_id = paused_child
                with contextlib.suppress(WorkflowExecutionError, WorkflowTimeoutError):
                    await child.resume_interrupt(
                        child_id, interrupt_id, value, workspace_id=workspace_id
                    )
                answers = None
            grant_token = _RESUME_GRANT.set(
                copy.deepcopy(pending.get("approved_request")) if node_approved else None
            )
            try:
                return await self._execute_loop(
                    workflow_run_id=workflow_run_id,
                    cursor=checkpoint.current_node_id,
                    current_state=copy.deepcopy(checkpoint.state),
                    step_count=checkpoint.step_count,
                    node_already_approved=node_approved,
                    resume_answers=answers,
                    operation="resume_interrupt",
                )
            finally:
                _RESUME_GRANT.reset(grant_token)

        return await self._journaled(journal, answered, run_id=workflow_run_id, takeover=True)

    async def history(self, workflow_run_id: str) -> list[WorkflowCheckpoint]:
        """Every checkpoint of a run, oldest first: inspect state step by step."""
        if self.store is None or not hasattr(self.store, "history"):
            raise WorkflowError(
                "Checkpoint history needs a store with history(), such as "
                "MemoryWorkflowStore, SQLiteWorkflowStore or PostgresWorkflowStore."
            )
        records: list[WorkflowCheckpoint] = await self.store.history(workflow_run_id)
        if records:
            self._check_owner(records[-1])
        return records

    async def fork(
        self,
        workflow_run_id: str,
        checkpoint_index: int,
        *,
        state_update: dict[str, Any] | None = None,
        new_run_id: str | None = None,
    ) -> WorkflowRunResult:
        """Start a new run from a past checkpoint, optionally changing its state first.

        ``checkpoint_index`` indexes :meth:`history` (negative counts from the end). The
        source run is not modified. Nodes before the checkpoint are not re-executed;
        approval gates ahead of it ask again. Without ``state_update`` this replays the
        run from that point.
        """
        records = await self.history(workflow_run_id)
        if not records:
            raise WorkflowError(f"Workflow run '{workflow_run_id}' has no checkpoints.")
        try:
            source = records[checkpoint_index]
        except IndexError as exc:
            raise WorkflowError(
                f"Checkpoint index {checkpoint_index} is out of range; run "
                f"'{workflow_run_id}' has {len(records)} checkpoints."
            ) from exc
        current_hash = self.spec.canonical_hash()
        if source.spec_hash != current_hash:
            raise WorkflowSpecMismatchError(
                expected_hash=source.spec_hash, actual_hash=current_hash
            )
        if source.current_node_id is None:
            raise WorkflowError(
                "That checkpoint is the end of the run; there is nothing left to execute. "
                "Pick an earlier checkpoint."
            )
        state = copy.deepcopy(source.state)
        if state_update:
            _validate_json_serializable(state_update, source.current_node_id)
            state.update(copy.deepcopy(state_update))

        run_id = new_run_id or f"{workflow_run_id}~fork-{uuid.uuid4().hex[:8]}"
        assert self.store is not None
        index = checkpoint_index if checkpoint_index >= 0 else len(records) + checkpoint_index
        first = WorkflowCheckpoint(
            workflow_run_id=run_id,
            spec_version=self.spec.spec_version,
            spec_hash=current_hash,
            workflow_version=self.spec.version,
            current_node_id=source.current_node_id,
            state=copy.deepcopy(state),
            step_count=source.step_count,
            status="running",
            forked_from={
                "workflow_run_id": workflow_run_id,
                "checkpoint_index": index,
                "step_count": source.step_count,
                "state_changed": bool(state_update),
            },
            workspace_id=self.workspace_id,
        )
        create = getattr(self.store, "create", None)
        if create is not None:
            created = await create(first)  # atomic: two forks cannot share a run id
        else:
            created = await self.store.get(run_id) is None
            if created:
                await self._save(first)
        if not created:
            raise WorkflowError(f"Workflow run '{run_id}' already exists.")
        journal = await self._new_journal(run_id, first)
        return await self._journaled(
            journal,
            lambda: self._execute_loop(
                workflow_run_id=run_id,
                cursor=source.current_node_id,
                current_state=state,
                step_count=source.step_count,
                node_already_approved=False,
                operation="fork",
            ),
            run_id=run_id,
        )

    # ------------------------------------------------------------------ recovery

    def _migration_path(self, source: str | int | None) -> list[tuple[str | int, str | int]] | None:
        target = self.spec.version
        if source is None or target is None or source == target:
            return None
        queue: list[tuple[str | int, list[tuple[str | int, str | int]]]] = [(source, [])]
        seen: set[str | int] = {source}
        for current, path in queue:
            for start, end in self.spec.migrations:
                if start != current or end in seen:
                    continue
                candidate = [*path, (start, end)]
                if end == target:
                    return candidate
                seen.add(end)
                queue.append((end, candidate))
        return None

    def _check_migration_graph(self, cp: WorkflowCheckpoint, snapshot: Any) -> None:
        available: set[str] = set()

        def collect(spec: WorkflowSpec, prefix: str = "") -> None:
            for node in spec.nodes.values():
                available.add(prefix + node.id)
                for branch in node.parallel:
                    available.add(prefix + node.id + "/" + branch)
                if node.subgraph is not None:
                    collect(node.subgraph, prefix + node.id + "/")

        collect(self.spec)
        required = {cp.current_node_id} if cp.current_node_id else set()
        if cp.pending_approval:
            required.add(cp.pending_approval.node_id)
        for pending in (cp.pending_effect, cp.interrupt):
            if pending and pending.get("node_id"):
                required.add(str(pending["node_id"]))
        for step in snapshot.steps.values():
            if step.intent is not None and step.outcome is None:
                effect = next(
                    (
                        e
                        for e in snapshot.effects.values()
                        if e.intent.step_key == step.intent.step_key
                    ),
                    None,
                )
                if (
                    effect is not None
                    and effect.status == EffectStatus.RESOLVED
                    and (effect.resolution != EffectResolution.NOT_APPLIED)
                ):
                    continue
                # Workflow keys: node[/branch]#visit[attempt/item]. Check branch ids too.
                required.add(str(step.intent.step_key).split("#", 1)[0])
        missing = sorted(required - available)
        if missing:
            raise WorkflowMigrationError(
                cp.spec_hash,
                self.spec.canonical_hash(),
                "missing node ids: " + ", ".join(missing),
            )

    async def _version_engine(self, run_id: str, continue_old: bool) -> WorkflowEngine:
        if self.store is None:
            return self
        cp = await self.store.get(run_id)
        if cp is None or cp.status in ("completed", "failed", "rejected"):
            return self
        self._check_owner(cp)
        if self._journaled_store():
            view = await self._open_journal(run_id)
            recorded = await view.snapshot()
            if recorded is not None:
                active = recorded.run.state.get("definition_digest", recorded.run.definition_digest)
                if cp.spec_hash == self.spec.canonical_hash() and active != cp.spec_hash:
                    raise WorkflowMigrationError(
                        cp.spec_hash,
                        str(active),
                        "a newer definition is committed in the journal; use that definition",
                    )
        if continue_old and cp.spec_hash != self.spec.canonical_hash():
            if not self._journaled_store():
                raise WorkflowMigrationError(
                    cp.spec_hash, self.spec.canonical_hash(), "the old definition was not journaled"
                )
            journal = await self._open_journal(run_id)
            snapshot = await journal.snapshot()
            if snapshot is None or not snapshot.run.state.get("definition"):
                raise WorkflowMigrationError(
                    cp.spec_hash, self.spec.canonical_hash(), "the old definition was not journaled"
                )
            self._check_migration_graph(cp, snapshot)
            await self._check_migration_children(cp, journal)
            selected = copy.copy(self)
            selected.spec = WorkflowSpec.from_dict(snapshot.run.state["definition"])
            return selected
        await self.migrate(run_id)
        return self

    async def _check_migration_children(
        self, cp: WorkflowCheckpoint, journal: WorkflowJournal
    ) -> None:
        """Check separately journaled child intents against their proposed subgraphs."""
        assert self.store is not None
        list_children = getattr(self.store, "list_children", None)
        if list_children is None:
            return
        for child in await list_children(cp.workflow_run_id):
            if child.status in ("completed", "failed", "rejected"):
                continue
            relative = child.workflow_run_id[len(cp.workflow_run_id) + 1 :]
            target = self.spec
            for segment in relative.split("/"):
                node_id = segment.split("#", 1)[0]
                node = target.nodes.get(node_id)
                if node is None or node.subgraph is None:
                    raise WorkflowMigrationError(
                        cp.spec_hash,
                        self.spec.canonical_hash(),
                        f"missing node ids: {relative} (sub-workflow {node_id})",
                    )
                target = node.subgraph
            try:
                snapshot = await journal.store.get(
                    ExecutionId(journal.eid.workspace_id, child.workflow_run_id)
                )
            except KeyError:
                continue
            checker = copy.copy(self)
            checker.spec = target
            checker._check_migration_graph(child, snapshot)
            if (
                child.spec_hash != target.canonical_hash()
                and checker._migration_path(child.workflow_version) is None
            ):
                raise WorkflowMigrationError(
                    child.spec_hash,
                    target.canonical_hash(),
                    f"child '{relative}' subgraph digest changed without a migration path "
                    f"({child.workflow_version!r} to {target.version!r})",
                )

    async def migrate(self, workflow_run_id: str) -> WorkflowCheckpoint:
        """Migrate checkpoint state to this definition under the run's fenced lease.

        Resume/recover call this automatically. A directed chain registered on the spec
        is required for every changed digest. The original journal row's digest remains
        immutable; its state records the active definition and atomic migration receipt.
        A committed receipt repairs a lost checkpoint save without calling a hook again.
        Pending approvals keep their original digests and are re-gated on resume.
        """
        from nexura_agents.durable import new_lease_owner

        if self.store is None:
            raise WorkflowError("State migration requires a checkpoint store.")
        cp = await self.store.get(workflow_run_id)
        if cp is None:
            raise WorkflowError(f"Workflow run '{workflow_run_id}' not found.")
        self._check_owner(cp)
        current = self.spec.canonical_hash()
        if cp.status in ("completed", "failed", "rejected"):
            raise WorkflowTerminalError(f"Workflow run '{workflow_run_id}' is terminal.")
        if not self._journaled_store():
            if cp.spec_hash == current:
                return cp
            raise WorkflowMigrationError(cp.spec_hash, current, "migration requires a journal")
        journal = await self._open_journal(workflow_run_id)
        snapshot = await journal.snapshot()
        if snapshot is None:
            if cp.spec_hash == current:
                return cp
            raise WorkflowMigrationError(cp.spec_hash, current, "run has no execution journal")
        active = snapshot.run.state.get("definition_digest", snapshot.run.definition_digest)
        if cp.spec_hash == current:
            if active != current:
                raise WorkflowMigrationError(
                    cp.spec_hash, str(active), "journal has a different active definition"
                )
            return cp
        self._check_migration_graph(cp, snapshot)
        await self._check_migration_children(cp, journal)
        receipt = snapshot.run.state.get("state_migration")
        source = receipt.get("source_checkpoint") if isinstance(receipt, dict) else None
        claimed_during_commit = (
            isinstance(source, dict)
            and isinstance(receipt, dict)
            and cp.revision == (receipt.get("source_revision") or 0) + 1
            and source["status"] == "paused"
            and cp.status == "running"
            and cp.spec_hash == source["spec_hash"]
            and cp.state == source["state"]
            and cp.current_node_id == source["current_node_id"]
            and cp.step_count == source["step_count"]
            and _pending_id(cp) == _pending_id(checkpoint_from_dict(source))
        )
        projecting = (
            isinstance(receipt, dict)
            and (receipt.get("source_revision") == cp.revision or claimed_during_commit)
            and receipt["checkpoint"]["spec_hash"] != cp.spec_hash
        )
        if projecting:
            assert isinstance(receipt, dict)
            effective = checkpoint_from_dict(receipt["checkpoint"])
            if effective.spec_hash != active:
                raise WorkflowMigrationError(
                    cp.spec_hash,
                    str(active),
                    "migration receipt disagrees with the active definition",
                )
        else:
            effective = cp
            if active != cp.spec_hash:
                raise WorkflowMigrationError(
                    cp.spec_hash,
                    str(active),
                    "checkpoint disagrees with the active journal definition; "
                    "no matching migration receipt, refusing to repeat migration hooks",
                )
        path = self._migration_path(effective.workflow_version)
        if effective.spec_hash != current and path is None:
            raise WorkflowMigrationError(
                cp.spec_hash,
                current,
                (
                    "digest changed without a version change; "
                    if effective.workflow_version == self.spec.version
                    else ""
                )
                + f"no migration path from {effective.workflow_version!r} "
                f"to {self.spec.version!r}; "
                "register_migration() or explicitly continue_on_old_definition=True "
                "with a compatible graph and the journaled old definition",
            )
        journal.digest = snapshot.run.definition_digest
        try:
            if not await journal.take(new_lease_owner()) or not journal.held:
                raise _LeaseBusyError(f"Workflow run '{workflow_run_id}' has a live run lease.")
            journal.start_heartbeat()
            if journal.state.get("definition_digest", journal.digest) != active:
                raise WorkflowMigrationError(
                    cp.spec_hash,
                    current,
                    "active definition changed while acquiring the migration lease; retry",
                )
            fresh = await self.store.get(workflow_run_id)
            assert fresh is not None
            # The preflight read is never authority after claiming the lease.
            if fresh.spec_hash != cp.spec_hash:
                raise WorkflowSpecMismatchError(fresh.spec_hash, cp.spec_hash)
            if fresh.revision != cp.revision:
                raise WorkflowConflictError(workflow_run_id, cp.revision or 0, fresh.revision or 0)
            receipt = journal.state.get("state_migration")
            if projecting:
                assert isinstance(receipt, dict)
                migrated = checkpoint_from_dict(receipt["checkpoint"])
                migrated.revision = cp.revision
                await self.store.save(migrated)
                cp = migrated
            else:
                migrated = copy.deepcopy(cp)
            if migrated.spec_hash != current:
                assert path is not None
                for start, end in path:
                    try:
                        result = self.spec.migrations[start, end](
                            start, end, copy.deepcopy(migrated.state)
                        )
                    except Exception as exc:
                        raise WorkflowMigrationError(
                            cp.spec_hash,
                            current,
                            f"migration {start!r} -> {end!r} raised {type(exc).__name__}: {exc}",
                        ) from exc
                    if not isinstance(result, dict):
                        raise WorkflowValidationError(
                            f"Migration {start!r} -> {end!r} must return a JSON object."
                        )
                    try:

                        def check_keys(value: Any) -> None:
                            if isinstance(value, dict):
                                if any(not isinstance(k, str) for k in value):
                                    raise ValueError("JSON object keys must be strings")
                                for item in value.values():
                                    check_keys(item)
                            elif isinstance(value, (list, tuple)):
                                for item in value:
                                    check_keys(item)

                        check_keys(result)
                        migrated.state = json.loads(json.dumps(result, allow_nan=False))
                    except (TypeError, ValueError, RecursionError) as exc:
                        raise WorkflowValidationError(
                            f"Migration {start!r} -> {end!r} returned invalid JSON: {exc}"
                        ) from exc
                previous_definition = journal.state.get("definition")
                if previous_definition:
                    previous_engine = copy.copy(self)
                    previous_engine.spec = WorkflowSpec.from_dict(previous_definition)
                    for record in journal.steps.values():
                        intent = record.intent
                        if intent is None or record.outcome is not None:
                            continue
                        effect = journal.effects.get(str(intent.step_key))
                        if (
                            effect is not None
                            and effect.status == EffectStatus.RESOLVED
                            and (effect.resolution != EffectResolution.NOT_APPLIED)
                        ):
                            continue
                        if intent.effect_class not in (
                            EffectClass.IDEMPOTENT,
                            EffectClass.RECONCILABLE,
                            EffectClass.NON_IDEMPOTENT,
                        ):
                            continue
                        node_id = str(intent.step_key).split("#", 1)[0].split("/", 1)[0]
                        old_node = previous_engine.spec.nodes[node_id]
                        new_node = self.spec.nodes[node_id]
                        if old_node.to_dict(False) != new_node.to_dict(
                            False
                        ) or previous_engine._binding_digest(
                            old_node, cp.state
                        ) != self._binding_digest(new_node, migrated.state):
                            if intent.effect_class == EffectClass.NON_IDEMPOTENT:
                                regates = dict(journal.state.get("migration_regates") or {})
                                regates[str(intent.step_key)] = {
                                    "node_id": node_id,
                                    "supersedes_approval_id": intent.approval_id,
                                    "regate_reason": "definition_migrated",
                                    "previous_input_state": copy.deepcopy(cp.state),
                                }
                                journal.state["migration_regates"] = regates
                                continue
                            raise WorkflowMigrationError(
                                cp.spec_hash,
                                current,
                                f"in-flight intent '{intent.step_key}' changes bound inputs or "
                                "node definition; recover it on the old definition first",
                            )
                migrated.workflow_version = self.spec.version
                migrated.spec_hash = current
                journal.state["definition"] = self.spec.to_dict()
                journal.state["definition_digest"] = current
                journal.state["workflow_version"] = self.spec.version
                journal.state["state_migration"] = {
                    "source_revision": cp.revision,
                    "source_checkpoint": checkpoint_to_dict(cp),
                    "checkpoint": checkpoint_to_dict(migrated),
                }
                await journal._commit(
                    status="paused" if migrated.status == "paused" else "running",
                    cursor=cp.current_node_id,
                    events=[
                        {
                            "type": "state_migrated",
                            "from_version": cp.workflow_version,
                            "to_version": self.spec.version,
                            "definition_digest": current,
                            "path": [list(edge) for edge in path],
                        }
                    ],
                )
                await self.store.save(migrated)
            if migrated.status == "paused" and journal.held:
                await journal.pause(
                    migrated.pause_reason or "approval",
                    migrated.current_node_id,
                    effect_id=(migrated.pending_effect or {}).get("effect_id"),
                )
            return migrated
        finally:
            await journal.close()

    async def recover(
        self,
        workflow_run_id: str,
        *,
        on_uncertain: Literal["pause", "fail"] = "pause",
        continue_on_old_definition: bool = False,
    ) -> WorkflowRunResult:
        """Continue a stopped run, or an ``effect_unknown`` pause a person resolved.

        For runs left ``running`` / ``interrupted`` by a process that stopped (Execution
        Contract v1, A6 S6).

        Needs a store with an execution journal (``MemoryWorkflowStore``,
        ``SQLiteWorkflowStore``, ``nexura_postgres.PostgresWorkflowStore``). The caller
        takes the run's lease: at once when the owner was a process on this host that has
        exited, otherwise only after the lease expired; while another worker holds it,
        ``WorkflowError`` is raised. Then, for the node at the stored cursor:

        - its outcome was committed (the checkpoint save was lost): the outcome is applied
          and the run continues from the next node;
        - it never dispatched (no intent): it runs. A gated node whose approval was claimed
          runs under that approval after re-checking its payload digest, binding and expiry;
          an expired or changed approval is asked for again;
        - it dispatched without an outcome: ``pure`` and ``idempotent`` nodes run again (the
          latter with the same ``step_idempotency_key()`` / ``ctx.idempotency_key()``);
          ``reconcilable`` nodes are probed; a ``non_idempotent`` node is never repeated:
          the run pauses with ``pause_reason="effect_unknown"`` and a ``pending_effect``
          (``on_uncertain="fail"`` fails the run instead).

        A paused ``effect_unknown`` run continues once its effect is resolved (see
        :meth:`resolve_effect`, or ``POST /runs/{id}/effects/{effect_id}/resolve``):
        ``confirmed`` continues with the given result as the node's output, ``not_applied``
        dispatches the node again as a new attempt with a new key (a gated node first asks
        for a fresh approval), ``failed`` fails the run. Any other paused run is returned as
        it is; a finished run raises ``WorkflowTerminalError``.

        Composite steps (A6 S7) are decided per part when the step runs again: a parallel
        branch or mapped item whose outcome was committed is not run again; one dispatched
        without an outcome follows the rules above with its own key (``<node>/<branch>#<step>``
        or ``<node>#<step>[<item>]``); a ``non_idempotent`` one pauses the run with a
        ``pending_effect`` naming it (``"part"``, ``"branch"`` / ``"item"``), after the other
        parts finished. A sub-workflow child is recovered under its own journal, from here
        (call ``recover`` on the top-level run, not on a child): when it pauses
        ``effect_unknown``, so does this run, with the child's effect and ``"run_id"``.
        """
        if on_uncertain not in ("pause", "fail"):
            raise WorkflowValidationError("on_uncertain must be 'pause' or 'fail'.")
        selected = await self._version_engine(workflow_run_id, continue_on_old_definition)
        if selected is not self:
            return await selected.recover(workflow_run_id, on_uncertain=on_uncertain)
        checkpoint = await self._recoverable_checkpoint(workflow_run_id)
        if checkpoint.status == "paused" and checkpoint.pending_effect is None:
            return self._result_from_checkpoint(checkpoint)  # waiting for a person
        journal = await self._take_journal(workflow_run_id)
        assert journal is not None
        if journal.status in _JOURNAL_TERMINAL:
            return await self._project_finished(journal, workflow_run_id)
        if journal.owner is None:
            await journal.close()
            raise _LeaseBusyError(
                f"Workflow run '{workflow_run_id}' is being executed by another worker (its "
                "run lease is live); recover it after that lease expires."
            )
        token = _ON_UNCERTAIN.set(on_uncertain)
        try:
            return await self._journaled(
                journal,
                lambda: self._recover_taken(journal, workflow_run_id, on_uncertain),
                run_id=workflow_run_id,
                takeover=True,
            )
        finally:
            _ON_UNCERTAIN.reset(token)

    async def resolve_effect(
        self,
        workflow_run_id: str,
        resolution: EffectResolution | str,
        *,
        actor: str,
        result: Any = None,
        effect_id: str | None = None,
    ) -> WorkflowRunResult:
        """Resolve the unknown effect a run is paused on, then continue it with :meth:`recover`.

        ``resolution`` is ``"confirmed"`` (it happened: ``result``, a dict of state updates
        with an optional ``"_branch"``, becomes the node's output and the node is not run
        again), ``"not_applied"`` (it did not happen: the node is dispatched again as a new
        attempt with a new idempotency key; a gated node first asks for a fresh approval) or
        ``"failed"`` (the run fails). The decision and ``actor`` are journaled once; a
        second, different resolution raises ``EffectAlreadyResolvedError``.

        For a parallel branch or mapped item the decision applies to that part only (its
        result is the part's update); for an effect inside a sub-workflow child (the
        ``pending_effect`` has the child's ``"run_id"``) it is journaled in the child's run
        and the child continues when this run is recovered.
        """
        resolution = EffectResolution(resolution)
        if resolution == EffectResolution.CONFIRMED:
            if result is not None and not isinstance(result, dict):
                raise WorkflowValidationError(
                    "A confirmed workflow effect's result must be a dict of state updates."
                )
            _validate_json_serializable(result or {}, "effect")
        checkpoint = await self._recoverable_checkpoint(workflow_run_id)
        pending = checkpoint.pending_effect
        if checkpoint.status != "paused" or pending is None:
            raise WorkflowError(f"Workflow run '{workflow_run_id}' is not paused on an effect.")
        wanted = effect_id or str(pending.get("effect_id"))
        if wanted != pending.get("effect_id"):
            raise WorkflowError(
                f"Workflow run '{workflow_run_id}' is paused on effect "
                f"'{pending.get('effect_id')}', not '{wanted}'."
            )
        target = str(pending.get("run_id") or workflow_run_id)
        if target != workflow_run_id:
            if not target.startswith(f"{workflow_run_id}/"):
                raise WorkflowError(
                    f"Effect '{wanted}' belongs to run '{target}', which is not a sub-workflow "
                    f"of '{workflow_run_id}'."
                )
            journal: WorkflowJournal | None = await self._take_run_journal(target)
        else:
            journal = await self._take_journal(workflow_run_id)
        assert journal is not None
        if journal.owner is None or journal.status in _JOURNAL_TERMINAL:
            await journal.close()
            raise WorkflowError(f"Workflow run '{target}' is held by another worker.")
        try:
            record = journal.effect_by_id(wanted)
            if record is None:
                raise WorkflowError(f"Effect '{wanted}' is not in the run's journal.")
            if record.status != EffectStatus.RESOLVED or record.resolution != resolution:
                await journal.resolve(wanted, resolution, actor=actor, result=result)
        finally:
            await journal.close()
        return await self.recover(workflow_run_id)

    async def _take_run_journal(self, run_id: str) -> WorkflowJournal:
        """The lease of another journaled run (a sub-workflow child), by its stored digest."""
        from nexura_agents.durable import new_lease_owner

        assert self.store is not None
        store = await self.store.open_execution_store()  # type: ignore[attr-defined]
        try:
            snapshot = await store.get(ExecutionId(self.workspace_id or "default", run_id))
        except KeyError as exc:
            raise WorkflowError(f"Workflow run '{run_id}' has no execution journal.") from exc
        journal = WorkflowJournal(
            store,
            self.workspace_id or "default",
            run_id,
            snapshot.run.definition_digest,
            self.lease_ttl,
            snapshot.run.parent_run_id,
        )
        if not await journal.take(new_lease_owner()):
            journal.owner = None
        return journal

    async def _part_resolution(
        self, journal: WorkflowJournal, pending: dict[str, Any]
    ) -> EffectResolution | None:
        """How a person resolved the part effect a run paused on (None: not resolved)."""
        run_id = str(pending.get("run_id") or journal.run_id)
        effect: EffectRecord | None
        if run_id == journal.run_id:
            effect = journal.effect_by_id(pending.get("effect_id"))
        else:
            store = journal.store
            try:
                snapshot = await store.get(ExecutionId(journal.eid.workspace_id, run_id))
            except KeyError:
                return None
            effect = snapshot.effects.get(str(pending.get("effect_id")))
        if effect is None or effect.status != EffectStatus.RESOLVED:
            return None
        return effect.resolution

    async def _recoverable_checkpoint(self, workflow_run_id: str) -> WorkflowCheckpoint:
        if self.store is None or not self._journaled_store():
            raise WorkflowError(
                "recover() needs a store with an execution journal, such as "
                "MemoryWorkflowStore, SQLiteWorkflowStore or PostgresWorkflowStore."
            )
        checkpoint = await self.store.get(workflow_run_id)
        if checkpoint is None:
            raise WorkflowExecutionError(
                node_id="unknown", message=f"Workflow run '{workflow_run_id}' not found."
            )
        self._check_owner(checkpoint)
        if checkpoint.status in ("completed", "failed", "rejected"):
            raise WorkflowTerminalError(
                f"Workflow run '{workflow_run_id}' is already in terminal state "
                f"'{checkpoint.status}'."
            )
        current_hash = self.spec.canonical_hash()
        if checkpoint.spec_hash != current_hash:
            raise WorkflowSpecMismatchError(
                expected_hash=checkpoint.spec_hash, actual_hash=current_hash
            )
        return checkpoint

    def _budget_state(self) -> dict[str, Any] | None:
        if self.budget is not None and hasattr(self.budget, "to_dict"):
            state: dict[str, Any] = self.budget.to_dict()
            return state
        return None

    async def get_result(self, workflow_run_id: str) -> WorkflowRunResult:
        """Inspect a run's latest checkpoint without dispatching or claiming approval."""
        if self.store is None:
            raise WorkflowError("Reading a run requires a checkpoint store.")
        cp = await self.store.get(workflow_run_id)
        if cp is None:
            raise WorkflowError(f"Workflow run '{workflow_run_id}' not found.")
        self._check_owner(cp)
        return self._result_from_checkpoint(cp)

    def _result_from_checkpoint(self, cp: WorkflowCheckpoint) -> WorkflowRunResult:
        budget_state = cp.budget_state
        return WorkflowRunResult(
            workflow_run_id=cp.workflow_run_id,
            status=cp.status,
            state=copy.deepcopy(cp.state),
            step_count=cp.step_count,
            current_node_id=cp.current_node_id,
            pending_approval=cp.pending_approval,
            error=cp.error,
            budget_status=budget_state.get("status") if budget_state else None,
            interrupt=(
                {k: cp.interrupt[k] for k in ("interrupt_id", "node_id", "value")}
                if cp.interrupt
                else None
            ),
            pause_reason="effect_unknown" if cp.pending_effect is not None else None,
            pending_effect=copy.deepcopy(cp.pending_effect),
            workflow_version=cp.workflow_version,
            definition_digest=cp.spec_hash,
        )

    async def _project_finished(
        self, journal: WorkflowJournal, workflow_run_id: str
    ) -> WorkflowRunResult:
        """The journal finished the run but its last checkpoint save was lost: write it."""
        assert self.store is not None
        cp = await self.store.get(workflow_run_id)
        assert cp is not None
        if journal.status == "completed" and cp.current_node_id in self.spec.nodes:
            _, key, step = journal.latest(cp.current_node_id, cp.step_count + 1)
            output = step.outcome.output if step is not None and step.outcome else None
            if isinstance(output, dict):
                cp.state.update(output.get("update") or {})
                cp.step_count += 1
                cp.current_node_id = output.get("next")
        cp.status = "completed" if journal.status == "completed" else "failed"
        cp.pending_approval = None
        cp.pending_effect = None
        if cp.status == "failed" and cp.error is None:
            cp.error = "The run failed (recorded in its execution journal)."
        await self.store.save(cp)
        return self._result_from_checkpoint(cp)

    def _next_node(self, node: WorkflowNode, branch: str | None) -> str | None:
        """Where the graph goes after ``node`` returns ``branch``."""
        if node.id in self.spec.terminal_nodes:
            return None
        if branch is not None:
            if branch not in node.transitions:
                raise WorkflowExecutionError(
                    node_id=node.id,
                    message=(
                        f"Handler chose branch '{branch}', but node '{node.id}' "
                        "defines no such transition."
                    ),
                )
            return node.transitions[branch]
        return node.default_transition or node.transitions.get("default")

    def _is_finished_cursor(self, cursor: str | None) -> bool:
        return cursor is None or (
            cursor in self.spec.terminal_nodes and cursor not in self.spec.nodes
        )

    def _approval_still_valid(
        self, node: WorkflowNode, pending: PendingApproval, state: dict[str, Any]
    ) -> bool:
        """A claimed approval may still dispatch its node: same node, payload and binding,
        same workspace, not expired (design §2.7, "claimed but no intent written")."""
        if pending.node_id != node.id:
            return False
        if pending.spec_hash != self.spec.canonical_hash():
            return False
        if pending.workspace_id and pending.workspace_id != self.workspace_id:
            return False
        deadline = self._approval_deadline(pending)
        if deadline is not None and time.time() > deadline:
            return False
        if pending.payload_digest:
            details = pending.details if isinstance(pending.details, dict) else None
            try:
                digest = (
                    canonical_digest({"state": pending.input_state, "request": details})
                    if details is not None and details.get("node_request") is True
                    else canonical_digest(pending.input_state)
                )
            except (TypeError, ValueError):
                return False
            if digest != pending.payload_digest:
                return False
        before = self._binding_digest(node, pending.input_state)
        return before is not None and before == self._binding_digest(node, state)

    def _pending_effect_view(self, effect: EffectRecord, node_id: str) -> dict[str, Any]:
        intent = effect.intent
        return {
            "effect_id": effect.effect_id,
            "step_key": str(intent.step_key),
            "node_id": node_id,
            "effect_class": intent.effect_class.value,
            "idempotency_key": intent.idempotency_key,
            "approval_id": intent.approval_id,
            "payload_digest": intent.payload_digest,
        }

    async def _recover_taken(
        self, journal: WorkflowJournal, workflow_run_id: str, on_uncertain: str
    ) -> WorkflowRunResult:
        assert self.store is not None
        cp = await self.store.get(workflow_run_id)  # re-read under the lease
        assert cp is not None
        if cp.spec_hash != self.spec.canonical_hash():
            raise WorkflowSpecMismatchError(cp.spec_hash, self.spec.canonical_hash())
        journal.cp_revision = cp.revision
        if cp.status in ("completed", "failed", "rejected"):
            with contextlib.suppress(LeaseLostError):
                await self._journal_sync(journal, cp)  # finished meanwhile: end the journal
            raise WorkflowTerminalError(
                f"Workflow run '{workflow_run_id}' is already in terminal state '{cp.status}'."
            )
        if cp.status == "paused" and cp.pending_effect is None:
            return self._result_from_checkpoint(cp)
        part = cp.pending_effect if cp.pending_effect and cp.pending_effect.get("part") else None
        if part is not None and part.get("run_id") == workflow_run_id:
            in_doubt = journal.effect_by_id(part.get("effect_id"))
            if in_doubt is not None and in_doubt.status == EffectStatus.INDETERMINATE:
                return self._result_from_checkpoint(cp)  # still waiting for a person
        cursor, step = cp.current_node_id, cp.step_count
        if self._is_finished_cursor(cursor):
            cp.status = "completed"
            await self._save(cp)
            return self._result_from_checkpoint(cp)
        node = self.spec.nodes.get(cursor or "")
        if node is None:
            raise WorkflowValidationError(f"Unknown node ID '{cursor}' in run '{workflow_run_id}'.")
        _, key, record = journal.latest(node.id, step + 1)
        effect = journal.effects.get(str(key)) if record is not None else None

        outcome = record.outcome if record is not None else None
        if (
            outcome is not None
            and outcome.status == "completed"
            and isinstance(outcome.output, dict)
        ):
            # The outcome committed but the checkpoint save after it was lost.
            output = outcome.output
            return await self._continue_after(
                journal, cp, node, key, output.get("update") or {}, output.get("branch")
            )
        if record is not None and record.intent is not None and effect is not None:
            intent = record.intent
            if effect.status == EffectStatus.RESOLVED:
                if effect.resolution == EffectResolution.CONFIRMED:
                    update, branch = _parse_handler_output(copy.deepcopy(effect.result))
                    return await self._continue_after(journal, cp, node, key, update, branch)
                if effect.resolution == EffectResolution.FAILED:
                    return await self._fail_recovered(
                        cp,
                        f"Node '{node.id}' failed: its effect was resolved as failed by "
                        f"'{effect.resolved_by}'.",
                    )
                # not_applied: dispatch again as a new attempt; a gated node asks again.
                return await self._continue_from(journal, cp, approved=False)
            if effect.status == EffectStatus.INDETERMINATE:
                # Still waiting for a person (paused effect_unknown).
                return await self._pause_effect_unknown(journal, cp, node, effect)
            effect_class = intent.effect_class
            if effect_class in (EffectClass.PURE, EffectClass.IDEMPOTENT):
                approved = intent.approval_id is not None
                if (
                    approved
                    and part is not None
                    and self._is_gated(node, cp.state)
                    and await self._part_resolution(journal, part) == EffectResolution.NOT_APPLIED
                ):
                    # A part of a gated step did not happen: sending it again needs a
                    # fresh approval of the step (design §2.5).
                    approved = False
                return await self._continue_from(
                    journal,
                    cp,
                    approved=approved,
                    approval_id=intent.approval_id if approved else None,
                )
            if effect_class == EffectClass.RECONCILABLE:
                probe = await self._reconcile(node, intent, cp.state)
                if isinstance(probe, Applied):
                    update, branch = _parse_handler_output(copy.deepcopy(probe.result))
                    return await self._continue_after(journal, cp, node, key, update, branch)
                if isinstance(probe, NotApplied):
                    # Same key; a gated node needs a fresh approval to send it again.
                    gated = self._is_gated(node, cp.state)
                    return await self._continue_from(
                        journal, cp, approved=not gated and intent.approval_id is not None
                    )
            if on_uncertain == "fail":
                return await self._fail_recovered(
                    cp,
                    f"Node '{node.id}' was dispatched before the process stopped and its "
                    "outcome is unknown (on_uncertain='fail').",
                )
            return await self._pause_effect_unknown(journal, cp, node, effect)

        pending = cp.pending_approval
        # Approval claimed, node never dispatched (no intent): dispatch under the same
        # approval if it still holds, else ask again.
        if (
            pending is not None
            and cp.status in ("running", "interrupted")
            and self._approval_still_valid(node, pending, cp.state)
        ):
            grant = (
                copy.deepcopy(pending.details)
                if isinstance(pending.details, dict) and pending.details.get("node_request") is True
                else None
            )
            grant_token = _RESUME_GRANT.set(grant)
            try:
                return await self._continue_from(
                    journal, cp, approved=True, approval_id=pending.approval_id
                )
            finally:
                _RESUME_GRANT.reset(grant_token)
        return await self._continue_from(journal, cp, approved=False)

    def _is_gated(self, node: WorkflowNode, state: dict[str, Any]) -> bool:
        return (
            node.requires_approval
            or node.handler in self.approval_required_handlers
            or (node.module_id is not None and node.module_id in self.approval_required_handlers)
            or self._write_operation_gated(node, state)
        )

    async def _reconcile(
        self, node: WorkflowNode, intent: StepIntent, state: dict[str, Any]
    ) -> Applied | NotApplied | Unknown:
        """Probe a reconcilable node; an error or a wrong answer counts as ``Unknown``."""
        _, reconcile = self._node_effect(node, state)
        if reconcile is None:
            return Unknown()
        return await self._probe(f"node '{node.id}'", reconcile, intent, state)

    @staticmethod
    async def _probe(
        what: str, reconcile: Callable[..., Any], intent: StepIntent, state: dict[str, Any]
    ) -> Applied | NotApplied | Unknown:
        """Call ``reconcile(state, intent)``; an error or a wrong answer is ``Unknown``."""
        import warnings

        try:
            answer = reconcile(copy.deepcopy(state), intent)
            if inspect.isawaitable(answer):
                answer = await answer
        except Exception as exc:
            warnings.warn(
                f"reconcile for {what} raised {type(exc).__name__}: {exc}; "
                "treating the outcome as unknown.",
                RuntimeWarning,
                stacklevel=3,
            )
            return Unknown()
        if isinstance(answer, (Applied, NotApplied, Unknown)):
            return answer
        warnings.warn(
            f"reconcile for {what} returned {type(answer).__name__}, not Applied, "
            "NotApplied or Unknown; treating the outcome as unknown.",
            RuntimeWarning,
            stacklevel=3,
        )
        return Unknown()

    async def _continue_from(
        self,
        journal: WorkflowJournal,
        cp: WorkflowCheckpoint,
        *,
        approved: bool,
        approval_id: str | None = None,
    ) -> WorkflowRunResult:
        """Run the graph again from the stored cursor, under the recovered lease."""
        await journal.ensure_running(cp.current_node_id, {"type": "run_recovered"})
        if cp.status != "running" or cp.pending_effect is not None:
            cp.status = "running"
            cp.pending_effect = None
            await self._save(cp)
        pending = cp.pending_approval
        changed_approval = (
            pending is not None
            and pending.node_id == cp.current_node_id
            and pending.spec_hash != self.spec.canonical_hash()
        )
        if changed_approval:
            approved = False
        # A resolved not_applied intent is still bound to its original inputs. A
        # migration may remove the gate, but cannot authorize sending changed inputs.
        regates = journal.state.get("migration_regates") or {}
        migrated_intent = next(
            (
                value
                for key, value in regates.items()
                if key.split("#", 1)[0].split("/", 1)[0] == cp.current_node_id
                and key.split("#", 1)[1].split("[", 1)[0].split(".", 1)[0] == str(cp.step_count + 1)
            ),
            None,
        )
        if (
            migrated_intent
            and pending is not None
            and self._approval_still_valid(
                self.spec.nodes[cp.current_node_id or ""], pending, cp.state
            )
            and cp.status == "running"
        ):
            approved, approval_id = True, pending.approval_id
            migrated_intent = None
        elif migrated_intent:
            approved = False
        provenance = _REGATE_PROVENANCE.set(
            {
                "node_id": cp.current_node_id,
                "supersedes_approval_id": pending.approval_id,
                "regate_reason": "definition_migrated",
                "previous_input_state": copy.deepcopy(pending.input_state),
            }
            if changed_approval and pending is not None
            else migrated_intent
        )
        try:
            return await self._execute_loop(
                workflow_run_id=cp.workflow_run_id,
                cursor=cp.current_node_id,
                current_state=copy.deepcopy(cp.state),
                step_count=cp.step_count,
                node_already_approved=approved,
                operation="recover",
                approval_id=approval_id if approved else None,
            )
        finally:
            _REGATE_PROVENANCE.reset(provenance)

    async def _continue_after(
        self,
        journal: WorkflowJournal,
        cp: WorkflowCheckpoint,
        node: WorkflowNode,
        key: StepKey,
        update: dict[str, Any],
        branch: str | None,
    ) -> WorkflowRunResult:
        """Apply a node's known output (committed, confirmed or reconciled) and go on."""
        _validate_json_serializable(update, node.id)
        await journal.ensure_running(cp.current_node_id, {"type": "run_recovered"})
        try:
            next_node = self._next_node(node, branch)
        except WorkflowExecutionError as exc:
            return await self._fail_recovered(cp, str(exc))
        state = {**copy.deepcopy(cp.state), **copy.deepcopy(update)}
        self._written.update(update)
        step = cp.step_count + 1
        terminal = self._is_finished_cursor(next_node)
        if terminal:  # end open reservations before the terminal commit (S11 review m1)
            await self._settle_reservations(cp.workflow_run_id, journal, "terminal")
        await journal.record_outcome(
            key,
            {"update": update, "branch": branch, "next": next_node},
            cursor=next_node,
            terminal=terminal,
        )
        await self._settle_deferred(cp.workflow_run_id, journal)
        checkpoint = WorkflowCheckpoint(
            workflow_run_id=cp.workflow_run_id,
            spec_version=self.spec.spec_version,
            spec_hash=self.spec.canonical_hash(),
            current_node_id=next_node,
            state=copy.deepcopy(state),
            step_count=step,
            status="completed" if terminal else "running",
            budget_state=self._budget_state(),
        )
        await self._save(checkpoint)
        if terminal:
            return self._result_from_checkpoint(checkpoint)
        return await self._execute_loop(
            workflow_run_id=cp.workflow_run_id,
            cursor=next_node,
            current_state=state,
            step_count=step,
            node_already_approved=False,
            operation="recover",
        )

    async def _pause_effect_unknown(
        self,
        journal: WorkflowJournal,
        cp: WorkflowCheckpoint,
        node: WorkflowNode,
        effect: EffectRecord,
    ) -> WorkflowRunResult:
        """Paused, ``pause_reason="effect_unknown"``: a person decides what happened."""
        pending_effect = self._pending_effect_view(effect, node.id)
        if journal.status != "paused" or effect.status == EffectStatus.PENDING:
            await journal.pause("effect_unknown", node.id, [effect.intent.step_key])
        pending_effect["effect_id"] = journal.effects[str(effect.intent.step_key)].effect_id
        if cp.status != "paused" or cp.pending_effect != pending_effect:
            cp = dataclasses.replace(
                cp,
                status="paused",
                pending_approval=None,
                interrupt=None,
                pending_effect=pending_effect,
                error=None,
            )
            await self._save(cp)
        self._progress(
            RunEventType.NODE_PAUSED,
            node_id=node.id,
            step=cp.step_count + 1,
            reason="effect_unknown",
            workflow_run_id=cp.workflow_run_id,
        )
        return self._result_from_checkpoint(cp)

    async def _fail_recovered(self, cp: WorkflowCheckpoint, error: str) -> WorkflowRunResult:
        cp = dataclasses.replace(
            cp, status="failed", error=error, pending_approval=None, pending_effect=None
        )
        await self._save(cp)
        return self._result_from_checkpoint(cp)

    # ------------------------------------------------------------------ steps

    def _gating_module(self, node: WorkflowNode, handler: str, module_id: str | None) -> Any:
        """The registered module behind a handler name, or None (never raises)."""
        try:
            obj = self._resolve_handler(handler, module_id, node.module_version)
        except Exception:
            return None
        return obj if hasattr(obj, "manifest") else None

    def _module_names(self, node: WorkflowNode) -> list[str]:
        return list(node.parallel) if node.parallel else [node.handler]

    def _is_module_node(self, node: WorkflowNode) -> bool:
        """True when every handler of the step is a module (declared inputs), not a function."""
        if node.subgraph is not None:
            return False
        for name in self._module_names(node):
            try:
                obj = self._resolve_handler(name, None if node.parallel else node.module_id, None)
            except Exception:
                return False
            if not hasattr(obj, "execute"):
                return False
        return True

    def _item_inputs(self, node: WorkflowNode, state: dict[str, Any], item: Any, index: int) -> Any:
        item_state = copy.deepcopy(state)
        item_state[node.map_as] = copy.deepcopy(item)
        item_state[f"{node.map_as}_index"] = index
        if node.input_mapping:
            return _map_inputs(node.input_mapping, item_state)
        return {node.map_as: copy.deepcopy(item), "operation": state.get("operation")}

    def _binding_digest(self, node: WorkflowNode, state: dict[str, Any]) -> str | None:
        """Digest of :meth:`_gate_binding`, or None if the state cannot be canonicalised."""
        try:
            return canonical_digest(self._gate_binding(node, state))
        except (TypeError, ValueError):
            return None

    def _approval_deadline(self, pending: PendingApproval) -> float | None:
        """When a pending approval stops being usable.

        ``expires_at`` when recorded; for a row stored without one, ``created_at`` plus the
        engine's ``approval_ttl`` (a row with no ``created_at`` either is treated as created
        at time 0, so it is already expired).
        """
        if pending.expires_at is not None:
            return pending.expires_at
        if self.approval_ttl is None:
            return None
        return pending.created_at + self.approval_ttl

    async def _fail_expired(
        self, checkpoint: WorkflowCheckpoint, approval_id: str, deadline: float
    ) -> NoReturn:
        checkpoint.status = "failed"
        checkpoint.error = f"Approval request '{approval_id}' has expired."
        if self._journal_for(checkpoint.workflow_run_id) is None and self._journaled_store():
            # Refused before the run's lease was taken: end its journal too (best effort).
            journal = await self._take_journal(checkpoint.workflow_run_id)
            if journal is not None and journal.owner is not None:
                token = _JOURNAL.set(journal)
                try:
                    with contextlib.suppress(LeaseLostError):
                        await self._journal_sync(journal, checkpoint)
                finally:
                    _JOURNAL.reset(token)
                    await journal.close()
        await self._save(checkpoint)
        raise ApprovalExpiredError(
            f"Approval request '{approval_id}' has expired. Zero side effects dispatched.",
            approval_id=approval_id,
            expired_at=deadline,
        )

    def _gate_binding(self, node: WorkflowNode, state: dict[str, Any]) -> Any:
        """The values a gated node will read: what its approval binds to.

        Every node kind has a binding, so a ``resume(state_update=...)`` that changes what
        the approved node would read re-gates it for a fresh approval:

        - module node: the resolved inputs (``input_mapping`` applied, so the operation and
          params), each mapped item's resolved inputs, and for a parallel step all the state;
        - function node (plain, mapped or parallel with any function branch): the whole
          state, because a function handler receives all of it;
        - sub-workflow node: the inputs it passes to the child run (``input_mapping``
          applied), per item when mapped.
        """
        if node.subgraph is not None:
            sub: dict[str, Any] = {"inputs": _map_inputs(node.input_mapping, state)}
            if node.map_over is not None:
                items = state.get(node.map_over)
                if isinstance(items, list):
                    sub["items"] = [
                        _map_inputs(
                            node.input_mapping,
                            {
                                **copy.deepcopy(state),
                                node.map_as: copy.deepcopy(item),
                                f"{node.map_as}_index": i,
                            },
                        )
                        for i, item in enumerate(items)
                    ]
            return sub
        if not self._is_module_node(node):
            # Function handlers (each mapped item and parallel branch too) read the state.
            return {"state": copy.deepcopy(state)}
        binding: dict[str, Any] = {"inputs": _map_inputs(node.input_mapping, state)}
        if node.parallel:
            # Branches read the whole state (input_mapping does not apply), so bind all of it.
            binding["state"] = copy.deepcopy(state)
        if node.map_over is not None:
            items = state.get(node.map_over)
            if isinstance(items, list):
                binding["items"] = [
                    self._item_inputs(node, state, item, i) for i, item in enumerate(items)
                ]
        return binding

    def _approval_details(self, node: WorkflowNode, state: dict[str, Any]) -> dict[str, Any] | None:
        """What the approver of a parallel or mapped module step approves in one decision.

        Parallel steps list their branches and which of them can write; mapped steps list the
        resolved items and the cap.
        """
        if node.parallel and self._is_module_node(node):
            branches = []
            for name in node.parallel:
                manifest = getattr(self._gating_module(node, name, None), "manifest", None)
                writes = bool(
                    manifest is not None
                    and (getattr(manifest, "write_operations", None) or manifest.always_write)
                )
                branches.append({"handler": name, "can_write": writes})
            return {
                "parallel": True,
                "branch_count": len(branches),
                "branches": branches,
                "write_branch_count": sum(1 for b in branches if b["can_write"]),
                "inputs_digest": canonical_digest(self._gate_binding(node, state)),
            }
        if node.map_over is None or not self._is_module_node(node):
            return None
        items = state.get(node.map_over)
        if not isinstance(items, list):
            return None
        resolved = [self._item_inputs(node, state, item, i) for i, item in enumerate(items)]
        return {
            "map_over": node.map_over,
            "item_count": len(resolved),
            "items": resolved,
            "items_digest": canonical_digest(resolved),
            "max_writes_per_approval": self.max_map_writes_per_approval,
        }

    def _pre_approval_refusal(self, node: WorkflowNode, state: dict[str, Any]) -> str | None:
        """Why a module refuses the inputs this step would be approved to run, or None.

        A module may define ``refuse_before_approval(inputs, config)`` returning the failure
        its ``invoke`` would return whatever the approval (for example the code sandbox for
        oversized code). It is asked for the resolved inputs of a plain step, of each mapped
        item and of each parallel branch, so nobody is asked to approve a refused call.
        """
        if node.subgraph is not None:
            return None
        if node.parallel:
            modules = [self._gating_module(node, name, None) for name in node.parallel]
        else:
            modules = [self._gating_module(node, node.handler, node.module_id)]
        checks: list[Callable[..., Any]] = [
            check
            for module in modules
            if callable(check := getattr(module, "refuse_before_approval", None))
        ]
        if not checks:
            return None
        inputs_list: list[Any]
        if node.parallel:
            inputs_list = [copy.deepcopy(state)]  # branches read the whole state
        elif node.map_over is not None:
            items = state.get(node.map_over)
            inputs_list = (
                [self._item_inputs(node, state, item, i) for i, item in enumerate(items)]
                if isinstance(items, list)
                else []
            )
        elif node.input_mapping:
            inputs_list = [_map_inputs(node.input_mapping, state)]
        else:
            inputs_list = [copy.deepcopy(state)]
        for check in checks:
            for inputs in inputs_list:
                if not isinstance(inputs, dict):
                    continue
                try:
                    refusal = check(inputs, copy.deepcopy(node.config or {}))
                except Exception:
                    continue  # the module checks again when it runs, after approval
                if refusal is not None and getattr(refusal, "status", None) != "success":
                    code = getattr(refusal, "error_code", None) or "REFUSED"
                    message = getattr(refusal, "error_message", None) or "refused"
                    return f"{code}: {message}"
        return None

    def _check_map_write_cap(self, node: WorkflowNode, state: dict[str, Any]) -> None:
        if node.map_over is None or not self._write_operation_gated(node, state):
            return
        items = state.get(node.map_over)
        if isinstance(items, list) and len(items) > self.max_map_writes_per_approval:
            raise WorkflowValidationError(
                f"Node '{node.id}' maps a write-capable module over {len(items)} items; one "
                f"approval covers at most {self.max_map_writes_per_approval} "
                "(max_map_writes_per_approval). Split the list or raise the limit."
            )

    def _write_operation_gated(self, node: WorkflowNode, state: dict[str, Any]) -> bool:
        """True when the node runs a module operation that writes to an external system.

        Reads the operation the module will receive (the node's input mapping applied to
        the current state). An operation that cannot be determined is gated conservatively.
        Map and parallel steps gate whenever a module has any write operation.
        """
        if node.subgraph is not None:
            return False  # inner nodes are gated by their own engine run
        names = list(node.parallel) if node.parallel else [node.handler]
        for name in names:
            module = self._gating_module(node, name, None if node.parallel else node.module_id)
            manifest = getattr(module, "manifest", None)
            if manifest is None:
                continue
            if not (getattr(manifest, "write_operations", None) or manifest.always_write):
                continue
            if node.parallel or node.map_over is not None or manifest.always_write:
                return True
            key = manifest.write_operation_key
            if node.input_mapping:
                operation = _map_inputs(
                    {k: v for k, v in node.input_mapping.items() if k == key}, state
                ).get(key)
            else:
                operation = state.get(key)
            if manifest.write_gate(operation):
                return True
        return False

    def _resolve_handler(
        self, name: str, module_id: str | None = None, module_version: str | None = None
    ) -> Any:
        handler_obj: Any
        if module_id and self.registry.has_module(module_id, module_version):
            handler_obj = self.registry.get_module(module_id, module_version)
        else:
            handler_obj = self.registry.get(name)

        # Enforce module workspace isolation
        mod_ws = getattr(handler_obj, "_workspace_id", None)
        if mod_ws is not None and self.workspace_id is not None and mod_ws != self.workspace_id:
            from nexura_runtime.service.auth.errors import CrossWorkspaceAccessError

            raise CrossWorkspaceAccessError(
                f"Cross-workspace access denied: module '{module_id or name}' "
                f"belongs to workspace '{mod_ws}', not '{self.workspace_id}'."
            )
        return handler_obj

    async def _run_step(
        self,
        node: WorkflowNode,
        handler_obj: Any,
        state: dict[str, Any],
        workflow_run_id: str,
        step_count: int,
        attempt: int,
        remaining: float | None,
    ) -> tuple[dict[str, Any], str | None]:
        if node.parallel:
            return await self._run_parallel(node, state, workflow_run_id, step_count, remaining)
        if node.map_over is not None:
            return await self._run_map(
                node, handler_obj, state, workflow_run_id, step_count, attempt, remaining
            )
        if node.subgraph is not None:
            update = await self._run_subgraph(
                node, state, workflow_run_id, step_count, attempt, remaining, None
            )
            return update, None
        return await self._invoke(handler_obj, node, state, workflow_run_id, step_count, remaining)

    async def _invoke(
        self,
        handler_obj: Any,
        node: WorkflowNode,
        state_snapshot: dict[str, Any],
        workflow_run_id: str,
        step_count: int,
        remaining_node_timeout: float | None,
        *,
        use_node_io: bool = True,
    ) -> tuple[dict[str, Any], str | None]:
        """Call one handler function or module and return (state update, branch)."""
        from nexura_agents.contracts.base import BaseModule

        if isinstance(handler_obj, BaseModule) or hasattr(handler_obj, "execute"):
            from nexura_agents.contracts.context import ExecutionContext

            identity = _STEP_IDENTITY.get()

            ctx = ExecutionContext(
                workspace_id=self.workspace_id,
                run_id=workflow_run_id,
                node_id=node.id,
                attempt_id=f"att_{step_count}",
                deadline=(time.time() + remaining_node_timeout) if remaining_node_timeout else None,
                budget=self.budget,
                event_sink=self.event_sink,
                artifact_store=self.artifact_store,
                credential_resolver=self.credential_resolver,
                approval_granted=_NODE_APPROVED.get(),
                step_key=str(identity.step_key) if identity is not None else None,
                fencing_token=identity.fencing_token if identity is not None else None,
            )

            inputs: dict[str, Any] = {}
            if node.input_mapping and use_node_io:
                inputs = _map_inputs(node.input_mapping, state_snapshot)
            else:
                inputs = copy.deepcopy(state_snapshot)

            config = copy.deepcopy(node.config or {})

            if remaining_node_timeout is not None:
                mod_res = await asyncio.wait_for(
                    handler_obj.execute(ctx, inputs, config),
                    timeout=remaining_node_timeout,
                )
            else:
                mod_res = await handler_obj.execute(ctx, inputs, config)

            if mod_res.status != "success":
                raise WorkflowExecutionError(
                    node_id=node.id,
                    message=mod_res.error_message or f"Module '{node.handler}' invocation failed.",
                )

            state_update: dict[str, Any] = {}
            if node.output_mapping and use_node_io:
                for port_name, target_key in node.output_mapping.items():
                    if port_name in mod_res.outputs:
                        state_update[target_key] = copy.deepcopy(mod_res.outputs[port_name])
            else:
                state_update = copy.deepcopy(mod_res.outputs)

            branch = mod_res.branch or (
                mod_res.outputs.get("_branch") if isinstance(mod_res.outputs, dict) else None
            )
            return state_update, branch
        else:
            handler_fn = handler_obj
            if inspect.iscoroutinefunction(handler_fn):
                if remaining_node_timeout is not None:
                    raw_output = await asyncio.wait_for(
                        handler_fn(state_snapshot),
                        timeout=remaining_node_timeout,
                    )
                else:
                    raw_output = await handler_fn(state_snapshot)
            else:
                if remaining_node_timeout is not None:
                    raw_output = await asyncio.wait_for(
                        asyncio.to_thread(handler_fn, state_snapshot),
                        timeout=remaining_node_timeout,
                    )
                else:
                    raw_output = await asyncio.to_thread(handler_fn, state_snapshot)

            state_update, branch = _parse_handler_output(raw_output)
        return state_update, branch

    async def _run_parallel(
        self,
        node: WorkflowNode,
        state: dict[str, Any],
        workflow_run_id: str,
        step_count: int,
        remaining: float | None,
    ) -> tuple[dict[str, Any], str | None]:
        async def branch(name: str) -> Any:
            _INTERRUPT_STATE.set(_InterruptState([], blocked=_NO_PAUSE_IN_PARALLEL))
            with _child_span(f"workflow.branch {name}", {"nexura.workflow.branch": name}):
                return await self._run_part(
                    node,
                    workflow_run_id,
                    state,
                    functools.partial(_branch, name),
                    branch=name,
                )

        async def _branch(name: str) -> dict[str, Any]:
            handler = self._resolve_handler(name)
            update, chosen = await self._invoke(
                handler,
                node,
                copy.deepcopy(state),
                workflow_run_id,
                step_count,
                remaining,
                use_node_io=False,
            )
            if chosen:
                raise WorkflowValidationError(
                    f"Parallel branch '{name}' of node '{node.id}' chose transition "
                    f"'{chosen}'; parallel steps continue via the node's default_transition."
                )
            return update

        updates = _raise_uncertain(
            await _gather_limited(
                [functools.partial(branch, name) for name in node.parallel], node.max_concurrency
            )
        )
        return _merge_branch_updates(node.id, updates, node.reducers, state), None

    async def _run_map(
        self,
        node: WorkflowNode,
        handler_obj: Any,
        state: dict[str, Any],
        workflow_run_id: str,
        step_count: int,
        attempt: int,
        remaining: float | None,
    ) -> tuple[dict[str, Any], str | None]:
        items = state.get(node.map_over or "")
        if not isinstance(items, list):
            raise WorkflowValidationError(
                f"Node '{node.id}' maps over '{node.map_over}', which must be a list in the "
                f"state; got {type(items).__name__}."
            )

        async def one(index: int, item: Any) -> Any:
            _INTERRUPT_STATE.set(_InterruptState([], blocked=_NO_PAUSE_IN_PARALLEL))
            with _child_span(f"workflow.map_item {index}", {"nexura.workflow.map_index": index}):
                if node.subgraph is not None:
                    # The child run journals its own steps; the item is that run.
                    try:
                        return await _item(index, item)
                    except _PartUncertain as uncertain:
                        return uncertain.parts[0]
                item_state = copy.deepcopy(state)
                item_state[node.map_as] = copy.deepcopy(item)
                item_state[f"{node.map_as}_index"] = index
                return await self._run_part(
                    node,
                    workflow_run_id,
                    item_state,
                    functools.partial(_item, index, item),
                    item=index,
                    digest_of=self._item_inputs(node, state, item, index),
                )

        async def _item(index: int, item: Any) -> dict[str, Any]:
            item_state = copy.deepcopy(state)
            item_state[node.map_as] = copy.deepcopy(item)
            item_state[f"{node.map_as}_index"] = index
            if node.subgraph is not None:
                return await self._run_subgraph(
                    node, item_state, workflow_run_id, step_count, attempt, remaining, index
                )
            update, chosen = await self._invoke(
                handler_obj, node, item_state, workflow_run_id, step_count, remaining
            )
            if chosen:
                raise WorkflowValidationError(
                    f"Item {index} of node '{node.id}' chose transition '{chosen}'; mapped "
                    "steps continue via the node's default_transition."
                )
            return update

        updates = _raise_uncertain(
            await _gather_limited(
                [functools.partial(one, i, item) for i, item in enumerate(items)],
                node.max_concurrency,
            )
        )
        return _merge_branch_updates(node.id, updates, node.reducers, state), None

    async def _run_part(
        self,
        node: WorkflowNode,
        workflow_run_id: str,
        part_state: dict[str, Any],
        call: Callable[[], Awaitable[dict[str, Any]]],
        *,
        item: int | None = None,
        branch: str | None = None,
        digest_of: Any = None,
    ) -> dict[str, Any] | _UncertainPart:
        """Run one parallel branch or mapped item under its own journal step (A6 S7).

        A part whose outcome was committed is not run again: its recorded update is used. A
        part dispatched without an outcome by a process that stopped follows §2.5 with its
        own key: ``pure``/``idempotent`` run again with the same key, ``reconcilable`` is
        probed, ``non_idempotent`` (or a probe answering ``Unknown``) is returned as in
        doubt; a person's resolution of the part is applied. A non-pure part writes its
        intent just before it runs; every part's outcome is committed when it returns.
        """
        parent = _STEP_IDENTITY.get()
        if parent is None:
            return await call()
        family, first = _part_family(parent.step_key, item, branch)
        journal = self._journal_for(workflow_run_id)
        if journal is None:
            _STEP_IDENTITY.set(dataclasses.replace(parent, step_key=family(first)))
            scope = self._dispatch_scope(workflow_run_id, family(first))
            try:
                return await call()
            finally:
                _linkage.leave_dispatch_scope(scope)
        if branch is not None:
            effect, reconcile = self._handler_effect(node, branch, None, part_state)
            label = f"branch '{branch}' of node '{node.id}'"
        else:
            effect, reconcile = self._handler_effect(node, node.handler, node.module_id, part_state)
            label = f"item {item} of node '{node.id}'"
        attempt, key, record = journal.latest_of(family, first)
        if record is not None:
            outcome = record.outcome
            if outcome is not None and outcome.status == "completed":
                output = outcome.output if isinstance(outcome.output, dict) else {}
                return copy.deepcopy(output.get("update") or {})
            if record.intent is not None and str(key) not in journal.local:
                decided = await self._part_after_crash(
                    journal, node, record.intent, label, reconcile, part_state, item, branch
                )
                if isinstance(decided, _UncertainPart):
                    return decided
                if isinstance(decided, dict):
                    await journal.record_part(key, decided, node.id)
                    return decided
                if decided == "new_attempt":
                    key = family(attempt + 1)
        if effect != EffectClass.PURE:
            try:
                digest = canonical_digest(
                    digest_of if digest_of is not None else self._gate_binding(node, part_state)
                )
            except (TypeError, ValueError):
                digest = ""
            await journal.record_intent(
                StepIntent(
                    key,
                    digest,
                    default_idempotency_key(journal.eid, key),
                    effect,
                    approval_id=parent.approval_id,
                ),
                node.id,
            )
        else:
            journal.check()
            journal.local.add(str(key))
        _STEP_IDENTITY.set(dataclasses.replace(parent, step_key=key))
        scope = self._dispatch_scope(workflow_run_id, key)
        try:
            update = await call()
        finally:
            _linkage.leave_dispatch_scope(scope)
        _validate_json_serializable(update, node.id)
        await journal.record_part(key, update, node.id)
        return update

    async def _part_after_crash(
        self,
        journal: WorkflowJournal,
        node: WorkflowNode,
        intent: StepIntent,
        label: str,
        reconcile: Callable[..., Any] | None,
        part_state: dict[str, Any],
        item: int | None,
        branch: str | None,
    ) -> dict[str, Any] | _UncertainPart | str:
        """A part dispatched by a process that stopped: its update, "rerun", "new_attempt"
        or an ``_UncertainPart`` (also used for "a person resolved it as failed")."""
        effect = journal.effects.get(str(intent.step_key))

        def doubtful(failed: str | None = None) -> _UncertainPart:
            assert effect is not None
            view = self._pending_effect_view(effect, node.id)
            view["part"] = "branch" if branch is not None else "item"
            if branch is not None:
                view["branch"] = branch
            else:
                view["item"] = item
            view["run_id"] = journal.run_id
            return _UncertainPart(view, [str(intent.step_key)], failed)

        if effect is None:
            return "rerun"
        if effect.status == EffectStatus.RESOLVED:
            if effect.resolution == EffectResolution.CONFIRMED:
                update, _ = _parse_handler_output(copy.deepcopy(effect.result))
                return update
            if effect.resolution == EffectResolution.FAILED:
                return doubtful(
                    f"The {label} failed: its effect was resolved as failed by "
                    f"'{effect.resolved_by}'."
                )
            return "new_attempt"  # not_applied: a new attempt with a new key
        if effect.status == EffectStatus.INDETERMINATE:
            return doubtful()  # still waiting for a person
        if intent.effect_class in (EffectClass.PURE, EffectClass.IDEMPOTENT):
            return "rerun"
        if intent.effect_class == EffectClass.RECONCILABLE and reconcile is not None:
            probe = await self._probe(f"the {label}", reconcile, intent, part_state)
            if isinstance(probe, Applied):
                update, _ = _parse_handler_output(copy.deepcopy(probe.result))
                return update
            if isinstance(probe, NotApplied):
                return "rerun"
        return doubtful()

    def _child_engine(
        self, spec: WorkflowSpec, timeout: float | None, parent_run_id: str | None = None
    ) -> WorkflowEngine:
        child = WorkflowEngine(
            spec,
            self.registry,
            store=self.store,
            max_steps=self.max_steps,
            timeout=timeout,
            workspace_id=self.workspace_id,
            budget=self.budget,
            event_sink=self.event_sink,
            artifact_store=self.artifact_store,
            credential_resolver=self.credential_resolver,
            approval_required_handlers=self.approval_required_handlers,
            approval_ttl=self.approval_ttl,
            policy_version=self.policy_version,
            max_map_writes_per_approval=self.max_map_writes_per_approval,
        )
        # A sub-workflow inside a mapped item cannot pause either, at any depth.
        child._pause_forbidden = self._pause_forbidden
        child.lease_ttl = self.lease_ttl
        # A child journals under its own run id, linked to the run that started it (A6 S7).
        child._journal_enabled = self._journal_enabled
        child._parent_run_id = parent_run_id
        return child

    async def _run_subgraph(
        self,
        node: WorkflowNode,
        state: dict[str, Any],
        workflow_run_id: str,
        step_count: int,
        attempt: int,
        remaining: float | None,
        item: int | None,
    ) -> dict[str, Any]:
        """Run (or re-attach to) the node's sub-workflow and return its state update."""
        assert node.subgraph is not None
        child_id = _child_run_id(workflow_run_id, node.id, step_count, attempt, item)
        child = self._child_engine(node.subgraph, remaining, workflow_run_id)
        if item is not None:
            child._pause_forbidden = _NO_PAUSE_IN_PARALLEL
        inputs = _map_inputs(node.input_mapping, state)

        existing = await self.store.get(child_id) if self.store is not None else None
        if existing is None:
            result = await child.run(inputs, workflow_run_id=child_id)
        elif child._journaled_store() and (
            existing.status in ("running", "interrupted")
            or (existing.status == "paused" and existing.pending_effect is not None)
        ):
            # A process stopped mid-way (or a person resolved the child's unknown effect):
            # recover the child under its own journal, with this run's on_uncertain.
            try:
                result = await child.recover(
                    child_id,
                    on_uncertain="fail" if _ON_UNCERTAIN.get() == "fail" else "pause",
                )
            except _LeaseBusyError as busy:
                raise _ChildLeaseBusy(str(busy)) from None
        elif existing.status == "running":  # the process stopped mid-way: continue it
            result = await child._execute_loop(
                child_id,
                existing.current_node_id,
                copy.deepcopy(existing.state),
                existing.step_count,
                False,
            )
        else:
            result = WorkflowRunResult(
                workflow_run_id=child_id,
                status=existing.status,
                state=copy.deepcopy(existing.state),
                step_count=existing.step_count,
                current_node_id=existing.current_node_id,
                pending_approval=existing.pending_approval,
                error=existing.error,
                interrupt=(
                    {k: existing.interrupt[k] for k in ("interrupt_id", "node_id", "value")}
                    if existing.interrupt
                    else None
                ),
                pause_reason="effect_unknown" if existing.pending_effect is not None else None,
                pending_effect=copy.deepcopy(existing.pending_effect),
            )

        if result.status == "paused" and result.pending_effect is not None:
            # The child stopped on an unknown effect: this run pauses on it too.
            view = {
                **copy.deepcopy(result.pending_effect),
                "node_id": f"{node.id}/{result.pending_effect.get('node_id')}",
                "part": "subworkflow",
                "run_id": result.pending_effect.get("run_id") or child_id,
            }
            if item is not None:
                view["item"] = item
            raise _PartUncertain([_UncertainPart(view)])
        if result.status == "paused":
            if item is not None:
                raise WorkflowValidationError(_NO_PAUSE_IN_PARALLEL)
            raise _SubgraphPaused(result)
        if result.status != "completed":
            raise WorkflowExecutionError(
                node_id=node.id,
                message=f"Sub-workflow run '{child_id}' ended '{result.status}': {result.error}",
            )
        final = result.state
        if node.output_mapping:
            return {
                target: copy.deepcopy(final[key])
                for key, target in node.output_mapping.items()
                if key in final
            }
        # Without a mapping, return what the sub-workflow wrote, added or changed (a
        # write of an unchanged value still counts, so reducers see every item).
        return {
            key: copy.deepcopy(value)
            for key, value in final.items()
            if key in child._written or key not in inputs or inputs[key] != value
        }

    async def _pause_for_subgraph(
        self,
        workflow_run_id: str,
        node: WorkflowNode,
        state: dict[str, Any],
        step_count: int,
        child: WorkflowRunResult,
        *,
        node_approved: bool = False,
    ) -> WorkflowRunResult:
        """Pause this run on the sub-workflow's approval or question, under a node path."""
        pending: PendingApproval | None = None
        payload: dict[str, Any] | None = None
        public: dict[str, Any] | None = None
        if child.pending_approval is not None:
            pending = dataclasses.replace(
                child.pending_approval,
                workflow_run_id=workflow_run_id,
                node_id=f"{node.id}/{child.pending_approval.node_id}",
            )
        elif child.interrupt is not None:
            public = {
                "interrupt_id": child.interrupt["interrupt_id"],
                "node_id": f"{node.id}/{child.interrupt['node_id']}",
                "value": child.interrupt["value"],
            }
            payload = {
                **public,
                "answers": [],
                "workspace_id": self.workspace_id,
                "approved_binding": (self._binding_digest(node, state) if node_approved else None),
            }
        budget_state = (
            self.budget.to_dict()
            if self.budget is not None and hasattr(self.budget, "to_dict")
            else None
        )
        if self.store is not None:
            await self._save(
                WorkflowCheckpoint(
                    workflow_run_id=workflow_run_id,
                    spec_version=self.spec.spec_version,
                    spec_hash=self.spec.canonical_hash(),
                    current_node_id=node.id,
                    state=copy.deepcopy(state),
                    step_count=step_count,
                    status="paused",
                    pending_approval=pending,
                    budget_state=budget_state,
                    interrupt=payload,
                )
            )
        return WorkflowRunResult(
            workflow_run_id=workflow_run_id,
            status="paused",
            state=copy.deepcopy(state),
            step_count=step_count,
            current_node_id=node.id,
            pending_approval=pending,
            budget_status=budget_state.get("status") if budget_state else None,
            interrupt=public,
        )

    async def _pause_for_node_request(
        self,
        workflow_run_id: str,
        node: WorkflowNode,
        state: dict[str, Any],
        step_count: int,
        details: dict[str, Any],
    ) -> WorkflowRunResult:
        """Pause on the approval path for a node's own :func:`request_approval` call."""
        expires_at = time.time() + self.approval_ttl if self.approval_ttl is not None else None
        pending = PendingApproval(
            approval_id=str(uuid.uuid4()),
            workflow_run_id=workflow_run_id,
            node_id=node.id,
            input_state=copy.deepcopy(state),
            created_at=time.time(),
            workspace_id=self.workspace_id,
            workflow_version=str(self.spec.version if self.spec.version is not None else "1.0.0"),
            spec_hash=self.spec.canonical_hash(),
            required_role=getattr(node, "required_role", "admin"),
            expires_at=expires_at,
            policy_version=self.policy_version,
            # Bound to the state and to the exact request the approver is shown.
            payload_digest=canonical_digest({"state": state, "request": details}),
            status="pending",
            details=copy.deepcopy(details),
            canonical_payload_json=canonical_json({"state": state, "request": details}),
            **_take_regate_provenance(node.id),
        )
        tracing.approval_requested(
            None, pending.approval_id, kind="workflow_node", reason="node_request", node_id=node.id
        )
        budget_state = (
            self.budget.to_dict()
            if self.budget is not None and hasattr(self.budget, "to_dict")
            else None
        )
        if self.store is not None:
            await self._save(
                WorkflowCheckpoint(
                    workflow_run_id=workflow_run_id,
                    spec_version=self.spec.spec_version,
                    spec_hash=self.spec.canonical_hash(),
                    current_node_id=node.id,
                    state=copy.deepcopy(state),
                    step_count=step_count,
                    status="paused",
                    pending_approval=pending,
                    budget_state=budget_state,
                )
            )
        return WorkflowRunResult(
            workflow_run_id=workflow_run_id,
            status="paused",
            state=copy.deepcopy(state),
            step_count=step_count,
            current_node_id=node.id,
            pending_approval=pending,
            budget_status=budget_state.get("status") if budget_state else None,
        )

    async def _pause_part_uncertain(
        self,
        workflow_run_id: str,
        node: WorkflowNode,
        state: dict[str, Any],
        step_count: int,
        uncertain: _PartUncertain,
    ) -> WorkflowRunResult:
        """A branch, item or child is in doubt: pause ``effect_unknown`` naming it.

        With ``on_uncertain="fail"`` (or a part a person resolved as failed) the run fails
        instead. The other parts of the step already finished and recorded their outcomes.
        """
        failure = uncertain.failed
        if failure is None and _ON_UNCERTAIN.get() == "fail":
            failure = (
                f"Node '{node.id}': part '{uncertain.pending.get('step_key')}' was dispatched "
                "before the process stopped and its outcome is unknown (on_uncertain='fail')."
            )
        budget_state = self._budget_state()
        if failure is not None:
            self._progress(
                RunEventType.NODE_FAILED, node_id=node.id, step=step_count + 1, error=failure
            )
            failed = WorkflowCheckpoint(
                workflow_run_id=workflow_run_id,
                spec_version=self.spec.spec_version,
                spec_hash=self.spec.canonical_hash(),
                current_node_id=node.id,
                state=copy.deepcopy(state),
                step_count=step_count,
                status="failed",
                error=failure,
                budget_state=budget_state,
            )
            if self.store is not None:
                await self._save(failed)
            return self._result_from_checkpoint(failed)
        pending = copy.deepcopy(uncertain.pending)
        journal = self._journal_for(workflow_run_id)
        if journal is not None:
            await journal.pause(
                "effect_unknown",
                node.id,
                uncertain.keys,
                effect_id=str(pending.get("effect_id") or "") or None,
            )
        paused = WorkflowCheckpoint(
            workflow_run_id=workflow_run_id,
            spec_version=self.spec.spec_version,
            spec_hash=self.spec.canonical_hash(),
            current_node_id=node.id,
            state=copy.deepcopy(state),
            step_count=step_count,
            status="paused",
            pending_effect=pending,
            budget_state=budget_state,
        )
        if self.store is not None:
            await self._save(paused)
        self._progress(
            RunEventType.NODE_PAUSED,
            node_id=node.id,
            step=step_count + 1,
            reason="effect_unknown",
            workflow_run_id=workflow_run_id,
        )
        return self._result_from_checkpoint(paused)

    @staticmethod
    def _waits_on_subgraph(node: WorkflowNode, pending_node_id: str) -> bool:
        return node.subgraph is not None and pending_node_id.startswith(f"{node.id}/")

    async def _paused_child(
        self, workflow_run_id: str, node: WorkflowNode, step: int, pending_id: str
    ) -> tuple[WorkflowEngine, str]:
        assert self.store is not None and node.subgraph is not None
        attempts = NodeRetryPolicy.from_dict(node.retry, node.id).max_attempts
        for attempt in range(1, attempts + 1):
            child_id = _child_run_id(workflow_run_id, node.id, step, attempt, None)
            cp = await self.store.get(child_id)
            if cp is not None and cp.status == "paused" and _pending_id(cp) == pending_id:
                return self._child_engine(node.subgraph, self.timeout, workflow_run_id), child_id
        raise WorkflowError(
            f"No sub-workflow of node '{node.id}' in run '{workflow_run_id}' is waiting on "
            f"'{pending_id}'."
        )

    async def _paused_checkpoint(self, workflow_run_id: str) -> WorkflowCheckpoint:
        if self.store is None:
            raise WorkflowError("Cannot resume workflow without a configured checkpoint store.")
        checkpoint = await self.store.get(workflow_run_id)
        if checkpoint is None:
            raise WorkflowExecutionError(
                node_id="unknown", message=f"Workflow run '{workflow_run_id}' not found."
            )
        if checkpoint.status in ("completed", "failed", "rejected"):
            raise WorkflowTerminalError(
                f"Cannot resume workflow run '{workflow_run_id}' in terminal state "
                f"'{checkpoint.status}'."
            )
        current_hash = self.spec.canonical_hash()
        if checkpoint.spec_hash != current_hash:
            raise WorkflowSpecMismatchError(
                expected_hash=checkpoint.spec_hash, actual_hash=current_hash
            )
        if checkpoint.status != "paused":
            raise ApprovalError(f"Workflow '{workflow_run_id}' is not paused.")
        return checkpoint

    def _start_workflow_span(self, workflow_run_id: str, operation: str) -> Any:
        span = tracing.start_workflow(
            self.spec.name,
            {
                "nexura.workflow.name": self.spec.name,
                "nexura.workflow.run_id": workflow_run_id,
                "nexura.workflow.operation": operation,
            },
        )
        decided = _TRACE_DECISION.get()
        if decided is not None:
            _TRACE_DECISION.set(None)
            tracing.approval_decided(span, **decided)
        return span

    async def _execute_loop(
        self,
        workflow_run_id: str,
        cursor: str | None,
        current_state: dict[str, Any],
        step_count: int,
        node_already_approved: bool,
        resume_answers: list[Any] | None = None,
        operation: str = "run",
        approval_id: str | None = None,
    ) -> WorkflowRunResult:
        """Run the graph inside a ``workflow`` span when tracing is on."""
        span = self._start_workflow_span(workflow_run_id, operation)
        if span is tracing.NOOP:
            return await self._execute_steps(
                workflow_run_id,
                cursor,
                current_state,
                step_count,
                node_already_approved,
                resume_answers,
                approval_id,
            )
        if span.capturing:
            span.content("nexura.workflow.input", "nexura.workflow.state", _redacted(current_state))
        span.attach()  # node spans, and sub-workflows below them, nest under this span
        status = "failed"
        steps = step_count
        try:
            result = await self._execute_steps(
                workflow_run_id,
                cursor,
                current_state,
                step_count,
                node_already_approved,
                resume_answers,
                approval_id,
            )
            status, steps = result.status, result.step_count
            if span.capturing:
                span.content(
                    "nexura.workflow.output", "nexura.workflow.state", _redacted(result.state)
                )
            return result
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except Exception as exc:
            span.fail(exc)
            raise
        finally:
            span.set("nexura.workflow.status", status)
            span.set("nexura.workflow.step_count", steps)
            span.end()

    async def _execute_steps(
        self,
        workflow_run_id: str,
        cursor: str | None,
        current_state: dict[str, Any],
        step_count: int,
        node_already_approved: bool,
        resume_answers: list[Any] | None = None,
        approval_id: str | None = None,
    ) -> WorkflowRunResult:
        start_time = time.monotonic()
        cursor_ref: list[str | None] = [cursor]

        while cursor_ref[0] is not None:
            active_node_id = cursor_ref[0]
            # 1. Enforce max_steps bound (limit accounting survives resume)
            if step_count >= self.max_steps:
                max_steps_err = WorkflowMaxStepsError(step_count, self.max_steps)
                fail_cp = WorkflowCheckpoint(
                    workflow_run_id=workflow_run_id,
                    spec_version=self.spec.spec_version,
                    spec_hash=self.spec.canonical_hash(),
                    current_node_id=active_node_id,
                    state=copy.deepcopy(current_state),
                    step_count=step_count,
                    status="failed",
                    error=str(max_steps_err),
                )
                if self.store is not None:
                    await self._save(fail_cp)
                raise max_steps_err

            # 2. Enforce whole-workflow timeout
            if self.timeout is not None:
                elapsed = time.monotonic() - start_time
                if elapsed >= self.timeout:
                    timeout_err = WorkflowTimeoutError(self.timeout, elapsed)
                    fail_cp = WorkflowCheckpoint(
                        workflow_run_id=workflow_run_id,
                        spec_version=self.spec.spec_version,
                        spec_hash=self.spec.canonical_hash(),
                        current_node_id=active_node_id,
                        state=copy.deepcopy(current_state),
                        step_count=step_count,
                        status="failed",
                        error=str(timeout_err),
                    )
                    if self.store is not None:
                        await self._save(fail_cp)
                    raise timeout_err

            # Check if active_node_id is a terminal label (not an executable node)
            if active_node_id not in self.spec.nodes and active_node_id in self.spec.terminal_nodes:
                break

            node = self.spec.nodes.get(active_node_id)
            if node is None:
                raise WorkflowValidationError(
                    f"Unknown node ID encountered during execution: '{active_node_id}'."
                )

            # 3. Handle approval requirement: pauses BEFORE handler runs
            provenance = _REGATE_PROVENANCE.get()
            migrated_gate = (
                provenance is not None
                and provenance.get("node_id") == node.id
                and provenance.get("regate_reason") == "definition_migrated"
            )
            is_approval_needed = (
                node.requires_approval
                or migrated_gate
                or (
                    node.handler in self.approval_required_handlers
                    or (
                        node.module_id is not None
                        and node.module_id in self.approval_required_handlers
                    )
                )
                or self._write_operation_gated(node, current_state)
            )
            if is_approval_needed and not node_already_approved:
                try:
                    self._check_map_write_cap(node, current_state)
                except WorkflowValidationError as cap_err:
                    if self.store is not None:
                        await self._save(
                            WorkflowCheckpoint(
                                workflow_run_id=workflow_run_id,
                                spec_version=self.spec.spec_version,
                                spec_hash=self.spec.canonical_hash(),
                                current_node_id=node.id,
                                state=copy.deepcopy(current_state),
                                step_count=step_count,
                                status="failed",
                                error=str(cap_err),
                            )
                        )
                    raise
                # Inputs the module will refuse anyway fail the run here, without asking a
                # reviewer to approve them first.
                refusal = self._pre_approval_refusal(node, current_state)
                if refusal is not None:
                    error = f"Node '{node.id}' refused before approval: {refusal}"
                    self._progress(
                        RunEventType.NODE_FAILED,
                        node_id=node.id,
                        step=step_count + 1,
                        error=error,
                    )
                    if self.store is not None:
                        await self._save(
                            WorkflowCheckpoint(
                                workflow_run_id=workflow_run_id,
                                spec_version=self.spec.spec_version,
                                spec_hash=self.spec.canonical_hash(),
                                current_node_id=node.id,
                                state=copy.deepcopy(current_state),
                                step_count=step_count,
                                status="failed",
                                error=error,
                            )
                        )
                    return WorkflowRunResult(
                        workflow_run_id=workflow_run_id,
                        status="failed",
                        state=copy.deepcopy(current_state),
                        step_count=step_count,
                        current_node_id=node.id,
                        error=error,
                    )
                approval_id = str(uuid.uuid4())
                payload_digest = canonical_digest(current_state)
                expires_at = (
                    time.time() + self.approval_ttl if self.approval_ttl is not None else None
                )
                pending = PendingApproval(
                    approval_id=approval_id,
                    workflow_run_id=workflow_run_id,
                    node_id=node.id,
                    input_state=copy.deepcopy(current_state),
                    created_at=time.time(),
                    workspace_id=self.workspace_id,
                    workflow_version=str(
                        self.spec.version if self.spec.version is not None else "1.0.0"
                    ),
                    spec_hash=self.spec.canonical_hash(),
                    required_role=getattr(node, "required_role", "admin"),
                    expires_at=expires_at,
                    policy_version=self.policy_version,
                    payload_digest=payload_digest,
                    status="pending",
                    details=self._approval_details(node, current_state),
                    canonical_payload_json=canonical_json(current_state),
                    **_take_regate_provenance(node.id),
                )
                budget_state = (
                    self.budget.to_dict()
                    if self.budget is not None and hasattr(self.budget, "to_dict")
                    else None
                )
                checkpoint = WorkflowCheckpoint(
                    workflow_run_id=workflow_run_id,
                    spec_version=self.spec.spec_version,
                    spec_hash=self.spec.canonical_hash(),
                    current_node_id=node.id,
                    state=copy.deepcopy(current_state),
                    step_count=step_count,
                    status="paused",
                    pending_approval=pending,
                    budget_state=budget_state,
                )
                if self.store is not None:
                    await self._save(checkpoint)

                gate_span = _node_span(node, step_count + 1)
                gate_span.set("nexura.workflow.pause_reason", "approval")
                tracing.approval_requested(
                    gate_span, pending.approval_id, kind="workflow_node", node_id=node.id
                )
                gate_span.end()
                self._progress(
                    RunEventType.NODE_PAUSED,
                    node_id=node.id,
                    step=step_count + 1,
                    reason="approval",
                    workflow_run_id=workflow_run_id,
                )
                budget_status = budget_state.get("status") if budget_state is not None else None
                return WorkflowRunResult(
                    workflow_run_id=workflow_run_id,
                    status="paused",
                    state=copy.deepcopy(current_state),
                    step_count=step_count,
                    current_node_id=node.id,
                    pending_approval=pending,
                    budget_status=budget_status,
                )

            # Node execution permitted. Only an approved node hands modules the approval
            # marker; the flag is reset for future nodes.
            node_approved_now = node_already_approved
            node_already_approved = False
            # The approval this visit runs under (recorded with its intent), first node only.
            node_approval_id = approval_id if node_approved_now else None
            approval_id = None

            # 4. Execute node handler
            remaining_node_timeout: float | None = None
            if self.timeout is not None:
                elapsed = time.monotonic() - start_time
                if elapsed >= self.timeout:
                    timeout_err = WorkflowTimeoutError(self.timeout, elapsed)
                    fail_cp = WorkflowCheckpoint(
                        workflow_run_id=workflow_run_id,
                        spec_version=self.spec.spec_version,
                        spec_hash=self.spec.canonical_hash(),
                        current_node_id=node.id,
                        state=copy.deepcopy(current_state),
                        step_count=step_count,
                        status="failed",
                        error=str(timeout_err),
                    )
                    if self.store is not None:
                        await self._save(fail_cp)
                    raise timeout_err
                remaining_node_timeout = self.timeout - elapsed

            # Resolve handler callable or BaseModule (composite steps resolve their own)
            handler_obj: Any = None
            if node.subgraph is None and not node.parallel:
                handler_obj = self._resolve_handler(
                    node.handler, node.module_id, node.module_version
                )

            step_count += 1
            if self.budget is not None and hasattr(self.budget, "record_turn"):
                self.budget.record_turn()
            # Journaled runs: the intent of a non-pure node is committed here, just before
            # it dispatches (a stale owner is refused here, before any effect).
            identity = await self._before_dispatch(
                workflow_run_id, node, current_state, step_count, node_approval_id
            )

            self._progress(
                RunEventType.NODE_STARTED,
                node_id=node.id,
                step=step_count,
                workflow_run_id=workflow_run_id,
            )
            retry_policy = NodeRetryPolicy.from_dict(node.retry, node.id)
            # Answers from resume_interrupt apply only to the node that asked for them.
            interrupt_state = _InterruptState(
                list(resume_answers or []), blocked=self._pause_forbidden
            )
            resume_answers = None
            interrupt_token = _INTERRUPT_STATE.set(interrupt_state)
            attempt = 0
            node_span = _node_span(node, step_count)
            node_span.attach()  # handlers, parallel branches and sub-workflows nest under it
            if node_span.capturing:
                node_span.content(
                    "nexura.workflow.input", "nexura.workflow.state", _redacted(current_state)
                )
            approved_token = _NODE_APPROVED.set(node_approved_now)
            node_grant = _RESUME_GRANT.get() if node_approved_now else None
            if node_grant is not None:
                _RESUME_GRANT.set(None)  # consumed by this step only
            grant_token = _NODE_GRANT.set(node_grant)
            identity_token = _STEP_IDENTITY.set(identity)
            scope_token = self._dispatch_scope(workflow_run_id, identity.step_key)
            try:
                while True:
                    attempt += 1
                    # Every attempt replays the stored interrupt answers from the first.
                    interrupt_state.index = 0
                    handler_completed = False
                    try:
                        if attempt > 1 and self.timeout is not None:
                            # Earlier attempts and backoff used part of the deadline.
                            remaining_node_timeout = self.timeout - (time.monotonic() - start_time)
                            if remaining_node_timeout <= 0:
                                raise TimeoutError
                        state_update, branch = await self._run_step(
                            node,
                            handler_obj,
                            copy.deepcopy(current_state),
                            workflow_run_id,
                            step_count,
                            attempt,
                            remaining_node_timeout,
                        )
                        handler_completed = True
                        # Validate before publishing completion or applying any output.
                        _validate_json_serializable(state_update, node.id)
                        next_node_id: str | None = None
                        if active_node_id in self.spec.terminal_nodes:
                            next_node_id = None
                        elif branch is not None:
                            if branch not in node.transitions:
                                raise WorkflowExecutionError(
                                    node_id=node.id,
                                    message=(
                                        f"Handler chose branch '{branch}', but node '{node.id}' "
                                        "defines no such transition."
                                    ),
                                )
                            next_node_id = node.transitions[branch]
                        else:
                            next_node_id = node.default_transition or node.transitions.get(
                                "default"
                            )
                    except TimeoutError as exc:
                        elapsed = time.monotonic() - start_time
                        node_timeout_err = WorkflowTimeoutError(self.timeout or 0.0, elapsed)
                        node_span.fail(node_timeout_err)
                        self._progress(
                            RunEventType.NODE_FAILED,
                            node_id=node.id,
                            step=step_count,
                            error=str(node_timeout_err),
                        )
                        fail_cp = WorkflowCheckpoint(
                            workflow_run_id=workflow_run_id,
                            spec_version=self.spec.spec_version,
                            spec_hash=self.spec.canonical_hash(),
                            current_node_id=node.id,
                            state=copy.deepcopy(current_state),
                            step_count=step_count,
                            status="failed",
                            error=str(node_timeout_err),
                        )
                        if self.store is not None:
                            await self._save(fail_cp)
                        raise node_timeout_err from exc
                    except Exception as exc:
                        if (
                            not handler_completed
                            and attempt < retry_policy.max_attempts
                            and retry_policy.retries(exc)
                        ):
                            wait = retry_policy.delay(attempt)
                            within_deadline = self.timeout is None or (
                                time.monotonic() - start_time + wait < self.timeout
                            )
                            if within_deadline:
                                node_span.event(
                                    "nexura.workflow.retry",
                                    {"attempt": attempt, "error.type": type(exc).__name__},
                                )
                                await asyncio.sleep(wait)
                                continue
                        exec_err = WorkflowExecutionError(
                            node_id=node.id,
                            message=str(exc),
                            original_error=exc,
                        )
                        node_span.fail(exc)
                        self._progress(
                            RunEventType.NODE_FAILED,
                            node_id=node.id,
                            step=step_count,
                            error=f"{type(exc).__name__}: {exc}",
                        )
                        fail_cp = WorkflowCheckpoint(
                            workflow_run_id=workflow_run_id,
                            spec_version=self.spec.spec_version,
                            spec_hash=self.spec.canonical_hash(),
                            current_node_id=node.id,
                            state=copy.deepcopy(current_state),
                            step_count=step_count,
                            status="failed",
                            error=f"{type(exc).__name__}: {exc}",
                        )
                        if self.store is not None:
                            await self._save(fail_cp)
                        raise exec_err from exc

                    if node_span.capturing:
                        node_span.content(
                            "nexura.workflow.output",
                            "nexura.workflow.update",
                            _redacted(state_update),
                        )
                    break
            except _PartUncertain as uncertain:
                node_span.set("nexura.workflow.pause_reason", "effect_unknown")
                # The node did not finish: keep the pre-node state and step count.
                return await self._pause_part_uncertain(
                    workflow_run_id, node, current_state, step_count - 1, uncertain
                )
            except _ChildLeaseBusy as busy:
                raise _LeaseBusyError(busy.message) from None
            except _SubgraphPaused as child_paused:
                self._progress(
                    RunEventType.NODE_PAUSED,
                    node_id=node.id,
                    step=step_count,
                    reason="interrupt" if child_paused.result.interrupt is not None else "approval",
                    workflow_run_id=workflow_run_id,
                )
                node_span.set(
                    "nexura.workflow.pause_reason",
                    "interrupt" if child_paused.result.interrupt is not None else "approval",
                )
                return await self._pause_for_subgraph(
                    workflow_run_id,
                    node,
                    current_state,
                    step_count - 1,
                    child_paused.result,
                    node_approved=node_approved_now,
                )
            except _NodeApprovalRequested as asked:
                self._progress(
                    RunEventType.NODE_PAUSED,
                    node_id=node.id,
                    step=step_count,
                    reason="approval",
                    workflow_run_id=workflow_run_id,
                )
                node_span.set("nexura.workflow.pause_reason", "approval")
                # The node did not finish: keep the pre-node state and step count.
                return await self._pause_for_node_request(
                    workflow_run_id, node, current_state, step_count - 1, asked.details
                )
            except WorkflowInterrupt as paused_for_input:
                self._progress(
                    RunEventType.NODE_PAUSED,
                    node_id=node.id,
                    step=step_count,
                    reason="interrupt",
                    workflow_run_id=workflow_run_id,
                )
                node_span.set("nexura.workflow.pause_reason", "interrupt")
                interrupt_payload = {
                    "interrupt_id": str(uuid.uuid4()),
                    "node_id": node.id,
                    "value": paused_for_input.value,
                    "answers": list(interrupt_state.answers),
                    "workspace_id": self.workspace_id,
                    # Set only when this node ran with an approval granted: the binding it
                    # was approved for, so resume_interrupt re-grants it for these inputs.
                    "approved_binding": (
                        self._binding_digest(node, current_state) if node_approved_now else None
                    ),
                    "approved_request": copy.deepcopy(node_grant),
                }
                budget_state = (
                    self.budget.to_dict()
                    if self.budget is not None and hasattr(self.budget, "to_dict")
                    else None
                )
                # The node did not finish: keep the pre-node state and step count.
                paused_cp = WorkflowCheckpoint(
                    workflow_run_id=workflow_run_id,
                    spec_version=self.spec.spec_version,
                    spec_hash=self.spec.canonical_hash(),
                    current_node_id=node.id,
                    state=copy.deepcopy(current_state),
                    step_count=step_count - 1,
                    status="paused",
                    budget_state=budget_state,
                    interrupt=interrupt_payload,
                )
                if self.store is not None:
                    await self._save(paused_cp)
                return WorkflowRunResult(
                    workflow_run_id=workflow_run_id,
                    status="paused",
                    state=copy.deepcopy(current_state),
                    step_count=step_count - 1,
                    current_node_id=node.id,
                    budget_status=budget_state.get("status") if budget_state else None,
                    interrupt={
                        "interrupt_id": interrupt_payload["interrupt_id"],
                        "node_id": node.id,
                        "value": paused_for_input.value,
                    },
                )
            finally:
                _INTERRUPT_STATE.reset(interrupt_token)
                _NODE_APPROVED.reset(approved_token)
                _NODE_GRANT.reset(grant_token)
                _STEP_IDENTITY.reset(identity_token)
                _linkage.leave_dispatch_scope(scope_token)
                node_span.set("nexura.workflow.node.attempts", attempt)
                node_span.end()

            # Validate and update state
            current_state.update(state_update)
            self._written.update(state_update)
            self._progress(
                RunEventType.NODE_COMPLETED,
                node_id=node.id,
                step=step_count,
                attempts=attempt,
                branch=branch,
                updated_keys=sorted(state_update),
                workflow_run_id=workflow_run_id,
            )

            cursor_ref[0] = next_node_id

            # 6. Advance cursor, state, and step count durably
            # A terminal node that has a handler still has to run; only a bare terminal
            # name (or no next node) means the run is finished.
            is_terminal = cursor_ref[0] is None or (
                cursor_ref[0] in self.spec.terminal_nodes and cursor_ref[0] not in self.spec.nodes
            )
            budget_state = (
                self.budget.to_dict()
                if self.budget is not None and hasattr(self.budget, "to_dict")
                else None
            )
            checkpoint = WorkflowCheckpoint(
                workflow_run_id=workflow_run_id,
                spec_version=self.spec.spec_version,
                spec_hash=self.spec.canonical_hash(),
                current_node_id=cursor_ref[0],
                state=copy.deepcopy(current_state),
                step_count=step_count,
                status="completed" if is_terminal else "running",
                budget_state=budget_state,
            )
            journal = self._journal_for(workflow_run_id)
            if journal is not None:
                if is_terminal:  # open reservations end before the terminal commit (m1)
                    await self._settle_reservations(workflow_run_id, journal, "terminal")
                # Outcome, effect confirmed and the new cursor: one fenced commit, before
                # the checkpoint moves (recover() applies it if the save below is lost).
                await journal.record_outcome(
                    identity.step_key,
                    {"update": state_update, "branch": branch, "next": cursor_ref[0]},
                    cursor=cursor_ref[0],
                    terminal=is_terminal,
                    approval_id=node_approval_id,
                )
                await self._settle_deferred(workflow_run_id, journal)
            if self.store is not None:
                await self._save(checkpoint)

        budget_status = (
            self.budget.to_dict().get("status")
            if self.budget is not None and hasattr(self.budget, "to_dict")
            else None
        )
        return WorkflowRunResult(
            workflow_run_id=workflow_run_id,
            status="completed",
            state=copy.deepcopy(current_state),
            step_count=step_count,
            current_node_id=cursor_ref[0],
            budget_status=budget_status,
        )


# Workflow bundles from nexura_agents.repro replay through this engine.
from nexura_agents.repro import register_workflow_replay as _register_workflow_replay  # noqa: E402


async def _replay_workflow(*args: Any) -> Any:
    from nexura_runtime.repro_workflow import replay_workflow

    return await replay_workflow(*args)


_register_workflow_replay(_replay_workflow)
