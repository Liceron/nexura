# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Durable execution engine with worker leases, fencing tokens, and crash recovery."""

from __future__ import annotations

import asyncio
import copy
import inspect
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from nexura_agents.contracts.canonical import canonical_digest
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.errors import (
    BudgetExhaustedError,
    DeadlineExceededError,
    IndeterminateEffectError,
)
from nexura_agents.contracts.execution import (
    EffectClass,
    EffectRecord,
    EffectResolution,
    EffectStatus,
    ExecutionId,
    LeaseLostError,
    effect_class_from_policy,
)
from nexura_agents.errors import ExecutionCancelledError, SessionConflictError, StorageError
from nexura_agents.execution_store import EffectAlreadyResolvedError, ExecutionStore

from nexura_runtime.runtime.execution.errors import (
    DurableDefinitionMismatchError,
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
from nexura_runtime.storage.execution_bridge import (
    ExecutionRepositoryBridge,
    execution_store_for,
    visit_key,
)


class DurableExecutionEngine:
    """Authoritative durable execution engine managing worker lifecycle and state."""

    def __init__(
        self,
        repository: StorageRepository | ExecutionStore,
        handlers: dict[str, Callable[..., Any]] | None = None,
    ) -> None:
        self.repository = repository
        self.handlers: dict[str, Callable[..., Any]] = handlers.copy() if handlers else {}
        self._execution_store: ExecutionStore | None = None

    async def get_execution_store(self) -> ExecutionStore:
        """Return the authoritative execution journal behind this deprecated adapter."""
        if self._execution_store is not None:
            return self._execution_store
        if isinstance(self.repository, ExecutionStore):
            await self.repository.init_db()
            self._execution_store = self.repository
        else:
            self._execution_store = await execution_store_for(self.repository)
        return self._execution_store

    async def _records(self) -> ExecutionRepositoryBridge:
        return ExecutionRepositoryBridge(await self.get_execution_store())

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

        record = await (await self._records()).create_run(
            workspace_id=workspace_id,
            run_id=run_id,
            initial_state=durable_state.model_dump(),
            metadata=meta,
        )
        durable_state.revision = record.revision
        return durable_state

    async def get_state(self, workspace_id: str, run_id: str) -> DurableRunState:
        """Retrieve current durable state from storage."""
        rec = await (await self._records()).get_run(workspace_id=workspace_id, run_id=run_id)
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
        store = await self.get_execution_store()
        identity = ExecutionId(workspace_id, run_id)
        cur_state = await self.get_state(workspace_id, run_id)
        if cur_state.status in {"completed", "failed", "cancelled"}:
            return cur_state
        snapshot = await store.get(identity)
        lease = await store.claim(
            identity, worker_id, lease_duration, definition_digest=snapshot.run.definition_digest
        )
        if lease is None:
            raise FencingConflictError(
                run_id, cur_state.fencing_token, cur_state.fencing_token, worker_id
            )
        cur_state = await self.get_state(workspace_id, run_id)
        cur_state.worker_id = worker_id
        cur_state.fencing_token = lease.fencing_token - 1
        cur_state.lease_expires_at = lease.expires_at
        if cur_state.status == "pending":
            cur_state.status = "running"
        # Keep unresolved runs paused; only the current visit's resolution can continue it.
        if cur_state.status == "paused" and any(
            effect.status == EffectStatus.RESOLVED
            and effect.intent.step_key == visit_key(cur_state.model_dump())
            for effect in snapshot.effects.values()
        ):
            cur_state.status = "running"
        try:
            rec = await (await self._records()).update_run(
                workspace_id,
                run_id,
                cur_state.revision,
                cur_state.status,
                cur_state.model_dump(),
                [
                    {
                        "type": "worker_lease_acquired",
                        "worker_id": worker_id,
                        "fencing_token": cur_state.fencing_token,
                    }
                ],
            )
        except (SessionConflictError, LeaseLostError) as exc:
            raise FencingConflictError(
                run_id, cur_state.fencing_token, cur_state.fencing_token, worker_id
            ) from exc
        return DurableRunState.model_validate(rec.state)

    async def execute_step(
        self,
        workspace_id: str,
        run_id: str,
        worker_id: str,
        spec: DurableWorkflowSpec,
        context: ExecutionContext | None = None,
    ) -> DurableRunState:
        """Execute a single workflow node with lease verification and crash recovery."""
        rec = await (await self._records()).get_run(workspace_id=workspace_id, run_id=run_id)
        if rec is None:
            raise StorageError(f"Run '{run_id}' not found in workspace '{workspace_id}'.")

        state = DurableRunState.model_validate(rec.state)
        expected_digest = rec.metadata.get("workflow_digest")
        if expected_digest is not None and expected_digest != canonical_digest(spec.model_dump()):
            raise DurableDefinitionMismatchError(
                "Workflow specification differs from the stored run"
            )
        state.revision = rec.revision
        state.status = rec.status

        # Terminal state is returned before lease checks because terminal commits release it.
        if state.status == "cancelled":
            raise ExecutionCancelledError(f"Run '{run_id}' has been cancelled.")
        if state.status in {"completed", "failed", "paused"}:
            return state
        # 1. Lease & fencing verification
        if state.worker_id != worker_id:
            raise FencingConflictError(
                run_id=run_id,
                expected_token=state.fencing_token,
                actual_token=state.fencing_token,
                worker_id=worker_id,
                details={"assigned_worker": state.worker_id},
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
        effect_class = (
            EffectClass(node_spec.effect_policy)
            if node_spec.effect_policy in {item.value for item in EffectClass}
            else effect_class_from_policy(node_spec.effect_policy)
        )
        recovered_output: Any = None
        recovered = False

        # 4. Crash Recovery Check on Pre-Dispatch Intent
        if state.pre_dispatch_intent is not None:
            intent = state.pre_dispatch_intent
            snapshot = await (await self.get_execution_store()).get(
                ExecutionId(workspace_id, run_id)
            )
            effects = [
                effect
                for effect in snapshot.effects.values()
                if effect.intent.step_key == visit_key(state.model_dump())
            ]
            effect = effects[0] if effects else None
            if effect is not None and effect.status == EffectStatus.RESOLVED:
                if effect.resolution == EffectResolution.CONFIRMED:
                    recovered_output, recovered = effect.result, True
                elif effect.resolution == EffectResolution.FAILED:
                    state.status, state.error = "failed", "Effect resolution failed"
                    await self._commit_state(workspace_id, run_id, state, rec.revision, [])
                    return state
                else:
                    # not_applied: dispatch again as a new attempt with a new key.
                    if effect.intent.approval_id is not None:
                        raise DurableExecutionError("Re-dispatch needs a fresh approval")
                    state.dispatch_attempt += 1
                    state.pre_dispatch_intent = None
                state.pause_reason, state.pending_effect = None, None
            if (
                intent.node_id == current_node_id
                and intent.dispatched
                and effect_class in (EffectClass.NON_IDEMPOTENT, EffectClass.RECONCILABLE)
                and not recovered
                and state.pre_dispatch_intent is not None
            ):
                state.status = "paused"
                state.pause_reason = "effect_unknown"
                state.pending_effect = asdict(effect) if effect is not None else intent.model_dump()
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

        # 7. Resolve the handler before recording a dispatch, so a missing handler
        # never leaves a dispatched intent (and a pending effect) behind.
        handler = self.handlers.get(node_spec.handler)
        if handler is None and not recovered:
            state.status = "failed"
            state.error = f"Handler '{node_spec.handler}' not registered."
            await self._commit_state(workspace_id, run_id, state, rec.revision, [])
            raise DurableExecutionError(state.error)

        # 8. Record Pre-Dispatch Intent & Advance Revision. For a non-pure node the
        # same fenced commit writes the journal StepIntent and a pending effect.
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
        if not recovered:
            rec = await self._commit_state(workspace_id, run_id, state, rec.revision, [pre_event])
        state.revision = rec.revision

        # 9. Execute with Timeout
        effective_timeout = state.time_budget_remaining
        if node_spec.timeout_seconds is not None:
            effective_timeout = min(effective_timeout, node_spec.timeout_seconds)

        if context is not None and not recovered:
            # Handlers that call remote systems should forward these: the key is stable
            # across takeover of this visit, the token only increases.
            snapshot = await (await self.get_execution_store()).get(
                ExecutionId(workspace_id, run_id)
            )
            key = visit_key(state.model_dump())
            context.attributes["fencing_token"] = snapshot.run.fencing_token
            context.attributes["idempotency_key"] = next(
                (
                    item.intent.idempotency_key
                    for item in snapshot.effects.values()
                    if item.intent.step_key == key
                ),
                None,
            )

        start_time = time.time()
        try:
            if recovered:
                output = recovered_output
            elif inspect.iscoroutinefunction(handler):
                async_call = handler(context, state.state) if context else handler(state.state)
                output = await asyncio.wait_for(async_call, timeout=effective_timeout)
            else:
                assert handler is not None
                thread_call = asyncio.to_thread(
                    handler, *([context, state.state] if context else [state.state])
                )
                output = await asyncio.wait_for(thread_call, timeout=effective_timeout)
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
        state.pause_reason, state.pending_effect = None, None
        state.dispatch_attempt = 1

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

        rec = await self._commit_state(
            workspace_id,
            run_id,
            state,
            rec.revision,
            completion_events,
            outcome=output,
            has_outcome=True,
        )
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
        rec = await (await self._records()).get_run(workspace_id=workspace_id, run_id=run_id)
        if rec is None:
            raise StorageError(f"Run '{run_id}' not found in workspace '{workspace_id}'.")

        state = DurableRunState.model_validate(rec.state)
        state.status = "cancelled"
        state.updated_at = time.time()

        updated_rec = await (await self._records()).update_run(
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
        *,
        outcome: Any = None,
        has_outcome: bool = False,
    ) -> Any:
        now = time.time()
        state.updated_at = now
        try:
            return await (await self._records()).update_run(
                workspace_id=workspace_id,
                run_id=run_id,
                expected_revision=expected_revision,
                status=state.status,
                state_update=state.model_dump(),
                events=events,
                outcome=outcome,
                has_outcome=has_outcome,
            )
        except LeaseLostError as exc:
            # The journal judged the lease by the store's clock; keep the legacy error type.
            raise LeaseExpiredError(
                run_id=run_id,
                worker_id=state.worker_id or "",
                expires_at=state.lease_expires_at,
                current_time=time.time(),
            ) from exc

    async def resolve_effect(
        self,
        workspace_id: str,
        run_id: str,
        effect_id: str,
        resolution: EffectResolution,
        *,
        actor: str,
        result: Any = None,
    ) -> EffectRecord:
        """Record a person's decision about an ``indeterminate`` effect, exactly once.

        ``resolution`` is ``confirmed`` (it happened; ``result`` becomes the node output),
        ``not_applied`` (it did not happen; the node runs again with a new idempotency key)
        or ``failed`` (the run fails). The decision and ``actor`` are journaled together.
        It does not dispatch anything: the next ``claim_run``/``run_until_idle`` continues
        the paused run. A second resolution raises ``EffectAlreadyResolvedError``; an
        unknown run raises ``StorageError``, an unknown effect ``DurableExecutionError``.
        """
        store = await self.get_execution_store()
        identity = ExecutionId(workspace_id, run_id)
        try:
            snapshot = await store.get(identity)
        except KeyError:
            raise StorageError(f"Run '{run_id}' not found in workspace '{workspace_id}'.") from None
        effect = snapshot.effects.get(effect_id)
        if effect is None:
            raise DurableExecutionError(f"Effect '{effect_id}' not found in run '{run_id}'.")
        if effect.status == EffectStatus.RESOLVED:
            raise EffectAlreadyResolvedError("Effect is already resolved")
        lease = await store.claim(
            identity,
            "resolve-" + uuid.uuid4().hex,
            30,
            definition_digest=snapshot.run.definition_digest,
        )
        if lease is None:
            raise DurableExecutionError("Run has a live lease; resolve it after the owner stops")
        try:
            return await store.resolve_effect(
                lease, effect_id, resolution, actor=actor, result=result
            )
        finally:
            current = await store.get(identity)
            if current.run.lease_owner == lease.owner:
                await store.release(lease)
