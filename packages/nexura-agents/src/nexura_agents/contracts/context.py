# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Execution context and injected capability protocols for module runs."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from nexura_agents.contracts.events import ModuleEvent, ModuleEventType
from nexura_agents.contracts.result import ArtifactReference
from nexura_agents.errors import ExecutionCancelledError
from nexura_agents.types import Usage


class CredentialReference(BaseModel):
    """Reference to a scoped credential identifier."""

    credential_id: str
    credential_type: str
    workspace_id: str | None = None
    environment_variable: str | None = None


@runtime_checkable
class CredentialResolver(Protocol):
    """Protocol for resolving credential references securely."""

    def resolve(self, ref: CredentialReference | str) -> str | None:
        """Resolve a secret token or key by reference."""
        ...


@runtime_checkable
class ArtifactStore(Protocol):
    """Protocol for persisting raw payloads outside checkpoints."""

    async def put(
        self,
        artifact_id: str,
        data: bytes,
        *,
        mime_type: str = "application/octet-stream",
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactReference:
        """Store bytes and return an ArtifactReference."""
        ...

    async def get(self, artifact_id: str) -> bytes | None:
        """Retrieve stored bytes for an artifact."""
        ...


@runtime_checkable
class BudgetInterface(Protocol):
    """Protocol for tracking and enforcing resource budgets."""

    def record_usage(self, usage: Usage) -> None:
        """Record token or invocation usage."""
        ...

    def is_exhausted(self) -> bool:
        """True if any budget limit is exceeded."""
        ...

    def remaining_tokens(self) -> int | None:
        """Return remaining tokens if configured."""
        ...


@runtime_checkable
class EventSink(Protocol):
    """Protocol for consuming module lifecycle events."""

    def emit(self, event: ModuleEvent) -> None:
        """Emit a module event."""
        ...


class ExecutionContext:
    """Carries invocation context, identity, deadline, and capabilities.

    Explicitly separates workspace, run, node, and attempt IDs so two nodes
    using the same module maintain distinct execution identities.
    """

    def __init__(
        self,
        *,
        workspace_id: str = "default",
        principal_id: str = "anonymous",
        run_id: str,
        node_id: str,
        attempt_id: str = "att_1",
        deadline: float | None = None,
        is_cancelled: bool = False,
        cancellation_checker: Callable[[], bool] | None = None,
        budget: BudgetInterface | None = None,
        event_sink: EventSink | None = None,
        artifact_store: ArtifactStore | None = None,
        credential_resolver: CredentialResolver | None = None,
        attributes: dict[str, Any] | None = None,
        approval_granted: bool = False,
        step_key: str | None = None,
        fencing_token: int | None = None,
    ) -> None:
        # Set only by the caller that owns the approval decision (the workflow engine after
        # a human approved this node, the agent runner after a tool approval, or an operator
        # script). Never derive it from module inputs: those come from specs and models.
        self._approval_granted = bool(approval_granted)
        self.workspace_id = workspace_id
        self.principal_id = principal_id
        self.run_id = run_id
        self.node_id = node_id
        self.attempt_id = attempt_id
        self.deadline = deadline
        self._is_cancelled = is_cancelled
        self._cancellation_checker = cancellation_checker
        self.budget = budget
        self.event_sink = event_sink
        self.artifact_store = artifact_store
        self.credential_resolver = credential_resolver
        self.attributes = attributes or {}
        # Execution Contract v1: the journal step this call belongs to (for example
        # ``charge#3`` or ``charge#3.2[0]``) and the run lease's fencing token.
        self.step_key = step_key
        self._fencing_token = fencing_token

    def idempotency_key(self, index: int = 0) -> str | None:
        """Stable idempotency key for effect ``index`` of this step, or ``None`` if unknown.

        ``nx-`` plus 32 hex characters of the effect id derived from the workspace, run,
        step key and ``index`` (see ``default_idempotency_key``). It is the same when the
        step runs again after a crash or a takeover, and different for a new graph visit or
        a re-dispatch a person authorised. Forward it to remote systems that de-duplicate
        (an ``Idempotency-Key`` header, a client reference) so a repeat cannot apply twice.
        """
        if self.step_key is not None and self.run_id:
            from nexura_agents.contracts.execution import ExecutionId, default_idempotency_key

            return default_idempotency_key(
                ExecutionId(self.workspace_id or "default", self.run_id), self.step_key, index
            )
        key = self.attributes.get("idempotency_key")
        return key if index == 0 and isinstance(key, str) else None

    @property
    def fencing_token(self) -> int | None:
        """The run lease's fencing token (only goes up); ``None`` when no lease is held.

        Connectors that support it should forward it (a header or a compare-and-swap field)
        so a remote system can refuse a stale owner's late write.
        """
        if self._fencing_token is not None:
            return self._fencing_token
        token = self.attributes.get("fencing_token")
        return token if isinstance(token, int) and not isinstance(token, bool) else None

    def require_write_approval(self, what: str) -> None:
        """Raise unless a trusted caller granted approval for the write ``what``."""
        if not self._approval_granted:
            from nexura_agents.contracts.errors import ContractError

            raise ContractError(
                f"{what} writes to an external system and needs human approval granted by "
                "the workflow engine or an operator; an 'approved' input grants nothing",
                code="APPROVAL_REQUIRED",
            )

    @property
    def approval_granted(self) -> bool:
        """True when a trusted caller approved this execution (read-only for modules)."""
        return self._approval_granted

    @property
    def is_cancelled(self) -> bool:
        """Check if execution has been marked cancelled."""
        return bool(
            self._is_cancelled or (self._cancellation_checker and self._cancellation_checker())
        )

    def cancel(self) -> None:
        """Mark context as cancelled."""
        self._is_cancelled = True

    def check_cancelled(self) -> None:
        """Raise ExecutionCancelledError if cancelled."""
        if self.is_cancelled:
            raise ExecutionCancelledError("Module execution was explicitly cancelled")

    def remaining_seconds(self) -> float | None:
        """Return remaining time in seconds until deadline, or None if no deadline."""
        if self.deadline is None:
            return None
        remaining = self.deadline - time.time()
        return max(0.0, remaining)

    def is_expired(self) -> bool:
        """True if the deadline has passed."""
        rem = self.remaining_seconds()
        return rem is not None and rem <= 0.0

    def emit_event(
        self,
        event_type: ModuleEventType,
        payload: dict[str, Any],
        *,
        module_id: str,
        version: str,
    ) -> ModuleEvent:
        """Helper to construct and emit a ModuleEvent."""
        event = ModuleEvent(
            type=event_type,
            module_id=module_id,
            version=version,
            run_id=self.run_id,
            node_id=self.node_id,
            attempt_id=self.attempt_id,
            payload=payload,
        )
        if self.event_sink is not None:
            self.event_sink.emit(event)
        return event
