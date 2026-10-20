"""O04 Public Example: Trustworthy usage, cost accounting, and run diagnostics.

Demonstrates ledger-backed run diagnostics, observed vs calculated vs estimated costs,
token cache categories, successful-task cost cohort math, bounded secret redaction,
and explicit uncertainty handling without paid API calls or live providers.
"""

from __future__ import annotations

import json
from decimal import Decimal

from nexura_runtime.usage.models import ModelPricing
from nexura_runtime.usage.pricing import PricingTable

from nexura_runtime.observability.run_diagnostics import describe_run


def main() -> None:
    print("=== O04: Trustworthy Usage, Cost & Run Diagnostics Demo ===\n")

    # 1. Setup versioned operator pricing table (zero default assumptions)
    pricing = PricingTable(
        version="operator_rates_2026_10",
        date="2026-10-01",
        source="operator_config",
    )
    pricing.add_model_pricing(
        ModelPricing(
            model_id="gpt-4o",
            input_cost_per_million=Decimal("3.00"),
            output_cost_per_million=Decimal("15.00"),
            cached_prompt_cost_per_million=Decimal("0.75"),
        )
    )
    pricing.add_model_pricing(
        ModelPricing(
            model_id="gpt-4o-mini",
            input_cost_per_million=Decimal("0.15"),
            output_cost_per_million=Decimal("0.60"),
        )
    )

    print(f"1. Configured Operator Pricing Table: {pricing.version} ({pricing.date})")
    print("   Models: gpt-4o ($3.00/1M in, $0.75/1M cached, $15.00/1M out), gpt-4o-mini")

    # 2. Seed a realistic mixed-outcome workflow run
    # Cohort attempts:
    # - Node 1: Order triage (gpt-4o) - Succeeded with 10k prompt (8k cached) + 1k completion
    # - Node 2: Fraud assessment Attempt 1 (gpt-4o) - Failed with timeout (5k prompt, 0 completion)
    # - Node 2: Fraud assessment Attempt 2 (gpt-4o) - Succeeded (5k prompt + 800 completion)
    # - Node 3: Notification dispatch (gpt-4o-mini) - Partial (2k prompt, completion omitted)
    run_payload = {
        "run_id": "run-delhi-logistics-404",
        "status": "completed",
        "created_at": 1728300000.0,
        "updated_at": 1728300010.5,
        "approval_id": "appr-high-value-override",
        "events": [
            # Node 1: Order Triage
            {
                "seq": 1,
                "type": "MODULE_INVOKE_STARTED",
                "timestamp": 1728300000.5,
                "data": {
                    "node_id": "order_triage",
                    "module_id": "nexura.models.openai",
                    "attempt_id": "att_triage_1",
                    "provider_call_id": "call_llm_001",
                    "model_id": "gpt-4o",
                    "input_state": {
                        "order_id": "ORD-DL-9821",
                        "api_key": "sk-secret-do-not-leak",
                        "customer": "Vikram Singh",
                    },
                },
            },
            {
                "seq": 2,
                "type": "MODULE_INVOKE_COMPLETED",
                "timestamp": 1728300002.0,
                "data": {
                    "node_id": "order_triage",
                    "module_id": "nexura.models.openai",
                    "attempt_id": "att_triage_1",
                    "provider_call_id": "call_llm_001",
                    "model_id": "gpt-4o",
                    "payload": {
                        "usage": {
                            "prompt_tokens": 10000,
                            "cached_prompt_tokens": 8000,
                            "completion_tokens": 1000,
                        },
                        "outputs": {"priority": "high", "routing": "fraud_check"},
                    },
                },
            },
            # Node 2 Attempt 1: Failed
            {
                "seq": 3,
                "type": "MODULE_INVOKE_STARTED",
                "timestamp": 1728300002.5,
                "data": {
                    "node_id": "fraud_check",
                    "module_id": "nexura.models.openai",
                    "attempt_id": "att_fraud_1",
                    "provider_call_id": "call_llm_002_fail",
                    "model_id": "gpt-4o",
                },
            },
            {
                "seq": 4,
                "type": "MODULE_INVOKE_FAILED",
                "timestamp": 1728300004.0,
                "data": {
                    "node_id": "fraud_check",
                    "module_id": "nexura.models.openai",
                    "attempt_id": "att_fraud_1",
                    "provider_call_id": "call_llm_002_fail",
                    "model_id": "gpt-4o",
                    "payload": {
                        "usage": {"prompt_tokens": 5000, "completion_tokens": 0},
                        "error": "Upstream transport socket timeout",
                    },
                },
            },
            # Node 2 Attempt 2: Succeeded (Retry)
            {
                "seq": 5,
                "type": "MODULE_INVOKE_STARTED",
                "timestamp": 1728300004.5,
                "data": {
                    "node_id": "fraud_check",
                    "module_id": "nexura.models.openai",
                    "attempt_id": "att_fraud_2",
                    "provider_call_id": "call_llm_002_succ",
                    "reservation_id": "res_fraud_check_01",
                    "effect_id": "eff_risk_scored",
                    "model_id": "gpt-4o",
                },
            },
            {
                "seq": 6,
                "type": "MODULE_INVOKE_COMPLETED",
                "timestamp": 1728300006.0,
                "data": {
                    "node_id": "fraud_check",
                    "module_id": "nexura.models.openai",
                    "attempt_id": "att_fraud_2",
                    "provider_call_id": "call_llm_002_succ",
                    "reservation_id": "res_fraud_check_01",
                    "effect_id": "eff_risk_scored",
                    "model_id": "gpt-4o",
                    "payload": {
                        "usage": {"prompt_tokens": 5000, "completion_tokens": 800},
                        "outputs": {"risk_score": 12, "decision": "approve"},
                    },
                },
            },
            # Node 3: Notification Dispatch (Partial Usage)
            {
                "seq": 7,
                "type": "MODULE_INVOKE_STARTED",
                "timestamp": 1728300006.5,
                "data": {
                    "node_id": "notify",
                    "module_id": "nexura.models.openai",
                    "attempt_id": "att_notify_1",
                    "provider_call_id": "call_llm_003",
                    "model_id": "gpt-4o-mini",
                },
            },
            {
                "seq": 8,
                "type": "MODULE_INVOKE_COMPLETED",
                "timestamp": 1728300007.5,
                "data": {
                    "node_id": "notify",
                    "module_id": "nexura.models.openai",
                    "attempt_id": "att_notify_1",
                    "provider_call_id": "call_llm_003",
                    "model_id": "gpt-4o-mini",
                    "payload": {
                        # Provider only reported prompt tokens; completion omitted
                        "usage": {"prompt_tokens": 2000},
                        "outputs": {"dispatched": True},
                    },
                },
            },
        ],
    }

    # 3. Generate authoritative diagnostic projection with redacted content
    diag = describe_run(
        run_payload,
        pricing_table=pricing,
        include_content=True,
    )

    print("\n2. Diagnostic Projection Generated:")
    print(f"   Run ID: {diag['run_id']} (Status: {diag['status']})")
    print(f"   Total Attempts Recorded: {diag['attempt_count']}")

    u = diag["usage"]
    print("\n3. Observed Token Accounting (Zero Synthesized Tokens):")
    print(f"   Known Input Tokens:  {u['known_input_tokens']}")
    print(f"   Cached Input Tokens: {u['cached_input_tokens']} (Read from prefix cache)")
    print(f"   Known Output Tokens: {u['known_output_tokens']}")
    print(f"   Reported Attempts:   {u['reported_attempts']} of {u['cohort_attempt_count']}")
    print(f"   Complete Coverage:   {u['complete']}")
    if u["incomplete_coverage_explanation"]:
        print(f"   Coverage Note:       {u['incomplete_coverage_explanation']}")

    print("\n4. Financial Accounting (Separated by Certainty):")
    print(f"   Reported Cost:       ${u['reported_cost_usd']}")
    print(
        f"   Estimated Cost:      ${u['estimated_cost_usd']} "
        "(Conservative fallback on partial calls)"
    )
    print(f"   Uncertain Cost:      ${u['uncertain_cost_usd']}")
    print(f"   Total Calculated:    ${u['calculated_cost_usd']} ({u['cost_status']})")
    print(f"   Disclaimer:          {u['disclaimer']}")

    print("\n5. Successful-Task Cost Metric (Deliverable 6):")
    print(f"   Cohort Attempts:     {u['cohort_attempt_count']} (includes 1 failed attempt)")
    print(f"   Successful Tasks:    {u['task_success_count']}")
    print(f"   Successful-Task Cost: ${u['successful_task_cost_usd']} / task")
    print(f"   Status:              {u['successful_task_cost_status']}")

    print("\n6. Linked Identifiers & Redacted Context Audit:")
    for att in diag["attempts"]:
        status_label = "FAILED" if att["status"] == "failed" else "OK"
        print(f"   [{status_label}] Node: {att['node_id']} (Attempt: {att['attempt_id']})")
        print(f"        Provider Call: {att['provider_call_id']}")
        print(f"        Reservation:   {att['reservation_id']}")
        print(f"        Approval:      {att['approval_id']}")
        print(f"        Effect:        {att['effect_id']}")
        print(f"        Cost:          ${att['calculated_cost_usd']} ({att['cost_state']})")
        if att.get("input"):
            print(f"        Input (Redacted): {json.dumps(att['input'])}")

    # 7. Negative Boundary Demonstration: All-Failed Run
    print("\n7. Negative Boundary Test: Zero-Success Run")
    failed_run = {
        "run_id": "run-fail-zero-000",
        "status": "failed",
        "created_at": 1728300100.0,
        "events": [
            {
                "seq": 1,
                "type": "MODULE_INVOKE_STARTED",
                "timestamp": 1728300100.5,
                "data": {"node_id": "fatal_step", "attempt_id": "att_1", "model_id": "gpt-4o"},
            },
            {
                "seq": 2,
                "type": "MODULE_INVOKE_FAILED",
                "timestamp": 1728300101.5,
                "data": {
                    "node_id": "fatal_step",
                    "attempt_id": "att_1",
                    "model_id": "gpt-4o",
                    "payload": {
                        "usage": {"prompt_tokens": 10000, "completion_tokens": 0},
                        "error": "Rate limit exceeded",
                    },
                },
            },
        ],
    }
    fail_diag = describe_run(failed_run, pricing_table=pricing)
    fu = fail_diag["usage"]
    print(
        f"   Zero-Success Task Cost: {fu['successful_task_cost_usd']} "
        f"(Status: {fu['successful_task_cost_status']})"
    )
    print(f"   Failure Cohort Spend:   ${fu['calculated_cost_usd']} (Incurred despite 0 successes)")

    print("\n=== O04 Demonstration Completed Successfully ===")


if __name__ == "__main__":
    main()
