"""Acceptance tests for Module B28: Usage, Cost and Budget Enforcement."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

import pytest
from nexura_runtime.contracts.context import ExecutionContext
from nexura_runtime.types import Usage
from nexura_runtime.usage.errors import (
    BudgetExceededError,
    CurrencyConversionError,
)
from nexura_runtime.usage.ledger import UsageLedger
from nexura_runtime.usage.manager import BudgetManager
from nexura_runtime.usage.models import (
    BudgetConfig,
    FXRate,
    ModelPricing,
    TokenUsage,
)
from nexura_runtime.usage.module import UsageBudgetModule
from nexura_runtime.usage.pricing import (
    CurrencyConverter,
    PricingTable,
    create_operator_standard_pricing_table,
)

from nexura_runtime.modules.registry import default_registry


class TestConcurrentReservationsAndBoundSafety:
    """Verifies that concurrent reservations cannot overspend a hard configured bound."""

    @pytest.mark.asyncio
    async def test_concurrent_reservations_cannot_overspend_bound(self) -> None:
        manager = BudgetManager()
        ws_id = "ws_concurrent_01"
        run_id = "run_concurrent_01"

        # Hard budget of $0.50
        manager.set_workspace_budget(ws_id, BudgetConfig(max_cost_usd=Decimal("0.50")))
        run_budget = manager.create_run_budget(
            workspace_id=ws_id,
            run_id=run_id,
            config=BudgetConfig(max_cost_usd=Decimal("0.50")),
        )

        successful_reservations = []
        rejected_count = 0

        async def worker(worker_id: int) -> None:
            nonlocal rejected_count
            try:
                # 32,000 tokens = $0.20 under standard GPT-4o pricing
                # (16k in @ $2.50/M + 16k out @ $10.00/M)
                res = await run_budget.reserve_async(
                    estimated_tokens=32000,
                    node_id=f"node_{worker_id}",
                )
                successful_reservations.append(res)
            except BudgetExceededError:
                rejected_count += 1

        # Launch 20 concurrent tasks trying to reserve $0.20 against a $0.50 cap
        tasks = [asyncio.create_task(worker(i)) for i in range(20)]
        await asyncio.gather(*tasks)

        # At most 2 reservations can succeed ($0.20 * 2 = $0.40 <= $0.50)
        # The 3rd would push to $0.60 > $0.50 and must be rejected.
        status = run_budget.get_status()
        assert status.effective_cost_usd <= Decimal("0.50")
        assert len(successful_reservations) == 2
        assert rejected_count == 18
        assert rejected_count + len(successful_reservations) == 20


class TestUsagePreservationAndCertainty:
    """Verifies that partial or unknown usage is never treated as zero-cost certainty."""

    def test_unreported_usage_not_zero_cost(self) -> None:
        pricing_table = create_operator_standard_pricing_table()
        # Completely unreported usage (provider omitted token metrics)
        unknown_usage = TokenUsage(prompt_tokens=None, completion_tokens=None, total_tokens=None)
        assert not unknown_usage.is_reported

        est = pricing_table.calculate_cost(
            "gpt-4o",
            unknown_usage,
            conservative_fallback_cost=Decimal("0.05"),
        )
        assert est.state == "unknown"
        assert est.is_estimate is True
        assert est.amount_usd > Decimal("0")
        assert est.amount_usd == Decimal("0.05")

    def test_partial_usage_prompt_only_not_zero_output(self) -> None:
        pricing_table = create_operator_standard_pricing_table()
        # Provider reported prompt tokens, but completion tokens are unknown
        partial_usage = TokenUsage(prompt_tokens=1000, completion_tokens=None)

        est = pricing_table.calculate_cost(
            "gpt-4o",
            partial_usage,
            conservative_fallback_cost=Decimal("0.02"),
        )
        assert est.state == "estimated"
        assert est.is_estimate is True
        # Cost should include prompt cost + conservative output fallback
        # 1000 * 2.50 / 1_000_000 = 0.0025 + 0.02 = 0.0225
        assert est.amount_usd == Decimal("0.02250000")

    def test_sdk_usage_incomplete_flag_preserved(self) -> None:
        sdk_usage = Usage(prompt_tokens=500, completion_tokens=None, is_complete=False)
        tok_usage = TokenUsage.from_sdk_usage(sdk_usage)
        assert tok_usage.state == "estimated"
        assert tok_usage.is_complete is False


class TestConservativeCancellationReconciliation:
    """Verifies that cancelled requests reconcile conservatively according to execution stage."""

    def test_pre_dispatch_cancellation_releases_without_charge(self) -> None:
        manager = BudgetManager()
        run_budget = manager.create_run_budget(
            workspace_id="ws_cancel_test",
            run_id="run_cancel_pre",
            config=BudgetConfig(max_cost_usd=Decimal("1.00")),
        )

        res = run_budget.reserve(10000, model_id="gpt-4o")
        initial_status = run_budget.get_status()
        assert initial_status.total_reserved_usd > Decimal("0")

        # Cancelled strictly before dispatch
        cost_est = run_budget.reconcile(
            res.reservation_id,
            is_cancelled=True,
            is_pre_dispatch=True,
            description="Cancelled before network call was issued",
        )
        assert cost_est.amount_usd == Decimal("0")

        after_status = run_budget.get_status()
        assert after_status.total_reserved_usd == Decimal("0")
        assert after_status.total_spent_usd == Decimal("0")
        assert after_status.effective_cost_usd == Decimal("0")

    def test_in_flight_cancellation_reconciles_conservatively(self) -> None:
        manager = BudgetManager()
        run_budget = manager.create_run_budget(
            workspace_id="ws_cancel_test",
            run_id="run_cancel_inflight",
            config=BudgetConfig(
                max_cost_usd=Decimal("1.00"),
                conservative_unreported_cost_usd=Decimal("0.04"),
            ),
        )

        res = run_budget.reserve(10000, model_id="gpt-4o")
        assert run_budget.get_status().total_reserved_usd > Decimal("0")

        # Cancelled mid-flight (timeout or abort while waiting for response)
        cost_est = run_budget.reconcile(
            res.reservation_id,
            is_cancelled=True,
            is_pre_dispatch=False,
            description="Timeout waiting for provider stream",
        )
        assert cost_est.amount_usd > Decimal("0")
        assert cost_est.state == "estimated"

        status = run_budget.get_status()
        # Reservation released, but conservative cost is committed
        assert status.total_reserved_usd == Decimal("0")
        assert status.total_spent_usd > Decimal("0")
        assert status.effective_cost_usd == status.total_spent_usd


class TestHierarchicalBudgetsAndRetries:
    """Verifies child agents and retries are accounted hierarchically."""

    def test_child_agent_spend_bubbles_to_parent_and_workspace(self) -> None:
        manager = BudgetManager()
        ws_id = "ws_hierarchy"
        parent_run_id = "run_parent_01"
        child_run_id = "run_child_01"

        # Workspace limit: $1.00
        manager.set_workspace_budget(ws_id, BudgetConfig(max_cost_usd=Decimal("1.00")))
        # Parent limit: $0.50
        parent_budget = manager.create_run_budget(
            workspace_id=ws_id,
            run_id=parent_run_id,
            config=BudgetConfig(max_cost_usd=Decimal("0.50")),
        )
        # Child limit: $0.30
        child_budget = parent_budget.create_child_budget(
            child_run_id=child_run_id,
            config=BudgetConfig(max_cost_usd=Decimal("0.30")),
        )

        # Child reserves resources
        res = child_budget.reserve(5000, model_id="gpt-4o")

        # Verify child reservation counts toward parent and workspace status
        parent_status = parent_budget.get_status()
        ws_status = manager.get_workspace_status(ws_id)

        assert parent_status.total_reserved_usd > Decimal("0")
        assert ws_status.total_reserved_usd > Decimal("0")
        assert parent_status.total_reserved_usd == ws_status.total_reserved_usd

        # Child completes and reconciles
        child_budget.reconcile(
            res.reservation_id,
            usage=Usage(prompt_tokens=2000, completion_tokens=1000),
        )

        parent_status_after = parent_budget.get_status()
        ws_status_after = manager.get_workspace_status(ws_id)

        assert parent_status_after.total_spent_usd > Decimal("0")
        assert ws_status_after.total_spent_usd > Decimal("0")
        assert parent_status_after.total_spent_usd == ws_status_after.total_spent_usd

    def test_child_cannot_exceed_parent_budget(self) -> None:
        manager = BudgetManager()
        ws_id = "ws_hierarchy_breach"
        parent_run_id = "run_parent_tiny"
        child_run_id = "run_child_greedy"

        manager.set_workspace_budget(ws_id, BudgetConfig(max_cost_usd=Decimal("1.00")))
        # Parent has only $0.05 limit
        parent_budget = manager.create_run_budget(
            workspace_id=ws_id,
            run_id=parent_run_id,
            config=BudgetConfig(max_cost_usd=Decimal("0.05")),
        )
        # Child has $0.50 limit, but parent only has $0.05
        child_budget = parent_budget.create_child_budget(
            child_run_id=child_run_id,
            config=BudgetConfig(max_cost_usd=Decimal("0.50")),
        )

        # Attempt to reserve $0.10 in child run -> should be rejected by parent bound
        with pytest.raises(BudgetExceededError) as exc_info:
            child_budget.reserve(50000, model_id="gpt-4o")
        assert "Parent run" in exc_info.value.message

    def test_retries_accounted_against_turns(self) -> None:
        manager = BudgetManager()
        run_budget = manager.create_run_budget(
            workspace_id="ws_retries",
            run_id="run_retry_test",
            config=BudgetConfig(max_turns=3),
        )

        run_budget.record_turn()  # Turn 1
        run_budget.record_retry(1, reason="Network timeout")  # Turn 2
        run_budget.record_turn()  # Turn 3

        # Turn 4 should exceed limit
        with pytest.raises(BudgetExceededError) as exc_info:
            run_budget.record_turn()
        assert exc_info.value.limit_type == "turns"


class TestIndependentTurnAndToolCallLimits:
    """Verifies that turn and tool call caps are enforced independently of token estimates."""

    def test_turn_cap_enforced_independently(self) -> None:
        manager = BudgetManager()
        run_budget = manager.create_run_budget(
            workspace_id="ws_caps",
            run_id="run_turn_cap",
            config=BudgetConfig(max_turns=2, max_cost_usd=Decimal("100.00")),
        )

        assert run_budget.record_turn() == 1
        assert run_budget.record_turn() == 2

        with pytest.raises(BudgetExceededError) as exc:
            run_budget.record_turn()
        assert exc.value.limit_type == "turns"

    def test_tool_call_cap_enforced_independently(self) -> None:
        manager = BudgetManager()
        run_budget = manager.create_run_budget(
            workspace_id="ws_caps",
            run_id="run_tool_cap",
            config=BudgetConfig(max_tool_calls=2),
        )

        assert run_budget.record_tool_call("fetch_weather") == 1
        assert run_budget.record_tool_call("fetch_weather") == 2

        with pytest.raises(BudgetExceededError) as exc:
            run_budget.record_tool_call("fetch_weather")
        assert exc.value.limit_type == "tool_calls"


class TestDecimalPrecisionAndRounding:
    """Verifies exact Decimal arithmetic without floating point imprecision."""

    def test_fractional_cent_quantization(self) -> None:
        table = PricingTable(
            version="2026-10-01",
            date="2026-10-01",
            source="operator_test",
        )
        table.add_model_pricing(
            ModelPricing(
                model_id="micro-model",
                input_cost_per_million=Decimal("0.12345678"),
                output_cost_per_million=Decimal("0.87654321"),
            )
        )

        usage = TokenUsage(prompt_tokens=123, completion_tokens=456)
        est = table.calculate_cost("micro-model", usage)

        # Verify precision is maintained to 8 decimal places
        expected_input = (Decimal(123) * Decimal("0.12345678")) / Decimal("1000000")
        expected_output = (Decimal(456) * Decimal("0.87654321")) / Decimal("1000000")
        expected_total = (expected_input + expected_output).quantize(Decimal("0.00000001"))

        assert est.amount_usd == expected_total

    def test_accumulation_without_drift(self) -> None:
        ledger = UsageLedger()
        ws_id = "ws_precision"
        run_id = "run_precision"

        # Commit 10,000 tiny fractional amounts ($0.00001 each)
        tiny_val = Decimal("0.00001000")
        for i in range(100):
            ledger.record_commit(
                workspace_id=ws_id,
                run_id=run_id,
                amount_usd=tiny_val,
                tokens=10,
                description=f"step_{i}",
            )

        status = ledger.get_status(ws_id, run_id)
        # 100 * 0.00001000 = 0.00100000 exactly
        assert status.total_spent_usd == Decimal("0.00100000")


class TestMultiCurrencyAndFXConversion:
    """Verifies USD base storage and explicit operator-supplied FX rate conversion."""

    def test_inr_conversion_requires_explicit_rate_and_is_estimate(self) -> None:
        converter = CurrencyConverter()

        # Without rate configured, conversion fails
        with pytest.raises(CurrencyConversionError):
            converter.convert_usd_to_inr(Decimal("10.00"))

        # Configure dated rate from operator
        converter.set_rate(
            FXRate(
                from_currency="USD",
                to_currency="INR",
                rate=Decimal("83.50"),
                date="2026-10-06",
                source="operator_config_forex_daily",
            )
        )

        amount_inr, rate_used = converter.convert_usd_to_inr(Decimal("10.00"))
        assert amount_inr == Decimal("835.00")
        assert rate_used.rate == Decimal("83.50")
        assert rate_used.date == "2026-10-06"

    def test_attach_inr_estimate_to_cost_estimate(self) -> None:
        converter = CurrencyConverter(
            [
                FXRate(
                    from_currency="USD",
                    to_currency="INR",
                    rate=Decimal("84.00"),
                    date="2026-10-06",
                )
            ]
        )
        pricing = create_operator_standard_pricing_table()
        usage = TokenUsage(prompt_tokens=10000, completion_tokens=2000)
        est = pricing.calculate_cost("gpt-4o", usage)

        enriched = converter.attach_inr_estimate(est)
        assert enriched.inr_estimate is not None
        assert enriched.is_estimate is True
        assert enriched.fx_rate_used == Decimal("84.00")


class TestSqliteLedgerDurability:
    """Verifies SQLite ledger persistence and replay durability."""

    def test_ledger_entries_persist_across_reopen(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "usage_ledger.db")
        ledger1 = UsageLedger(db_path=db_path)

        res = ledger1.reserve(
            workspace_id="ws_persist",
            run_id="run_persist_01",
            estimated_tokens=5000,
            estimated_cost_usd=Decimal("0.125"),
        )
        ledger1.reconcile(
            res.reservation_id,
            actual_cost_usd=Decimal("0.110"),
            usage=TokenUsage(prompt_tokens=4000, completion_tokens=800),
            description="Completed first task",
        )

        # Reopen ledger with new instance pointing to same file
        ledger2 = UsageLedger(db_path=db_path)
        status = ledger2.get_status("ws_persist", "run_persist_01")

        assert status.total_spent_usd == Decimal("0.11000000")
        assert status.total_reserved_usd == Decimal("0")
        entries = ledger2.get_entries("ws_persist", "run_persist_01")
        assert len(entries) == 2  # reservation + reconcile


class TestUsageBudgetModuleContract:
    """Verifies B00/B01 module contract and registry discovery."""

    @pytest.mark.asyncio
    async def test_module_invoke_operations(self) -> None:
        mod = UsageBudgetModule()
        ctx = ExecutionContext(run_id="run_mod_test", node_id="node_1")

        # 1. Set budget
        set_res = await mod.invoke(
            ctx,
            {
                "operation": "set_budget",
                "max_cost_usd": "5.00",
                "max_tokens": 100000,
            },
            {},
        )
        assert set_res.status == "success"

        # 2. Reserve
        res_res = await mod.invoke(
            ctx,
            {
                "operation": "reserve",
                "estimated_tokens": 5000,
                "model_id": "gpt-4o",
            },
            {},
        )
        assert res_res.status == "success"
        res_id = res_res.outputs["reservation"]["reservation_id"]

        # 3. Reconcile
        rec_res = await mod.invoke(
            ctx,
            {
                "operation": "reconcile",
                "reservation_id": res_id,
                "usage": {"prompt_tokens": 3000, "completion_tokens": 1000},
                "model_id": "gpt-4o",
            },
            {},
        )
        assert rec_res.status == "success"
        assert rec_res.outputs["cost_estimate"]["amount_usd"] is not None

        # 4. Check budget
        chk_res = await mod.invoke(ctx, {"operation": "check_budget"}, {})
        assert chk_res.status == "success"
        assert chk_res.outputs["budget_status"]["total_spent_usd"] is not None

    def test_default_registry_contains_usage_module(self) -> None:
        assert default_registry.has("nexura.usage.budget")
        entry = default_registry.get("nexura.usage.budget")
        assert entry.manifest.title == "Usage, Cost, and Budget Governance"
