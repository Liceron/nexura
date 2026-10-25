"""Acceptance test suite for O02: Durable recovery, exact-action approvals and effects.

Verifies:
1. Approval exact-action bindings: canonical argument digests, workspace, actor role,
   expiration, and duplicate resume prevention.
2-4. (Moved, A6 S12) real OS process kills, pre-dispatch recovery and stale-worker fencing
   now run on WorkflowEngine in tests/durability and tests/backend/b05 (see the note below).
5. Carrying O01 budget reservations across checkpoint pause and resumption without double charging.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from nexura_runtime.contracts.canonical import canonical_digest
from nexura_runtime.contracts.context import ExecutionContext
from nexura_runtime.errors import (
    ApprovalAlreadyClaimedError,
    WorkflowTerminalError,
)
from nexura_runtime.providers.base import BaseProvider
from nexura_runtime.providers.types import (
    EmbeddingResult,
    GenerationRequest,
    ProviderCapabilities,
    StreamDelta,
)
from nexura_runtime.types import Message, ModelResponse, Usage
from nexura_runtime.usage.ledger import UsageLedger
from nexura_runtime.usage.manager import BudgetManager
from nexura_runtime.usage.models import BudgetConfig, ModelPricing
from nexura_runtime.usage.pricing import PricingTable

from nexura_runtime.runtime.approvals.errors import (
    ApprovalExpiredError,
    MutatedArgumentsError,
    WrongActorError,
    WrongWorkspaceError,
)
from nexura_runtime.workflow import (
    ModuleRegistry,
    SQLiteWorkflowStore,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
)

# ============================================================================
# Test Classes
# ============================================================================


class TestApprovalExactActionBindings:
    """Deliverable 2: Bind approval to canonical arguments, workspace, role, and expiry."""

    @pytest.mark.asyncio
    async def test_mutated_arguments_rejected_before_gated_effect(self, tmp_path: Path) -> None:
        """Mutated arguments between request and approval raise MutatedArgumentsError.

        Zero side effects should be dispatched.
        """
        db_path = tmp_path / "wf_approval.db"
        store = SQLiteWorkflowStore(db_path)
        registry = ModuleRegistry()

        effect_calls = []

        def secure_action(state: dict[str, Any]) -> dict[str, Any]:
            effect_calls.append(state)
            return {"executed": True}

        registry.register("secure_action", secure_action)

        spec = WorkflowSpec(
            "wf_wire",
            entry_node="gate",
            terminal_nodes=["gate"],
            nodes={
                "gate": WorkflowNode(
                    id="gate",
                    handler="secure_action",
                    requires_approval=True,
                    required_role="admin",
                )
            },
        )
        engine = WorkflowEngine(spec, registry, store=store, workspace_id="ws_finance")

        # 1. Initial run pauses at gate
        init_state = {"action": "wire_transfer", "amount": 100_000, "recipient": "ACME_CORP"}
        result = await engine.run(init_state, workflow_run_id="run_wire_01")
        assert result.status == "paused"
        assert result.pending_approval is not None
        appr_id = result.pending_approval.approval_id
        original_digest = result.pending_approval.payload_digest
        assert original_digest == canonical_digest(init_state)
        assert len(effect_calls) == 0

        # 2. Attempt resume with mutated argument digest
        tampered_digest = canonical_digest({"action": "wire_transfer", "amount": 999_999})
        with pytest.raises(MutatedArgumentsError) as exc_info:
            await engine.resume(
                "run_wire_01",
                appr_id,
                expected_payload_digest=tampered_digest,
            )
        assert exc_info.value.expected_digest == tampered_digest
        assert exc_info.value.actual_digest == original_digest
        assert len(effect_calls) == 0, "No gated effects must be dispatched on mutated arguments!"

    @pytest.mark.asyncio
    async def test_cross_workspace_approval_rejected(self, tmp_path: Path) -> None:
        """Approval resolution from a foreign workspace raises WrongWorkspaceError."""
        db_path = tmp_path / "wf_approval.db"
        store = SQLiteWorkflowStore(db_path)
        registry = ModuleRegistry()
        registry.register("action", lambda state: {"ok": True})

        spec = WorkflowSpec(
            "wf_alpha",
            entry_node="node",
            terminal_nodes=["node"],
            nodes={"node": WorkflowNode(id="node", handler="action", requires_approval=True)},
        )
        engine = WorkflowEngine(spec, registry, store=store, workspace_id="ws_corp_alpha")
        res = await engine.run({"test": 1}, workflow_run_id="run_alpha_01")
        assert res.pending_approval is not None
        appr_id = res.pending_approval.approval_id

        # Attempt to resume specifying workspace 'ws_corp_beta'
        with pytest.raises(WrongWorkspaceError) as exc_info:
            await engine.resume("run_alpha_01", appr_id, workspace_id="ws_corp_beta")

        assert exc_info.value.request_workspace == "ws_corp_alpha"
        assert exc_info.value.decision_workspace == "ws_corp_beta"

    @pytest.mark.asyncio
    async def test_expired_approval_rejected_and_marked_failed(self, tmp_path: Path) -> None:
        """Expired approvals cannot be approved and result in ApprovalExpiredError."""
        db_path = tmp_path / "wf_approval.db"
        store = SQLiteWorkflowStore(db_path)
        registry = ModuleRegistry()
        registry.register("action", lambda state: {"done": True})

        spec = WorkflowSpec(
            "wf_expire",
            entry_node="node",
            terminal_nodes=["node"],
            nodes={"node": WorkflowNode(id="node", handler="action", requires_approval=True)},
        )
        # Configure very short approval TTL (100ms)
        engine = WorkflowEngine(spec, registry, store=store, approval_ttl=0.1)
        res = await engine.run({}, workflow_run_id="run_expire_01")
        assert res.pending_approval is not None
        appr_id = res.pending_approval.approval_id

        # Wait for expiration
        await asyncio.sleep(0.15)

        with pytest.raises(ApprovalExpiredError):
            await engine.resume("run_expire_01", appr_id)

        # Checkpoint is marked failed in store
        cp = await store.get("run_expire_01")
        assert cp is not None
        assert cp.status == "failed"

    @pytest.mark.asyncio
    async def test_foreign_actor_role_rejected(self, tmp_path: Path) -> None:
        """Decisions by actors lacking required role raise WrongActorError."""
        db_path = tmp_path / "wf_approval.db"
        store = SQLiteWorkflowStore(db_path)
        registry = ModuleRegistry()
        registry.register("action", lambda state: {"ok": True})

        spec = WorkflowSpec(
            "wf_role",
            entry_node="node",
            terminal_nodes=["node"],
            nodes={
                "node": WorkflowNode(
                    id="node",
                    handler="action",
                    requires_approval=True,
                    required_role="admin",
                )
            },
        )
        engine = WorkflowEngine(spec, registry, store=store)
        res = await engine.run({}, workflow_run_id="run_role_01")
        assert res.pending_approval is not None
        appr_id = res.pending_approval.approval_id

        with pytest.raises(WrongActorError) as exc_info:
            await engine.resume(
                "run_role_01",
                appr_id,
                actor="user_intern",
                actor_role="viewer",
            )
        assert exc_info.value.required_role == "admin"
        assert exc_info.value.deciding_role == "viewer"

    @pytest.mark.asyncio
    async def test_rejected_decision_is_terminal_with_zero_effects(self, tmp_path: Path) -> None:
        """Rejecting an approval records status='rejected' and 0 effects.

        Forbids further resume attempts.
        """
        db_path = tmp_path / "wf_approval.db"
        store = SQLiteWorkflowStore(db_path)
        registry = ModuleRegistry()
        dispatched = []

        def _action(state: dict[str, Any]) -> dict[str, Any]:
            dispatched.append(1)
            return {"done": True}

        registry.register("action", _action)

        spec = WorkflowSpec(
            "wf_rej",
            entry_node="node",
            terminal_nodes=["node"],
            nodes={"node": WorkflowNode(id="node", handler="action", requires_approval=True)},
        )
        engine = WorkflowEngine(spec, registry, store=store)
        res = await engine.run({}, workflow_run_id="run_rej_01")
        assert res.pending_approval is not None
        appr_id = res.pending_approval.approval_id

        # Reject decision
        reject_res = await engine.resume("run_rej_01", appr_id, decision="reject")
        assert reject_res.status == "rejected"
        assert len(dispatched) == 0

        # Attempt to resume again raises WorkflowTerminalError
        with pytest.raises(WorkflowTerminalError):
            await engine.resume("run_rej_01", appr_id, decision="approve")
        assert len(dispatched) == 0

    @pytest.mark.asyncio
    async def test_duplicate_resume_claim_rejected(self, tmp_path: Path) -> None:
        """Repeated resume on already-claimed approval raises ApprovalAlreadyClaimedError."""
        db_path = tmp_path / "wf_approval.db"
        store = SQLiteWorkflowStore(db_path)
        registry = ModuleRegistry()
        calls = 0

        def action(state: dict[str, Any]) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            return {"calls": calls}

        registry.register("action", action)
        spec = WorkflowSpec(
            "wf_dup",
            entry_node="node",
            terminal_nodes=["node"],
            nodes={"node": WorkflowNode(id="node", handler="action", requires_approval=True)},
        )
        engine = WorkflowEngine(spec, registry, store=store)
        res = await engine.run({}, workflow_run_id="run_dup_01")
        assert res.pending_approval is not None
        appr_id = res.pending_approval.approval_id

        # First resume claims and succeeds
        res1 = await engine.resume("run_dup_01", appr_id, decision="approve")
        assert res1.status == "completed"
        assert calls == 1

        # Second resume on the same approval raises terminal or claimed error
        with pytest.raises((ApprovalAlreadyClaimedError, WorkflowTerminalError)):
            await engine.resume("run_dup_01", appr_id, decision="approve")
        assert calls == 1


# The OS-process kill and stale-worker cases of O02 ran on the legacy DurableExecutionEngine,
# removed by P35 A6 S12. Their WorkflowEngine forms (real SIGKILL, an external effect service
# with its own ledger, SQLite and Postgres) are in tests/durability/test_durability_workflows.py:
# - kill after the remote accepted a non-idempotent effect -> one effect, run paused
#   effect_unknown: test_mid_node_kill_recover_pauses_effect_unknown (and _without_a_budget);
# - kill before dispatch -> the step runs exactly once:
#   test_kill_after_node_commit_recover_continues_from_the_cursor (K5 for "a" = K1 for "b"),
#   plus tests/backend/b05/test_durable_execution.py::test_crash_before_dispatch_runs_the_step_once;
# - a stale worker cannot commit after a newer lease:
#   tests/workflows/test_s6_workflow_journal.py::test_a_stale_owner_cannot_commit_after_takeover.


class TestBudgetReservationRecovery:
    """Deliverable 6: Carry O01 reservations across recovery.

    Ensures spending is not reset or double charged.
    """

    @pytest.mark.asyncio
    async def test_budget_reservations_carried_across_workflow_pause_and_resume(
        self, tmp_path: Path
    ) -> None:
        """Budget spend and reservations carry across workflow pauses and SQLite reopens."""
        db_path = tmp_path / "wf_budget.db"
        usage_db = tmp_path / "usage_ledger.db"
        store = SQLiteWorkflowStore(db_path)

        pricing = PricingTable(version="2026-10-07-standard", date="2026-10-07")
        pricing.add_model_pricing(
            ModelPricing(
                model_id="mock-gpt-4o",
                input_cost_per_million=Decimal("2.50"),
                output_cost_per_million=Decimal("10.00"),
            )
        )
        ledger = UsageLedger(db_path=str(usage_db))
        manager = BudgetManager(ledger=ledger, pricing_table=pricing)
        budget = manager.create_run_budget(
            workspace_id="ws_finance",
            run_id="run_budgeted_wf",
            config=BudgetConfig(max_cost_usd=Decimal("0.10")),
            default_model="mock-gpt-4o",
        )

        class MockBillingProvider(BaseProvider):
            def __init__(self) -> None:
                super().__init__(
                    "mock-gpt-4o",
                    ProviderCapabilities(can_stream=False, can_tools=False, can_embed=False),
                )
                self.call_count = 0

            async def _generate_internal(
                self, request: GenerationRequest, context: ExecutionContext | None
            ) -> ModelResponse:
                self.call_count += 1
                return ModelResponse(
                    message=Message(role="assistant", content="Result"),
                    usage=Usage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
                )

            async def _stream_internal(
                self, request: GenerationRequest, context: ExecutionContext | None
            ) -> AsyncIterator[StreamDelta]:
                if False:
                    yield  # pragma: no cover

            async def _embed_internal(
                self, texts: list[str], context: ExecutionContext | None
            ) -> EmbeddingResult:
                return EmbeddingResult(embeddings=[], model=self.model_id, usage=Usage())

        provider = MockBillingProvider()
        registry = ModuleRegistry()

        active_budget = budget

        async def step_1(state: dict[str, Any]) -> dict[str, Any]:
            ctx = ExecutionContext(
                workspace_id="ws_finance",
                run_id="run_budgeted_wf",
                node_id="step_1",
                budget=active_budget,
            )
            resp = await provider.generate([Message(role="user", content="Prompt 1")], context=ctx)
            return {"step1_content": resp.message.content}

        async def step_2(state: dict[str, Any]) -> dict[str, Any]:
            ctx = ExecutionContext(
                workspace_id="ws_finance",
                run_id="run_budgeted_wf",
                node_id="step_2",
                budget=active_budget,
            )
            resp = await provider.generate([Message(role="user", content="Prompt 2")], context=ctx)
            return {"step2_content": resp.message.content}

        registry.register("step_1", step_1)
        registry.register("step_2", step_2)

        spec = WorkflowSpec(
            "wf_budget",
            entry_node="step_1",
            terminal_nodes=["step_2"],
            nodes={
                "step_1": WorkflowNode(
                    id="step_1",
                    handler="step_1",
                    default_transition="step_2",
                ),
                "step_2": WorkflowNode(
                    id="step_2",
                    handler="step_2",
                    requires_approval=True,  # Pauses here
                ),
            },
        )

        engine1 = WorkflowEngine(
            spec,
            registry,
            store=store,
            workspace_id="ws_finance",
            budget=budget,
        )

        # 1. Execute run: runs step_1, then pauses before step_2
        res1 = await engine1.run({}, workflow_run_id="run_budgeted_wf")
        assert res1.status == "paused"
        assert res1.pending_approval is not None
        assert provider.call_count == 1

        # Verify step 1 spend
        status1 = budget.get_status()
        assert status1.provider_calls_count == 1
        assert status1.total_spent_usd > Decimal("0")
        spend_after_step1 = status1.total_spent_usd

        # 2. Simulate process restart: fresh manager from SQLite ledger
        ledger2 = UsageLedger(db_path=str(usage_db))
        manager2 = BudgetManager(ledger=ledger2, pricing_table=pricing)
        budget2 = manager2.create_run_budget(
            workspace_id="ws_finance",
            run_id="run_budgeted_wf",
            config=BudgetConfig(max_cost_usd=Decimal("0.10")),
            default_model="mock-gpt-4o",
        )
        assert budget2.get_status().total_spent_usd == spend_after_step1
        active_budget = budget2

        # Fresh engine instance
        engine2 = WorkflowEngine(
            spec,
            registry,
            store=store,
            workspace_id="ws_finance",
            budget=budget2,
        )

        # 3. Resume after approval
        appr_id = res1.pending_approval.approval_id
        res2 = await engine2.resume(
            "run_budgeted_wf",
            appr_id,
            decision="approve",
        )
        assert res2.status == "completed"
        assert provider.call_count == 2

        # 4. Spending verification: spending accumulated without reset or double charge
        status2 = budget2.get_status()
        assert status2.provider_calls_count == 2
        assert status2.total_spent_usd == spend_after_step1 * 2
        assert status2.total_tokens == 300
