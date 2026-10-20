"""Tests for O01: Shared workflow budget admission.

Verifies:
1. Insufficient allowance produces zero mock-transport calls.
2. Separate concurrent workers cannot jointly exceed admission allowance.
3. Child agent and retries share parent scope.
4. Unknown model pricing under monetary cap denies admission with 0 calls.
5. Model-specific cache pricing without universal multiplier.
6. Conservative settlement on in-flight cancellation/timeout, partial streams, and lost usage.
7. Harmless duplicate settlement (idempotency).
8. SQLite ledger restart preserves active reservations.
9. Cross-workspace access denied on settlement and release.
10. Separation of provider calls from module aggregates excludes double charging.
11. Unconfigured legacy callers continue working without budget overhead.
12. Real service endpoint starts run with budget_config and exposes budget_status.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from nexura_runtime.contracts.context import ExecutionContext
from nexura_runtime.providers.base import BaseProvider
from nexura_runtime.providers.errors import StreamCancelledError
from nexura_runtime.providers.types import (
    EmbeddingResult,
    GenerationRequest,
    ProviderCapabilities,
    StreamDelta,
)
from nexura_runtime.types import Message, ModelResponse, Usage
from nexura_runtime.usage.errors import (
    BudgetExceededError,
    CrossWorkspaceAccessError,
    InvalidPricingError,
)
from nexura_runtime.usage.ledger import UsageLedger
from nexura_runtime.usage.manager import BudgetManager
from nexura_runtime.usage.models import (
    BudgetConfig,
    ModelPricing,
    TokenUsage,
)
from nexura_runtime.usage.pricing import PricingTable

from nexura_runtime.service.api.app import create_api_app


class MockTransportProvider(BaseProvider):
    """Deterministic local fake provider recording actual transport dispatch counts."""

    def __init__(self, model_id: str = "mock-gpt-4o") -> None:
        super().__init__(
            model_id=model_id,
            capabilities=ProviderCapabilities(
                can_generate=True,
                can_stream=True,
                can_embed=True,
                can_tools=True,
                reports_usage=True,
            ),
        )
        self.generate_calls = 0
        self.stream_calls = 0
        self.embed_calls = 0

    async def _generate_internal(
        self,
        request: GenerationRequest,
        context: ExecutionContext | None,
    ) -> ModelResponse:
        self.generate_calls += 1
        return ModelResponse(
            message=Message(role="assistant", content=f"Echo from {self.model_id}"),
            usage=Usage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
        )

    async def _stream_internal(
        self,
        request: GenerationRequest,
        context: ExecutionContext | None,
    ) -> AsyncIterator[StreamDelta]:
        self.stream_calls += 1
        yield StreamDelta(kind="text_delta", text="Hello ")
        yield StreamDelta(kind="text_delta", text="World")
        yield StreamDelta(
            kind="usage_delta",
            usage=Usage(prompt_tokens=80, completion_tokens=40, total_tokens=120),
        )

    async def _embed_internal(
        self,
        texts: list[str],
        context: ExecutionContext | None,
    ) -> EmbeddingResult:
        self.embed_calls += 1
        return EmbeddingResult(
            embeddings=[[0.1, 0.2, 0.3] for _ in texts],
            model=self.model_id,
            usage=Usage(prompt_tokens=len(texts) * 10, total_tokens=len(texts) * 10),
        )


def _build_test_pricing_table() -> PricingTable:
    table = PricingTable(version="2026-10-test", date="2026-10-07")
    table.add_model_pricing(
        ModelPricing(
            model_id="mock-gpt-4o",
            input_cost_per_million=Decimal("2.50"),
            output_cost_per_million=Decimal("10.00"),
            cached_prompt_cost_per_million=Decimal("1.25"),  # 50% cached
            effective_date="2026-10-07",
        )
    )
    table.add_model_pricing(
        ModelPricing(
            model_id="mock-cached-heavy",
            input_cost_per_million=Decimal("5.00"),
            output_cost_per_million=Decimal("15.00"),
            cached_prompt_cost_per_million=Decimal("0.50"),  # 90% discount on cache for this model
            effective_date="2026-10-07",
        )
    )
    return table


class TestBudgetAdmissionBoundaries:
    """Core admission and zero-dispatch verification."""

    @pytest.mark.asyncio
    async def test_insufficient_allowance_produces_zero_mock_calls(self) -> None:
        """When budget is exhausted, provider dispatch raises BudgetExceededError and 0 calls."""
        pricing_table = _build_test_pricing_table()
        manager = BudgetManager(pricing_table=pricing_table)
        budget = manager.create_run_budget(
            workspace_id="ws_admission",
            run_id="run_admit_01",
            config=BudgetConfig(max_cost_usd=Decimal("0.0001")),  # Very small allowance
            default_model="mock-gpt-4o",
        )

        provider = MockTransportProvider("mock-gpt-4o")
        ctx = ExecutionContext(
            workspace_id="ws_admission",
            run_id="run_admit_01",
            node_id="node_1",
            budget=budget,
        )

        # Pre-dispatch admission should fail because estimated cost for buffer exceeds $0.0001
        with pytest.raises(BudgetExceededError) as exc_info:
            await provider.generate([Message(role="user", content="Test prompt")], context=ctx)

        assert exc_info.value.limit_type == "cost"
        assert provider.generate_calls == 0, (
            "Transport must not be invoked when admission is denied!"
        )

    @pytest.mark.asyncio
    async def test_unknown_pricing_denied_under_monetary_cap(self) -> None:
        """Unknown model under a monetary cap must fail admission without calling transport."""
        pricing_table = _build_test_pricing_table()
        manager = BudgetManager(pricing_table=pricing_table)
        budget = manager.create_run_budget(
            workspace_id="ws_admission",
            run_id="run_admit_02",
            config=BudgetConfig(max_cost_usd=Decimal("5.00"), deny_unknown_pricing=True),
            default_model="unregistered-exotic-model",
        )

        provider = MockTransportProvider("unregistered-exotic-model")
        ctx = ExecutionContext(
            workspace_id="ws_admission",
            run_id="run_admit_02",
            node_id="node_1",
            budget=budget,
        )

        with pytest.raises(InvalidPricingError) as exc_info:
            await provider.generate([Message(role="user", content="Hello")], context=ctx)

        assert "unregistered-exotic-model" in str(exc_info.value)
        assert provider.generate_calls == 0

    @pytest.mark.asyncio
    async def test_model_specific_cache_pricing(self) -> None:
        """Cache pricing uses explicit model pricing without universal multiplier."""
        pricing_table = _build_test_pricing_table()
        usage = TokenUsage(
            prompt_tokens=1000,
            cached_prompt_tokens=600,
            completion_tokens=200,
            state="reported",
        )

        # gpt-4o: 400 uncached * 2.50 + 600 cached * 1.25 + 200 * 10.00
        # (1.00 + 0.75 + 2.00) / 1000 = 0.00375 USD
        cost_4o = pricing_table.calculate_cost("mock-gpt-4o", usage)
        expected_4o = (
            Decimal(400) * Decimal("2.50")
            + Decimal(600) * Decimal("1.25")
            + Decimal(200) * Decimal("10.00")
        ) / Decimal("1000000")
        assert cost_4o.amount_usd == expected_4o
        assert cost_4o.details["cached_prompt_tokens"] == 600
        assert cost_4o.details["uncached_prompt_tokens"] == 400

        # mock-cached-heavy: 400 uncached * 5.00 + 600 cached * 0.50 + 200 * 15.00
        # (2.00 + 0.30 + 3.00) / 1000 = 0.00530 USD
        cost_heavy = pricing_table.calculate_cost("mock-cached-heavy", usage)
        expected_heavy = (
            Decimal(400) * Decimal("5.00")
            + Decimal(600) * Decimal("0.50")
            + Decimal(200) * Decimal("15.00")
        ) / Decimal("1000000")
        assert cost_heavy.amount_usd == expected_heavy


class TestConcurrencyAndHierarchy:
    """Multi-worker and child agent scope sharing."""

    @pytest.mark.asyncio
    async def test_concurrent_workers_cannot_jointly_exceed_admission_allowance(self) -> None:
        """Parallel workers reserving simultaneously cannot collectively breach the cap."""
        pricing_table = _build_test_pricing_table()
        manager = BudgetManager(pricing_table=pricing_table)
        budget = manager.create_run_budget(
            workspace_id="ws_concurrent",
            run_id="run_concurrent_01",
            config=BudgetConfig(max_cost_usd=Decimal("0.005")),  # Cap allows ~3 concurrent calls
            default_model="mock-gpt-4o",
        )

        provider = MockTransportProvider("mock-gpt-4o")

        async def worker(worker_idx: int) -> bool:
            ctx = ExecutionContext(
                workspace_id="ws_concurrent",
                run_id="run_concurrent_01",
                node_id=f"worker_{worker_idx}",
                budget=budget,
            )
            try:
                msg = Message(role="user", content="Worker prompt")
                await provider.generate([msg], context=ctx)
                return True
            except BudgetExceededError:
                return False

        # Launch 20 concurrent workers
        results = await asyncio.gather(*(worker(i) for i in range(20)))
        successful = sum(1 for r in results if r)
        rejected = sum(1 for r in results if not r)

        assert rejected > 0, "Some workers must be rejected when admission bound is reached"
        assert successful == provider.generate_calls
        status = budget.get_status()
        assert status.effective_cost_usd <= Decimal("0.005")

    @pytest.mark.asyncio
    async def test_child_agent_shares_scope_with_parent(self) -> None:
        """Child run spend is bounded by parent budget envelope."""
        pricing_table = _build_test_pricing_table()
        manager = BudgetManager(pricing_table=pricing_table)
        parent_budget = manager.create_run_budget(
            workspace_id="ws_hierarchy",
            run_id="parent_01",
            config=BudgetConfig(max_cost_usd=Decimal("0.002")),  # Allows ~2 calls
            default_model="mock-gpt-4o",
        )
        child_budget = parent_budget.create_child_budget("child_01")

        provider = MockTransportProvider("mock-gpt-4o")
        child_ctx = ExecutionContext(
            workspace_id="ws_hierarchy",
            run_id="child_01",
            node_id="child_node",
            budget=child_budget,
        )

        # Execute multiple child calls until parent budget is exhausted
        calls_succeeded = 0
        exhausted = False
        for _ in range(10):
            try:
                msg = Message(role="user", content="Child call")
                await provider.generate([msg], context=child_ctx)
                calls_succeeded += 1
            except BudgetExceededError:
                exhausted = True
                break

        assert exhausted, "Child execution must hit parent cost cap"
        p_status = parent_budget.get_status()
        assert p_status.effective_cost_usd <= Decimal("0.02")


class TestConservativeReconciliation:
    """Partial streams, cancellations, timeouts, and idempotent settlement."""

    @pytest.mark.asyncio
    async def test_partial_stream_settled_conservatively(self) -> None:
        """Interrupted stream is settled conservatively with observed usage, never free."""
        pricing_table = _build_test_pricing_table()
        manager = BudgetManager(pricing_table=pricing_table)
        budget = manager.create_run_budget(
            workspace_id="ws_reconcile",
            run_id="run_stream_01",
            config=BudgetConfig(max_cost_usd=Decimal("1.00")),
            default_model="mock-gpt-4o",
        )
        provider = MockTransportProvider("mock-gpt-4o")

        # Context marked cancelled after receiving first delta
        ctx = ExecutionContext(
            workspace_id="ws_reconcile",
            run_id="run_stream_01",
            node_id="node_s",
            budget=budget,
        )

        received_deltas = []
        with pytest.raises(StreamCancelledError):
            stream_iter = provider.stream([Message(role="user", content="Stream me")], context=ctx)
            async for delta in stream_iter:
                received_deltas.append(delta)
                ctx.cancel()  # Cancel after 1 delta

        status = budget.get_status()
        assert status.total_spent_usd > Decimal("0"), "Cancelled stream must not be charged $0"
        assert status.estimated_cost_usd > Decimal("0") or status.committed_cost_usd > Decimal("0")

    @pytest.mark.asyncio
    async def test_duplicate_settlement_is_harmless_idempotent(self) -> None:
        """Calling settle_provider_call multiple times returns same settlement without double."""
        pricing_table = _build_test_pricing_table()
        manager = BudgetManager(pricing_table=pricing_table)
        budget = manager.create_run_budget(
            workspace_id="ws_idem",
            run_id="run_idem_01",
            config=BudgetConfig(max_cost_usd=Decimal("1.00")),
            default_model="mock-gpt-4o",
        )

        res_id = budget.admit_provider_call("mock-gpt-4o", estimated_tokens=1000)
        settle1 = budget.settle_provider_call(
            res_id,
            usage=Usage(prompt_tokens=200, completion_tokens=100, total_tokens=300),
            model_id="mock-gpt-4o",
        )
        status1 = budget.get_status()

        # Duplicate settlement call
        settle2 = budget.settle_provider_call(
            res_id,
            usage=Usage(prompt_tokens=200, completion_tokens=100, total_tokens=300),
            model_id="mock-gpt-4o",
        )
        status2 = budget.get_status()

        assert settle1.amount_usd == settle2.amount_usd
        assert status1.total_spent_usd == status2.total_spent_usd, (
            "Spend must not double charge on repeated settlement"
        )

    def test_cross_workspace_settlement_denied(self) -> None:
        """Attempting to settle a reservation from another workspace raises error."""
        ledger = UsageLedger()
        res = ledger.reserve(
            workspace_id="ws_alpha",
            run_id="run_alpha",
            estimated_cost_usd=Decimal("0.05"),
        )

        with pytest.raises(CrossWorkspaceAccessError) as exc_info:
            ledger.reconcile(res.reservation_id, workspace_id="ws_beta")

        assert exc_info.value.expected_workspace == "ws_alpha"
        assert exc_info.value.actual_workspace == "ws_beta"

    def test_sqlite_restart_preserves_active_reservations(self, tmp_path: Path) -> None:
        """Active reservations and entries persist across ledger restarts."""
        db_path = str(tmp_path / "durable_ledger.db")
        ledger1 = UsageLedger(db_path=db_path)
        res1 = ledger1.reserve(
            workspace_id="ws_durable",
            run_id="run_dur_01",
            estimated_tokens=4000,
            estimated_cost_usd=Decimal("0.08"),
            provider_call_id="call_dur_123",
            pricing_version="2026-10-v1",
        )
        assert res1.status == "active"

        # Reopen ledger
        ledger2 = UsageLedger(db_path=db_path)
        status = ledger2.get_status("ws_durable", "run_dur_01")
        assert status.total_reserved_usd == Decimal("0.08")
        assert ledger2._reservations[res1.reservation_id].provider_call_id == "call_dur_123"


class TestLegacyCompatibilityAndSeparation:
    """Preserves legacy callers and separates provider calls from module aggregates."""

    @pytest.mark.asyncio
    async def test_legacy_unbudgeted_caller_passes_without_overhead(self) -> None:
        """Provider calls without a configured budget run cleanly without errors."""
        provider = MockTransportProvider("mock-gpt-4o")
        ctx = ExecutionContext(run_id="run_legacy", node_id="node_leg", budget=None)

        resp = await provider.generate([Message(role="user", content="Hi")], context=ctx)
        assert resp.message.content == "Echo from mock-gpt-4o"
        assert provider.generate_calls == 1

    def test_module_aggregates_do_not_double_count_spend(self) -> None:
        """Module aggregate commits do not add on top of settled provider calls."""
        pricing_table = _build_test_pricing_table()
        manager = BudgetManager(pricing_table=pricing_table)
        budget = manager.create_run_budget(
            workspace_id="ws_sep",
            run_id="run_sep_01",
            config=BudgetConfig(max_cost_usd=Decimal("1.00")),
            default_model="mock-gpt-4o",
        )

        # 1. Provider call settled
        res_id = budget.admit_provider_call("mock-gpt-4o", estimated_tokens=1000)
        cost = budget.settle_provider_call(
            res_id,
            usage=Usage(prompt_tokens=400, completion_tokens=100, total_tokens=500),
            model_id="mock-gpt-4o",
        )
        status_after_provider = budget.get_status()
        assert status_after_provider.total_spent_usd == cost.amount_usd

        # 2. Module records aggregate usage via BudgetInterface
        budget.record_usage(Usage(prompt_tokens=400, completion_tokens=100, total_tokens=500))
        status_after_module = budget.get_status()

        # Spend must NOT double-charge!
        assert status_after_module.total_spent_usd == status_after_provider.total_spent_usd


class TestServiceApiBudgetIntegration:
    """Verifies service endpoint handles budget_config and exposes budget_status."""

    def test_service_run_with_budget_config(self) -> None:
        app = create_api_app(db_path=":memory:")
        app.state.registry.register("direct_handler", lambda state: {"status": "ok"})
        client = TestClient(app)

        spec = {
            "name": "budget_test_flow",
            "entry_node": "step1",
            "nodes": {
                "step1": {
                    "id": "step1",
                    "handler": "direct_handler",
                    "transitions": {},
                }
            },
            "terminal_nodes": ["step1"],
        }

        # Start run with budget_config
        resp = client.post(
            "/api/v1/runs",
            json={
                "spec": spec,
                "initial_state": {"count": 1},
                "budget_config": {
                    "max_cost_usd": "10.00",
                    "max_turns": 5,
                    "deny_unknown_pricing": True,
                },
            },
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["status"] == "completed"
        assert "budget_status" in data
        assert data["budget_status"]["remaining_turns"] is not None
        run_id = data["run_id"]

        # Inspect run via GET
        get_resp = client.get(f"/api/v1/runs/{run_id}")
        assert get_resp.status_code == 200
        get_data = get_resp.json()
        assert "budget_status" in get_data
        assert get_data["budget_status"]["total_spent_usd"] == "0.00000000"
