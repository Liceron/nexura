# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Durable execution engine with worker leases, fencing tokens, and crash recovery."""

from __future__ import annotations

import asyncio
import copy
import inspect
import time
from collections.abc import Callable
from typing import Any

from nexura_agents.contracts.canonical import canonical_digest
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.errors import (
    BudgetExhaustedError,
    DeadlineExceededError,
    IndeterminateEffectError,
)
from nexura_agents.errors import ExecutionCancelledError, SessionConflictError, StorageError

from nexura_runtime.runtime.execution.errors import (
    DurableExecutionError,
    FencingConflictError,
    LeaseExpiredError,
)
from nexura_runtime.runtime.execution.models import (
    DurableRunState,
    DurableWorkflowSpec,
    NodeIntent,
)
from nexura_runtime.storage import StorageRepository


class DurableExecutionEngine:
    """Authoritative durable execution engine managing worker lifecycle and state."""

    def __init__(
        self,
        repository: StorageRepository,
        handlers: dict[str, Callable[..., Any]] | None = None,
    ) -> None:
        self.repository = repository
        self.handlers: dict[str, Callable[..., Any]] = handlers.copy() if handlers else {}

    def register_handler(self, name: str, handler: Callable[..., Any]) -> None:
        """Register a node handler callable."""
        self.handlers[name] = handler

    async def init_run(
        self,
        workspace_id: str,
        run_id: str,
        spec: DurableWorkflowSpec,
        initial_state: dict[str, Any] | None = None,
        initial_fuel: int | None = None,
        time_budget_seconds: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> DurableRunState:
        """Initialize a new durable workflow run in storage."""
        now = time.time()
        fuel = initial_fuel if initial_fuel is not None else spec.default_fuel_budget
        time_budget = (
            time_budget_seconds
            if time_budget_seconds is not None
            else spec.default_time_budget_seconds
        )

        durable_state = DurableRunState(
            workspace_id=workspace_id,
            run_id=run_id,
            workflow_id=spec.workflow_id,
            status="pending",
            revision=1,
            fencing_token=0,
            current_node_id=spec.entry_node,
            completed_nodes=[],
            state=copy.deepcopy(initial_state or {}),
            fuel_remaining=fuel,
            fuel_consumed=0,
            time_budget_remaining=time_budget,
            time_consumed=0.0,
            pre_dispatch_intent=None,
            created_at=now,
            updated_at=now,
        )

        meta = copy.deepcopy(metadata or {})
        meta["workflow_id"] = spec.workflow_id
        meta["entry_node"] = spec.entry_node
        meta["workflow_digest"] = canonical_digest(spec.model_dump())

        await self.repository.create_run(
            workspace_id=workspace_id,
            run_id=run_id,
            initial_state=durable_state.model_dump(),
            metadata=meta,
        )
        return durable_state

    async def get_state(self, workspace_id: str, run_id: str) -> DurableRunState:
        """Retrieve current durable state from storage."""
        rec = await self.repository.get_run(workspace_id=workspace_id, run_id=run_id)
        if rec is None:
            raise StorageError(f"Run '{run_id}' not found in workspace '{workspace_id}'.")
        state_dict = copy.deepcopy(rec.state)
        state_dict["revision"] = rec.revision
        state_dict["status"] = rec.status
        return DurableRunState.model_validate(state_dict)

    async def claim_run(
        self,
        workspace_id: str,
        run_id: str,
        worker_id: str,
        lease_duration: float = 30.0,
    ) -> DurableRunState:
        """Claim a run for execution by an active worker, acquiring a lease and fencing token."""
        rec = await self.repository.get_run(workspace_id=workspace_id, run_id=run_id)
        if rec is None:
            raise StorageError(f"Run '{run_id}' not found in workspace '{workspace_id}'.")

        cur_state = DurableRunState.model_validate(rec.state)
        now = time.time()

        # If already terminal, return as is
        if rec.status in {"completed", "failed", "cancelled"}:
            cur_state.revision = rec.revision
            cur_state.status = rec.status
            return cur_state

        # Check if existing lease is active and held by a different worker
        if (
            cur_state.worker_id
            and cur_state.worker_id != worker_id
            and now < cur_state.lease_expires_at
        ):
            raise FencingConflictError(
                run_id=run_id,
                expected_token=cur_state.fencing_token,
                actual_token=cur_state.fencing_token,
                worker_id=worker_id,
                details={
                    "held_by": cur_state.worker_id,
                    "expires_at": cur_state.lease_expires_at,
                },
            )

        # Increment fencing token
        new_token = cur_state.fencing_token + 1
        new_status = "running" if rec.status == "pending" else rec.status

        cur_state.fencing_token = new_token
        cur_state.worker_id = worker_id
        cur_state.lease_expires_at = now + lease_duration
        cur_state.status = new_status
        cur_state.updated_at = now

        claim_event = {
            "type": "worker_lease_acquired",
            "worker_id": worker_id,
            "fencing_token": new_token,
            "lease_expires_at": cur_state.lease_expires_at,
        }

        try:
            updated_rec = await self.repository.update_run(
                workspace_id=workspace_id,
                run_id=run_id,
                expected_revision=rec.revision,
                status=new_status,
                state_update=cur_state.model_dump(),
                events=[claim_event],
            )
            cur_state.revision = updated_rec.revision
            return cur_state
        except SessionConflictError as exc:
            raise FencingConflictError(
                run_id=run_id,
                expected_token=new_token,
                actual_token=cur_state.fencing_token,
                worker_id=worker_id,
                details={"reason": "CAS race during claim", "error": str(exc)},
            ) from exc

    async def execute_step(
        self,
        workspace_id: str,
        run_id: str,
        worker_id: str,
        spec: DurableWorkflowSpec,
        context: ExecutionContext | None = None,
    ) -> DurableRunState:
        """Execute a single workflow node with lease verification and crash recovery."""
        rec = await self.repository.get_run(workspace_id=workspace_id, run_id=run_id)
        if rec is None:
            raise StorageError(f"Run '{run_id}' not found in workspace '{workspace_id}'.")

        state = DurableRunState.model_validate(rec.state)
        expected_digest = rec.metadata.get("workflow_digest")
        if expected_digest is not None and expected_digest != canonical_digest(spec.model_dump()):
            raise DurableExecutionError("Workflow specification differs from the stored run")
        state.revision = rec.revision
        state.status = rec.status
        now = time.time()

        # 1. Lease & fencing verification
        if state.worker_id != worker_id:
            raise FencingConflictError(
                run_id=run_id,
                expected_token=state.fencing_token,
                actual_token=state.fencing_token,
                worker_id=worker_id,
                details={"assigned_worker": state.worker_id},
            )
        if now > state.lease_expires_at:
            raise LeaseExpiredError(
                run_id=run_id,
                worker_id=worker_id,
                expires_at=state.lease_expires_at,
                current_time=now,
            )

        # 2. Check for explicit cancellation
        if state.status == "cancelled":
            raise ExecutionCancelledError(f"Run '{run_id}' has been cancelled.")

        # 3. Check if workflow already completed
        if state.status in {"completed", "failed"}:
            return state

        current_node_id = state.current_node_id
        if current_node_id is None:
            state.status = "completed"
            await self._commit_state(
                workspace_id, run_id, state, rec.revision, [{"type": "workflow_completed"}]
            )
            return state

        if current_node_id not in spec.nodes:
            state.status = "failed"
            state.error = f"Node '{current_node_id}' not declared in workflow spec."
            await self._commit_state(
                workspace_id, run_id, state, rec.revision, [{"type": "node_not_found"}]
            )
            raise DurableExecutionError(state.error)

        node_spec = spec.nodes[current_node_id]

        # 4. Crash Recovery Check on Pre-Dispatch Intent
        if state.pre_dispatch_intent is not None:
            intent = state.pre_dispatch_intent
            if (
                intent.node_id == current_node_id
                and intent.dispatched
                and intent.effect_policy == "non_idempotent_write"
            ):
                state.status = "failed"
                state.error = (
                    f"Indeterminate remote outcome on non-idempotent node '{current_node_id}'"
                )
                await self._commit_state(
                    workspace_id,
                    run_id,
                    state,
                    rec.revision,
                    [{"type": "indeterminate_effect_detected", "node_id": current_node_id}],
                )
                raise IndeterminateEffectError(
                    message=state.error,
                    node_id=current_node_id,
                    action="execute_node",
                )
            # Clear pre-dispatch intent before proceeding
            state.pre_dispatch_intent = None

        # Completion and cursor advancement commit atomically. A cursor pointing back
        # to a completed node is a new graph visit; intent recovery above guards retries.

        # 6. Budget & Step Fuel Checks
        if state.fuel_remaining < node_spec.fuel_cost:
            state.status = "failed"
            state.error = (
                f"Fuel exhausted: requires {node_spec.fuel_cost}, remaining {state.fuel_remaining}"
            )
            await self._commit_state(
                workspace_id,
                run_id,
                state,
                rec.revision,
                [{"type": "fuel_exhausted", "node_id": current_node_id}],
            )
            raise BudgetExhaustedError(
                state.error,
                budget_kind="step_fuel",
                consumed=state.fuel_consumed,
                limit=state.fuel_consumed + state.fuel_remaining,
            )

        if state.time_budget_remaining <= 0.0:
            state.status = "failed"
            state.error = f"Active time budget exceeded (limit {spec.default_time_budget_seconds}s)"
            await self._commit_state(
                workspace_id,
                run_id,
                state,
                rec.revision,
                [{"type": "time_budget_exceeded", "node_id": current_node_id}],
            )
            raise DeadlineExceededError(
                state.error,
                timeout_seconds=spec.default_time_budget_seconds,
                elapsed_seconds=state.time_consumed,
            )

        # 7. Record Pre-Dispatch Intent & Advance Revision
        state.pre_dispatch_intent = NodeIntent(
            node_id=current_node_id,
            effect_policy=node_spec.effect_policy,
            dispatched=True,
            timestamp=time.time(),
        )
        pre_event = {
            "type": "node_dispatch_started",
            "node_id": current_node_id,
            "effect_policy": node_spec.effect_policy,
        }
        rec = await self._commit_state(workspace_id, run_id, state, rec.revision, [pre_event])
        state.revision = rec.revision

        # 8. Resolve handler
        handler = self.handlers.get(node_spec.handler)
        if handler is None:
            state.status = "failed"
            state.error = f"Handler '{node_spec.handler}' not registered."
            await self._commit_state(workspace_id, run_id, state, rec.revision, [])
            raise DurableExecutionError(state.error)

        # 9. Execute with Timeout
        effective_timeout = state.time_budget_remaining
        if node_spec.timeout_seconds is not None:
            effective_timeout = min(effective_timeout, node_spec.timeout_seconds)

        start_time = time.time()
        try:
            if inspect.iscoroutinefunction(handler):
                coro = handler(context, state.state) if context else handler(state.state)
                output = await asyncio.wait_for(coro, timeout=effective_timeout)
            else:
                coro = asyncio.to_thread(
                    handler, *([context, state.state] if context else [state.state])
                )
                output = await asyncio.wait_for(coro, timeout=effective_timeout)
        except TimeoutError as exc:
            elapsed = time.time() - start_time
            state.time_consumed += elapsed
            state.time_budget_remaining = max(0.0, state.time_budget_remaining - elapsed)
            state.status = "failed"
            state.error = f"Node '{current_node_id}' timed out after {effective_timeout:.2f}s"
            await self._commit_state(
                workspace_id,
                run_id,
                state,
                rec.revision,
                [{"type": "node_timeout", "node_id": current_node_id, "elapsed": elapsed}],
            )
            raise DeadlineExceededError(
                state.error,
                timeout_seconds=effective_timeout,
                elapsed_seconds=elapsed,
            ) from exc
        except BaseException as exc:
            elapsed = time.time() - start_time
            state.time_consumed += elapsed
            state.time_budget_remaining = max(0.0, state.time_budget_remaining - elapsed)
            state.status = "failed"
            state.error = f"Node '{current_node_id}' failed: {exc}"
            await self._commit_state(
                workspace_id,
                run_id,
                state,
                rec.revision,
                [{"type": "node_error", "node_id": current_node_id, "error": str(exc)}],
            )
            raise

        # 10. Update state, fuel, and transition
        elapsed = time.time() - start_time
        state.fuel_consumed += node_spec.fuel_cost
        state.fuel_remaining -= node_spec.fuel_cost
        state.time_consumed += elapsed
        state.time_budget_remaining = max(0.0, state.time_budget_remaining - elapsed)

        if isinstance(output, dict):
            state.state.update(output)

        state.completed_nodes.append(current_node_id)
        state.pre_dispatch_intent = None

        # Resolve next transition
        if current_node_id in spec.terminal_nodes:
            state.current_node_id = None
            state.status = "completed"
        else:
            next_node = self._resolve_next_node(spec, current_node_id, state.state)
            state.current_node_id = next_node
            if next_node is None:
                state.status = "completed"

        completion_events = [
            {
                "type": "node_completed",
                "node_id": current_node_id,
                "elapsed": elapsed,
                "fuel_cost": node_spec.fuel_cost,
            }
        ]
        if state.status == "completed":
            completion_events.append({"type": "workflow_completed"})

        rec = await self._commit_state(workspace_id, run_id, state, rec.revision, completion_events)
        state.revision = rec.revision
        return state

    async def run_until_idle(
        self,
        workspace_id: str,
        run_id: str,
        worker_id: str,
        spec: DurableWorkflowSpec,
        context: ExecutionContext | None = None,
        max_steps: int = 100,
    ) -> DurableRunState:
        """Run workflow steps sequentially until a terminal status or idle state is reached."""
        cur_state = await self.claim_run(workspace_id, run_id, worker_id)
        steps = 0
        while cur_state.status == "running" and cur_state.current_node_id is not None:
            if steps >= max_steps:
                break
            cur_state = await self.execute_step(workspace_id, run_id, worker_id, spec, context)
            steps += 1
        return cur_state

    async def request_cancellation(self, workspace_id: str, run_id: str) -> DurableRunState:
        """Durable cancellation request independent of API transport disconnection."""
        rec = await self.repository.get_run(workspace_id=workspace_id, run_id=run_id)
        if rec is None:
            raise StorageError(f"Run '{run_id}' not found in workspace '{workspace_id}'.")

        state = DurableRunState.model_validate(rec.state)
        state.status = "cancelled"
        state.updated_at = time.time()

        updated_rec = await self.repository.update_run(
            workspace_id=workspace_id,
            run_id=run_id,
            expected_revision=rec.revision,
            status="cancelled",
            state_update=state.model_dump(),
            events=[{"type": "run_cancelled", "timestamp": time.time()}],
        )
        state.revision = updated_rec.revision
        return state

    def _resolve_next_node(
        self,
        spec: DurableWorkflowSpec,
        current_node_id: str,
        current_state: dict[str, Any],
    ) -> str | None:
        """Resolve next node transition based on branch or direct target."""
        transition = spec.transitions.get(current_node_id)
        if transition is None:
            return None
        if isinstance(transition, str):
            return transition
        if isinstance(transition, dict):
            branch_key = current_state.get(f"{current_node_id}_branch", "default")
            target = transition.get(str(branch_key))
            if target is None:
                target = transition.get("default")
            return target
        return None

    async def _commit_state(
        self,
        workspace_id: str,
        run_id: str,
        state: DurableRunState,
        expected_revision: int,
        events: list[dict[str, Any]],
    ) -> Any:
        now = time.time()
        if state.worker_id is not None and now >= state.lease_expires_at:
            raise LeaseExpiredError(
                run_id=run_id,
                worker_id=state.worker_id,
                expires_at=state.lease_expires_at,
                current_time=now,
            )
        state.updated_at = now
        return await self.repository.update_run(
            workspace_id=workspace_id,
            run_id=run_id,
            expected_revision=expected_revision,
            status=state.status,
            state_update=state.model_dump(),
            events=events,
        )
