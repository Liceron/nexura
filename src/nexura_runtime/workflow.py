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
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NoReturn, Protocol, runtime_checkable

from nexura_agents import tracing
from nexura_agents.contracts.canonical import canonical_digest, canonical_json
from nexura_agents.errors import (
    ApprovalAlreadyClaimedError,
    ApprovalError,
    UnsupportedWorkflowVersionError,
    WorkflowError,
    WorkflowExecutionError,
    WorkflowMaxStepsError,
    WorkflowSpecMismatchError,
    WorkflowTerminalError,
    WorkflowTimeoutError,
    WorkflowValidationError,
)
from nexura_agents.events import RunEvent, RunEventType

if TYPE_CHECKING:
    from nexura_agents.contracts.base import BaseModule
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

    def register(self, name: str, handler: Callable[..., Any] | BaseModule) -> None:
        """Register a Python callable or BaseModule by name."""
        if not isinstance(name, str) or not name.strip():
            raise WorkflowValidationError("Handler name must be a non-empty string.")
        if not callable(handler) and not hasattr(handler, "execute"):
            raise WorkflowValidationError(f"Handler '{name}' must be a callable or BaseModule.")
        self._handlers[name.strip()] = handler

    def register_handler(self, name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Decorator to register a handler callable."""

        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.register(name, fn)
            return fn

        return decorator

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
    return {
        "workflow_run_id": cp.workflow_run_id,
        "spec_version": cp.spec_version,
        "spec_hash": cp.spec_hash,
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


def checkpoint_from_dict(data: dict[str, Any]) -> WorkflowCheckpoint:
    pending = data.get("pending_approval")
    return WorkflowCheckpoint(
        workflow_run_id=data["workflow_run_id"],
        spec_version=int(data["spec_version"]),
        spec_hash=data["spec_hash"],
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


class MemoryWorkflowStore:
    """In-memory checkpoint store for transient workflow runs and tests."""

    def __init__(self) -> None:
        self._checkpoints: dict[str, WorkflowCheckpoint] = {}
        self._history: dict[str, list[WorkflowCheckpoint]] = {}
        self._lock = asyncio.Lock()

    async def get(self, workflow_run_id: str) -> WorkflowCheckpoint | None:
        """The latest checkpoint of a run, or ``None`` if it does not exist."""
        async with self._lock:
            cp = self._checkpoints.get(workflow_run_id)
            if cp is None:
                return None
            return copy.deepcopy(cp)

    async def save(self, checkpoint: WorkflowCheckpoint) -> None:
        """Store a checkpoint as the run's latest and append it to its history."""
        async with self._lock:
            stored = copy.deepcopy(checkpoint)
            previous = self._checkpoints.get(checkpoint.workflow_run_id)
            if previous is not None:
                if stored.forked_from is None:
                    stored.forked_from = copy.deepcopy(previous.forked_from)
                if stored.workspace_id is None:
                    stored.workspace_id = previous.workspace_id
            stored.updated_at = time.time()
            self._checkpoints[checkpoint.workflow_run_id] = stored
            self._history.setdefault(checkpoint.workflow_run_id, []).append(copy.deepcopy(stored))

    async def create(self, checkpoint: WorkflowCheckpoint) -> bool:
        """Save the first checkpoint of a new run; False if the run id is taken."""
        async with self._lock:
            if checkpoint.workflow_run_id in self._checkpoints:
                return False
            stored = copy.deepcopy(checkpoint)
            stored.updated_at = time.time()
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
                return True
            return False

    async def close(self) -> None:
        """Release resources held by the store."""
        pass


class SQLiteWorkflowStore:
    """Durable workflow checkpoint store backed by sqlite3."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).resolve()
        self._lock = threading.Lock()
        self._closed = False

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
            for column in ("budget_json", "interrupt_json", "meta_json", "workspace_id"):
                with contextlib.suppress(sqlite3.OperationalError):
                    self._conn.execute(
                        f"ALTER TABLE nexura_workflow_checkpoints ADD COLUMN {column} TEXT;"
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
                   budget_json, interrupt_json, meta_json, workspace_id
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
            insert = """
                    INSERT INTO nexura_workflow_checkpoints (
                        workflow_run_id, spec_version, spec_hash, current_node_id,
                        state_json, step_count, status, approval_id, approval_json,
                        error, updated_at, budget_json, interrupt_json, meta_json,
                        workspace_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """
            try:
                with conn:
                    conn.execute(
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
                        workspace_id = excluded.workspace_id
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
                        ),
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
            return True

    def _claim_approval_sync(self, workflow_run_id: str, approval_id: str) -> bool:
        with self._lock:
            conn = self._ensure_open()
            with conn:
                now = time.time()
                # One UPDATE: a pending approval past its stored expires_at is never claimed.
                cursor = conn.execute(
                    """
                    UPDATE nexura_workflow_checkpoints
                    SET status = 'running', updated_at = ?
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

    async def get(self, workflow_run_id: str) -> WorkflowCheckpoint | None:
        """The latest checkpoint of a run, or ``None`` if it does not exist."""
        return await asyncio.to_thread(self._get_sync, workflow_run_id)

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

    The first failure cancels the rest and is raised as itself, not as a group.
    """
    gate = asyncio.Semaphore(limit)

    async def guarded(factory: Callable[[], Any]) -> dict[str, Any]:
        async with gate:
            result: dict[str, Any] = await factory()
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
        # Set on engines running a sub-workflow per map_over item: those cannot pause.
        self._pause_forbidden: str | None = None
        # State keys written by steps this engine ran (sub-workflow outputs).
        self._written: set[str] = set()

    async def _save(self, checkpoint: WorkflowCheckpoint) -> None:
        if checkpoint.workspace_id is None:
            checkpoint.workspace_id = self.workspace_id
        assert self.store is not None
        await self.store.save(checkpoint)

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

        return await self._execute_loop(
            workflow_run_id=run_id,
            cursor=self.spec.entry_node,
            current_state=state,
            step_count=0,
            node_already_approved=False,
            operation="run",
        )

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
    ) -> WorkflowRunResult:
        """Resume an approval-gated workflow run with exact-action bindings."""
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
        approval_stands = True
        updated_state: dict[str, Any] | None = None
        if state_update:
            state_update = _as_json_update(state_update, cursor or "approval")
            if paused_child is None:
                updated_state = {**copy.deepcopy(state), **copy.deepcopy(state_update)}
                if gate_node is not None:
                    before = canonical_digest(self._gate_binding(gate_node, state))
                    after = canonical_digest(self._gate_binding(gate_node, updated_state))
                    approval_stands = before == after

        # 6. Atomically Claim Approval (the store refuses one past its stored expires_at)
        claimed = await self.store.claim_approval(workflow_run_id, approval_id)
        if not claimed:
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
        provenance_token = _REGATE_PROVENANCE.set(
            {
                "node_id": cursor,
                "supersedes_approval_id": approval_id,
                "regate_reason": "inputs_edited",
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
            )
        finally:
            _RESUME_GRANT.reset(grant_token)
            _REGATE_PROVENANCE.reset(provenance_token)

    async def resume_interrupt(
        self,
        workflow_run_id: str,
        interrupt_id: str,
        value: Any,
        *,
        workspace_id: str | None = None,
    ) -> WorkflowRunResult:
        """Answer an :func:`interrupt` and continue; the paused node runs again with it."""
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
        if not await self.store.claim_approval(workflow_run_id, interrupt_id):  # type: ignore[union-attr]
            raise ApprovalAlreadyClaimedError(
                f"Interrupt '{interrupt_id}' for workflow run '{workflow_run_id}' "
                "has already been answered."
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
        if asking is not None and self._waits_on_subgraph(asking, str(pending.get("node_id"))):
            # The question came from inside the sub-workflow: answer it there.
            child, child_id = await self._paused_child(
                workflow_run_id, asking, checkpoint.step_count + 1, interrupt_id
            )
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

    async def history(self, workflow_run_id: str) -> list[WorkflowCheckpoint]:
        """Every checkpoint of a run, oldest first: inspect state step by step."""
        if self.store is None or not hasattr(self.store, "history"):
            raise WorkflowError(
                "Checkpoint history needs a store with history(), such as "
                "MemoryWorkflowStore or SQLiteWorkflowStore."
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
        return await self._execute_loop(
            workflow_run_id=run_id,
            cursor=source.current_node_id,
            current_state=state,
            step_count=source.step_count,
            node_already_approved=False,
            operation="fork",
        )

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
        async def branch(name: str) -> dict[str, Any]:
            _INTERRUPT_STATE.set(_InterruptState([], blocked=_NO_PAUSE_IN_PARALLEL))
            with _child_span(f"workflow.branch {name}", {"nexura.workflow.branch": name}):
                return await _branch(name)

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

        updates = await _gather_limited(
            [functools.partial(branch, name) for name in node.parallel], node.max_concurrency
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

        async def one(index: int, item: Any) -> dict[str, Any]:
            _INTERRUPT_STATE.set(_InterruptState([], blocked=_NO_PAUSE_IN_PARALLEL))
            with _child_span(f"workflow.map_item {index}", {"nexura.workflow.map_index": index}):
                return await _item(index, item)

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

        updates = await _gather_limited(
            [functools.partial(one, i, item) for i, item in enumerate(items)],
            node.max_concurrency,
        )
        return _merge_branch_updates(node.id, updates, node.reducers, state), None

    def _child_engine(self, spec: WorkflowSpec, timeout: float | None) -> WorkflowEngine:
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
        child = self._child_engine(node.subgraph, remaining)
        if item is not None:
            child._pause_forbidden = _NO_PAUSE_IN_PARALLEL
        inputs = _map_inputs(node.input_mapping, state)

        existing = await self.store.get(child_id) if self.store is not None else None
        if existing is None:
            result = await child.run(inputs, workflow_run_id=child_id)
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
            )

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
            workflow_version="1.0.0",
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
                return self._child_engine(node.subgraph, self.timeout), child_id
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
        return tracing.start(
            f"workflow {self.spec.name}",
            {
                tracing.OPERATION_NAME: "workflow",
                "nexura.workflow.name": self.spec.name,
                "nexura.workflow.run_id": workflow_run_id,
                "nexura.workflow.operation": operation,
            },
        )

    async def _execute_loop(
        self,
        workflow_run_id: str,
        cursor: str | None,
        current_state: dict[str, Any],
        step_count: int,
        node_already_approved: bool,
        resume_answers: list[Any] | None = None,
        operation: str = "run",
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
            is_approval_needed = (
                node.requires_approval
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
                    workflow_version="1.0.0",
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
