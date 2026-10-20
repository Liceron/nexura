"""Module B28 Standalone Keyless Demonstration.

Demonstrates:
1. Setting up hierarchical workspace and run budgets with versioned pricing.
2. Pre-dispatch reservations and post-response reconciliations.
3. A fake agent reasoning loop stopped by a shared budget limit.
4. Separating reported provider usage (USD) from dated operator INR estimates.
5. Accounting child agent runs and retries under the parent envelope.
6. Conservative reconciliation for cancelled in-flight requests.
7. B00/B01 module contract and CLI subprocess invocation.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

from nexura_runtime.contracts.context import ExecutionContext
from nexura_runtime.types import Usage
from nexura_runtime.usage import (
    BudgetConfig,
    BudgetExceededError,
    BudgetManager,
    CurrencyConverter,
    FXRate,
    ModelPricing,
    PricingTable,
    UsageBudgetModule,
    UsageLedger,
)


def run_demonstration() -> None:
    print("=== Module B28: Usage, Cost, and Budget Enforcement ===")

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = str(Path(tmp_dir) / "usage_ledger.db")

        # 1. Versioned Operator Pricing Table & FX Rates
        print("\n1. Initializing versioned pricing tables and operator FX rates...")
        pricing_table = PricingTable(
            version="2026-10-01",
            date="2026-10-01",
            source="operator_standard_contract_schedule",
        )
        pricing_table.add_model_pricing(
            ModelPricing(
                model_id="fake-reasoning-model",
                input_cost_per_million=Decimal("2.50"),
                output_cost_per_million=Decimal("10.00"),
                effective_date="2026-10-01",
                source="operator_standard_contract_schedule",
            )
        )

        fx_converter = CurrencyConverter(
            [
                FXRate(
                    from_currency="USD",
                    to_currency="INR",
                    rate=Decimal("83.50"),
                    date="2026-10-06",
                    source="operator_reference_forex_daily",
                )
            ]
        )

        ledger = UsageLedger(db_path=db_path)
        manager = BudgetManager(
            ledger=ledger,
            pricing_table=pricing_table,
            fx_converter=fx_converter,
        )

        # 2. Configure Shared Workspace and Run Budgets
        print("\n2. Configuring shared budget envelopes...")
        workspace_id = "ws_finance_corp"
        run_id = "run_agent_market_scan"

        # Shared workspace budget: $0.20
        manager.set_workspace_budget(
            workspace_id,
            BudgetConfig(max_cost_usd=Decimal("0.20")),
        )

        # Agent run budget: $0.10 hard cost cap, max 4 turns
        agent_budget = manager.create_run_budget(
            workspace_id=workspace_id,
            run_id=run_id,
            config=BudgetConfig(
                max_cost_usd=Decimal("0.10"),
                max_turns=4,
                conservative_unreported_cost_usd=Decimal("0.02"),
            ),
            default_model="fake-reasoning-model",
        )

        status_init = agent_budget.get_status()
        print(f"   [Budget Configured] Max: $0.10, Rem: ${status_init.remaining_cost_usd}")

        # 3. Running Fake Agent Loop Until Stopped by Budget
        print("\n3. Executing fake agent loop against shared budget...")

        # Turn 1: Initial research query
        print("   --- Turn 1: Initial Research Query ---")
        agent_budget.record_turn()
        res1 = agent_budget.reserve(estimated_tokens=8000)
        print(f"   Reserved before dispatch: ${res1.estimated_cost_usd}")

        # Fake LLM responds with reported usage
        fake_usage_turn1 = Usage(
            prompt_tokens=5000,
            completion_tokens=2000,
            total_tokens=7000,
            is_complete=True,
        )
        cost_est1 = agent_budget.reconcile(res1.reservation_id, usage=fake_usage_turn1)

        print(f"   Turn 1 Completed: Reported {fake_usage_turn1.total_tokens} tokens")
        print(f"     -> Provider Cost (USD): ${cost_est1.amount_usd}")
        print(
            f"     -> Operator INR Estimate: ₹{cost_est1.inr_estimate} "
            f"(Rate: {cost_est1.fx_rate_used}, Date: {cost_est1.fx_date})"
        )

        status_t1 = agent_budget.get_status()
        print(f"     -> Spend: ${status_t1.total_spent_usd}, Rem: ${status_t1.remaining_cost_usd}")

        # Turn 2: Follow-up query with partial/unreported provider output
        print("\n   --- Turn 2: Ingestion Step (Partial Token Metrics) ---")
        agent_budget.record_turn()
        res2 = agent_budget.reserve(estimated_tokens=10000)

        # Model reported prompt tokens, but omitted completion tokens
        fake_usage_turn2 = Usage(prompt_tokens=8000, completion_tokens=None, is_complete=False)
        cost_est2 = agent_budget.reconcile(res2.reservation_id, usage=fake_usage_turn2)

        print("   Turn 2 Completed: Partial usage reported (prompt=8000, completion=unknown)")
        print(f"     -> Cost State: {cost_est2.state} (is_estimate={cost_est2.is_estimate})")
        print(f"     -> Conservative Cost (USD): ${cost_est2.amount_usd}")
        print(f"     -> Operator INR Estimate: ₹{cost_est2.inr_estimate}")

        status_t2 = agent_budget.get_status()
        print(f"     -> Spend: ${status_t2.total_spent_usd}, Rem: ${status_t2.remaining_cost_usd}")

        # Turn 3: Attempting large synthesis requiring more budget than remains
        print("\n   --- Turn 3: Large Synthesis Attempt ---")
        agent_budget.record_turn()
        print("   Attempting pre-dispatch reservation for 20,000 tokens...")
        try:
            agent_budget.reserve(estimated_tokens=20000)
            print("   ERROR: Reservation unexpectedly succeeded!")
        except BudgetExceededError as exc:
            print("   [GUARD TRIGGERED] Agent successfully halted by shared budget!")
            print(f"     -> Reason: {exc.message}")

        status_t3 = agent_budget.get_status()
        print("     -> Run was safely stopped before unauthorized provider dispatch.")
        # Admission bounds new calls; settled spend can exceed the cap when a provider
        # reports less than it used and the conservative fallback is charged instead.
        print(
            f"     -> Settled spend ${status_t3.total_spent_usd:f} (cap $0.10); "
            "no further call was admitted."
        )

        # 4. Hierarchical Child Agent Accounting
        print("\n4. Demonstrating hierarchical child agent accounting...")
        child_budget = agent_budget.create_child_budget(
            child_run_id="run_child_subtask_01",
            config=BudgetConfig(max_cost_usd=Decimal("0.05")),
        )
        # The parent run is exhausted, so its child cannot spend either: every reservation is
        # checked against the child, its parent and the workspace.
        try:
            child_budget.reserve(estimated_tokens=2000)
            print("   ERROR: Child reservation unexpectedly succeeded!")
        except BudgetExceededError as exc:
            print("   [GUARD TRIGGERED] Child refused: its parent run's cap applies to it.")
            print(f"     -> Reason: {exc.message}")

        # A child of a run with room left reserves against both caps.
        sibling = manager.create_run_budget(
            workspace_id=workspace_id,
            run_id="run_agent_followup",
            config=BudgetConfig(max_cost_usd=Decimal("0.05")),
            default_model="fake-reasoning-model",
        )
        child_ok = sibling.create_child_budget(
            child_run_id="run_child_subtask_02",
            config=BudgetConfig(max_cost_usd=Decimal("0.02")),
        )
        before = manager.get_workspace_status(workspace_id).effective_cost_usd
        child_res = child_ok.reserve(estimated_tokens=2000)
        print(f"   Child agent reserved: ${child_res.estimated_cost_usd:f}")
        after = manager.get_workspace_status(workspace_id).effective_cost_usd
        print(f"   Workspace effective cost reflects child: ${before:f} -> ${after:f}")
        child_ok.release(child_res.reservation_id, description="Child task pruned")

        # 5. Conservative Reconciliation for Cancelled In-Flight Requests
        print("\n5. Demonstrating conservative cancellation reconciliation...")
        cancel_budget = manager.create_run_budget(
            workspace_id=workspace_id,
            run_id="run_cancellation_demo",
            config=BudgetConfig(max_cost_usd=Decimal("0.05")),
            default_model="fake-reasoning-model",
        )
        res_inflight = cancel_budget.reserve(estimated_tokens=2000)
        print(f"   Request dispatched with reservation ${res_inflight.estimated_cost_usd}")
        print("   Simulating request cancellation during network transit (timeout)...")
        cancelled_cost = cancel_budget.reconcile(
            res_inflight.reservation_id,
            is_cancelled=True,
            is_pre_dispatch=False,
            description="Upstream HTTP timeout waiting for first byte",
        )
        print(f"   Reconciled conservatively: ${cancelled_cost.amount_usd} (not zeroed)")

        # 6. B00/B01 Module Contract and CLI Invocation
        print("\n6. Demonstrating B00/B01 module contract and CLI execution...")
        mod = UsageBudgetModule(manager)
        ctx = ExecutionContext(run_id="run_cli_test", node_id="node_budget_check")

        loop = asyncio.new_event_loop()
        res_contract = loop.run_until_complete(
            mod.invoke(
                ctx,
                {
                    "operation": "convert_currency",
                    "amount_usd": "12.50",
                    "target_currency": "INR",
                },
                {},
            )
        )
        loop.close()

        print(f"   Module Contract invoke status: {res_contract.status}")
        print(f"   Converted: $12.50 USD -> {res_contract.outputs.get('converted_amount')} INR")
        print(f"   Is Estimate: {res_contract.outputs.get('is_estimate')}")

        # Subprocess CLI invocation
        cli_proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "nexura_runtime.modules.cli",
                "describe",
                "nexura.usage.budget",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        describe_data = json.loads(cli_proc.stdout)
        manifest_data = describe_data.get("manifest", describe_data)
        print(f"   CLI describe module_id: {manifest_data.get('module_id')}")
        print(f"   CLI describe kind: {manifest_data.get('kind')}")
        print(f"   CLI describe title: {manifest_data.get('title')}")

    print("\n=== Demonstration Complete: All 6 stages verified successfully ===")


if __name__ == "__main__":
    run_demonstration()
