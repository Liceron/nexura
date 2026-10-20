"""Test suite for O04: Trustworthy usage, cost, and run diagnostics."""

from __future__ import annotations

import time
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from nexura_runtime.usage.ledger import UsageLedger
from nexura_runtime.usage.models import ModelPricing
from nexura_runtime.usage.pricing import PricingTable

from nexura_runtime.observability.run_diagnostics import (
    describe_run,
    redact_run_content,
)
from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.studio.store import StudioStore
from nexura_runtime.workflow import WorkflowNode, WorkflowSpec


@pytest.fixture
def operator_pricing() -> PricingTable:
    pt = PricingTable(
        version="operator_pricing_2026_10",
        date="2026-10-01",
        source="operator_config",
    )
    # $3.00 / 1M input, $15.00 / 1M output, $0.75 / 1M cached input
    pt.add_model_pricing(
        ModelPricing(
            model_id="gpt-4o",
            input_cost_per_million=Decimal("3.00"),
            output_cost_per_million=Decimal("15.00"),
            cached_prompt_cost_per_million=Decimal("0.75"),
        )
    )
    # $0.15 / 1M input, $0.60 / 1M output
    pt.add_model_pricing(
        ModelPricing(
            model_id="gpt-4o-mini",
            input_cost_per_million=Decimal("0.15"),
            output_cost_per_million=Decimal("0.60"),
        )
    )
    return pt


@pytest.fixture
def test_app(tmp_path: Path, operator_pricing: PricingTable) -> FastAPI:
    db_file = tmp_path / "test_o04.sqlite3"
    app = create_api_app(db_path=db_file, profile="local-owner")
    app.state.pricing_table = operator_pricing
    return app


# ============================================================================
# 1. ID Linking & Module Aggregates Separation
# ============================================================================


class TestRunDiagnosticsIDLinkingAndAggregates:
    """Deliverable 1: Link run/node/attempt/provider/reservation/approval/effect IDs.

    Ensure module aggregates are not counted as new provider usage.
    """

    def test_link_ids_across_lifecycle_events(self) -> None:
        run_data = {
            "run_id": "run-link-101",
            "status": "completed",
            "created_at": 1000.0,
            "updated_at": 1002.5,
            "approval_id": "appr-pay-999",
            "events": [
                {
                    "seq": 1,
                    "type": "MODULE_INVOKE_STARTED",
                    "timestamp": 1000.1,
                    "data": {
                        "node_id": "payout_node",
                        "module_id": "nexura.models.openai",
                        "attempt_id": "att-1",
                        "provider_call_id": "call_openai_abc123",
                        "reservation_id": "res_budget_555",
                        "effect_id": "eff_stripe_payout",
                        "model_id": "gpt-4o",
                    },
                },
                {
                    "seq": 2,
                    "type": "MODULE_INVOKE_COMPLETED",
                    "timestamp": 1001.2,
                    "data": {
                        "node_id": "payout_node",
                        "module_id": "nexura.models.openai",
                        "attempt_id": "att-1",
                        "provider_call_id": "call_openai_abc123",
                        "reservation_id": "res_budget_555",
                        "effect_id": "eff_stripe_payout",
                        "model_id": "gpt-4o",
                        "payload": {
                            "usage": {"prompt_tokens": 120, "completion_tokens": 40},
                        },
                    },
                },
            ],
        }

        diag = describe_run(run_data)
        assert diag["attempt_count"] == 1
        att = diag["attempts"][0]

        assert att["run_id"] == "run-link-101"
        assert att["node_id"] == "payout_node"
        assert att["attempt_id"] == "att-1"
        assert att["provider_call_id"] == "call_openai_abc123"
        assert att["reservation_id"] == "res_budget_555"
        assert att["approval_id"] == "appr-pay-999"
        assert att["effect_id"] == "eff_stripe_payout"
        assert att["model_id"] == "gpt-4o"
        assert att["input_tokens"] == 120
        assert att["output_tokens"] == 40
        assert att["total_tokens"] == 160

    def test_module_aggregates_not_counted_as_new_provider_usage(self) -> None:
        run_data = {
            "run_id": "run-agg-202",
            "status": "completed",
            "created_at": 2000.0,
            "updated_at": 2005.0,
            "events": [
                # Real provider call attempt 1
                {
                    "seq": 1,
                    "type": "MODULE_INVOKE_STARTED",
                    "timestamp": 2000.1,
                    "data": {
                        "node_id": "leaf_llm_1",
                        "module_id": "nexura.models.openai",
                        "attempt_id": "att-leaf-1",
                        "model_id": "gpt-4o",
                    },
                },
                {
                    "seq": 2,
                    "type": "MODULE_INVOKE_COMPLETED",
                    "timestamp": 2001.0,
                    "data": {
                        "node_id": "leaf_llm_1",
                        "module_id": "nexura.models.openai",
                        "attempt_id": "att-leaf-1",
                        "payload": {
                            "usage": {"prompt_tokens": 500, "completion_tokens": 100},
                        },
                    },
                },
                # Wrapper module aggregate event (reporting combined tally)
                {
                    "seq": 3,
                    "type": "MODULE_INVOKE_STARTED",
                    "timestamp": 2001.1,
                    "data": {
                        "node_id": "composite_pipeline",
                        "module_id": "nexura.workflows.composite",
                        "attempt_id": "att-comp-1",
                        "is_aggregate": True,
                        "entry_category": "module_aggregate",
                    },
                },
                {
                    "seq": 4,
                    "type": "MODULE_INVOKE_COMPLETED",
                    "timestamp": 2002.0,
                    "data": {
                        "node_id": "composite_pipeline",
                        "module_id": "nexura.workflows.composite",
                        "attempt_id": "att-comp-1",
                        "is_aggregate": True,
                        "entry_category": "module_aggregate",
                        "payload": {
                            "usage": {"prompt_tokens": 500, "completion_tokens": 100},
                        },
                    },
                },
            ],
        }

        diag = describe_run(run_data)
        # Total recorded attempts = 2, but provider tallies MUST NOT double-count the aggregate
        assert diag["attempt_count"] == 2
        u = diag["usage"]
        assert u["known_input_tokens"] == 500
        assert u["known_output_tokens"] == 100
        assert u["total_tokens"] == 600
        # Cohort attempt count only counts underlying provider attempts
        assert u["cohort_attempt_count"] == 1


# ============================================================================
# 2. Observed Usage, Cost Accounting, and Uncertainty Separation
# ============================================================================


class TestObservedUsageAndCostSeparation:
    """Deliverables 2 & 3: Report observed usage, calculated costs, and uncertainty separately."""

    def test_observed_vs_calculated_vs_estimated_vs_uncertain_costs(
        self, operator_pricing: PricingTable
    ) -> None:
        run_data = {
            "run_id": "run-costs-303",
            "status": "completed",
            "created_at": 3000.0,
            "updated_at": 3004.0,
            "events": [
                # 1. Reported: 10,000 prompt, 2,000 completion on gpt-4o
                # cost = (10,000 * 3.00 + 2,000 * 15.00) / 1,000,000 = $0.06000000
                {
                    "seq": 1,
                    "type": "MODULE_INVOKE_STARTED",
                    "timestamp": 3000.1,
                    "data": {"node_id": "n1", "attempt_id": "a1", "model_id": "gpt-4o"},
                },
                {
                    "seq": 2,
                    "type": "MODULE_INVOKE_COMPLETED",
                    "timestamp": 3001.0,
                    "data": {
                        "node_id": "n1",
                        "attempt_id": "a1",
                        "model_id": "gpt-4o",
                        "payload": {
                            "usage": {"prompt_tokens": 10000, "completion_tokens": 2000},
                        },
                    },
                },
                # 2. Partial/Estimated: only prompt tokens reported (5,000) on gpt-4o-mini
                # input_cost = 5,000 * 0.15 / 1,000,000 = $0.00075000 + fallback $0.01 = $0.01075000
                {
                    "seq": 3,
                    "type": "MODULE_INVOKE_STARTED",
                    "timestamp": 3001.1,
                    "data": {"node_id": "n2", "attempt_id": "a2", "model_id": "gpt-4o-mini"},
                },
                {
                    "seq": 4,
                    "type": "MODULE_INVOKE_COMPLETED",
                    "timestamp": 3002.0,
                    "data": {
                        "node_id": "n2",
                        "attempt_id": "a2",
                        "model_id": "gpt-4o-mini",
                        "payload": {"usage": {"prompt_tokens": 5000}},
                    },
                },
                # 3. Uncertain: failed without reporting any tokens
                # fallback applied: $0.005
                {
                    "seq": 5,
                    "type": "MODULE_INVOKE_STARTED",
                    "timestamp": 3002.1,
                    "data": {"node_id": "n3", "attempt_id": "a3", "model_id": "gpt-4o"},
                },
                {
                    "seq": 6,
                    "type": "MODULE_INVOKE_FAILED",
                    "timestamp": 3003.0,
                    "data": {
                        "node_id": "n3",
                        "attempt_id": "a3",
                        "model_id": "gpt-4o",
                        "payload": {"error": "Timeout", "usage": None},
                    },
                },
            ],
        }

        diag = describe_run(run_data, pricing_table=operator_pricing)
        u = diag["usage"]

        assert u["known_input_tokens"] == 15000  # 10000 + 5000
        assert u["known_output_tokens"] == 2000
        assert u["reported_attempts"] == 2
        assert u["unreported_attempts"] == 1
        assert u["complete"] is False

        # Verify separation of cost classes
        assert Decimal(u["reported_cost_usd"]) == Decimal("0.06000000")
        assert Decimal(u["estimated_cost_usd"]) == Decimal("0.01075000")
        assert Decimal(u["uncertain_cost_usd"]) == Decimal("0.00500000")
        assert Decimal(u["calculated_cost_usd"]) == (
            Decimal("0.06000000") + Decimal("0.01075000") + Decimal("0.00500000")
        )

        assert u["is_provider_invoice"] is False
        assert "not represent reconciled provider invoices" in u["disclaimer"]
        assert u["cost_status"] == "partial"
        assert "tracked as uncertain, not free" in u["incomplete_coverage_explanation"]

    def test_cache_categories_and_incomplete_coverage_explanation(
        self, operator_pricing: PricingTable
    ) -> None:
        run_data = {
            "run_id": "run-cache-404",
            "status": "completed",
            "created_at": 4000.0,
            "updated_at": 4002.0,
            "events": [
                {
                    "seq": 1,
                    "type": "MODULE_INVOKE_STARTED",
                    "timestamp": 4000.1,
                    "data": {"node_id": "cached_node", "attempt_id": "a1", "model_id": "gpt-4o"},
                },
                {
                    "seq": 2,
                    "type": "MODULE_INVOKE_COMPLETED",
                    "timestamp": 4001.0,
                    "data": {
                        "node_id": "cached_node",
                        "attempt_id": "a1",
                        "model_id": "gpt-4o",
                        "payload": {
                            "usage": {
                                "prompt_tokens": 10000,
                                "cached_prompt_tokens": 8000,
                                "completion_tokens": 1000,
                            },
                        },
                    },
                },
            ],
        }

        diag = describe_run(run_data, pricing_table=operator_pricing)
        u = diag["usage"]

        # Cached prompt tokens breakdown: 8,000 cached, 2,000 uncached
        assert u["cached_input_tokens"] == 8000
        assert u["known_input_tokens"] == 10000
        # Cost check:
        # 2,000 uncached * $3.00 / 1M = $0.00600000
        # 8,000 cached * $0.75 / 1M   = $0.00600000
        # 1,000 output * $15.00 / 1M  = $0.01500000
        # Total = $0.02700000
        assert Decimal(u["calculated_cost_usd"]) == Decimal("0.02700000")
        assert u["cost_status"] == "reported"


# ============================================================================
# 3. Successful-Task Cost and Cohort Accounting
# ============================================================================


class TestSuccessfulTaskCostAndCohortMetrics:
    """Deliverable 6 & Required Negatives.

    Successful-task cost definition & zero-success boundary.
    """

    def test_successful_task_cost_divided_by_successes(
        self, operator_pricing: PricingTable
    ) -> None:
        """Cost of all cohort attempts divided by successes."""
        run_data = {
            "run_id": "run-cohort-505",
            "status": "completed",
            "created_at": 5000.0,
            "updated_at": 5005.0,
            "events": [
                # Attempt 1: Failed ($0.03 cost)
                {
                    "seq": 1,
                    "type": "MODULE_INVOKE_STARTED",
                    "timestamp": 5000.1,
                    "data": {"node_id": "call_1", "attempt_id": "att-1", "model_id": "gpt-4o"},
                },
                {
                    "seq": 2,
                    "type": "MODULE_INVOKE_FAILED",
                    "timestamp": 5001.0,
                    "data": {
                        "node_id": "call_1",
                        "attempt_id": "att-1",
                        "model_id": "gpt-4o",
                        "payload": {
                            "usage": {"prompt_tokens": 10000, "completion_tokens": 0},
                            "error": "Temporary network drop",
                        },
                    },
                },
                # Attempt 2: Retry Succeeded ($0.045 cost)
                {
                    "seq": 3,
                    "type": "MODULE_INVOKE_STARTED",
                    "timestamp": 5001.1,
                    "data": {"node_id": "call_1", "attempt_id": "att-2", "model_id": "gpt-4o"},
                },
                {
                    "seq": 4,
                    "type": "MODULE_INVOKE_COMPLETED",
                    "timestamp": 5002.5,
                    "data": {
                        "node_id": "call_1",
                        "attempt_id": "att-2",
                        "model_id": "gpt-4o",
                        "payload": {
                            "usage": {"prompt_tokens": 10000, "completion_tokens": 1000},
                        },
                    },
                },
            ],
        }

        diag = describe_run(run_data, pricing_table=operator_pricing)
        u = diag["usage"]

        # Attempt 1 cost = 10,000 * 3.00 / 1M = $0.03000000
        # Attempt 2 cost = 10,000 * 3.00 / 1M + 1,000 * 15.00 / 1M = $0.04500000
        # Total cohort cost = $0.07500000
        # Total successes = 1
        # successful_task_cost = $0.07500000 / 1 = $0.07500000
        assert u["cohort_attempt_count"] == 2
        assert u["task_success_count"] == 1
        assert Decimal(u["calculated_cost_usd"]) == Decimal("0.07500000")
        assert Decimal(u["successful_task_cost_usd"]) == Decimal("0.07500000")
        assert u["successful_task_cost_status"] == "defined"

    def test_zero_successes_is_undefined_not_zero_cost(
        self, operator_pricing: PricingTable
    ) -> None:
        """When 0 attempts succeed, successful_task_cost_usd MUST be None, not $0.00."""
        run_data = {
            "run_id": "run-fail-606",
            "status": "failed",
            "created_at": 6000.0,
            "updated_at": 6003.0,
            "events": [
                {
                    "seq": 1,
                    "type": "MODULE_INVOKE_STARTED",
                    "timestamp": 6000.1,
                    "data": {"node_id": "step_a", "attempt_id": "a1", "model_id": "gpt-4o"},
                },
                {
                    "seq": 2,
                    "type": "MODULE_INVOKE_FAILED",
                    "timestamp": 6001.0,
                    "data": {
                        "node_id": "step_a",
                        "attempt_id": "a1",
                        "model_id": "gpt-4o",
                        "payload": {
                            "usage": {"prompt_tokens": 5000, "completion_tokens": 0},
                            "error": "Fatal provider crash",
                        },
                    },
                },
            ],
        }

        diag = describe_run(run_data, pricing_table=operator_pricing)
        u = diag["usage"]

        assert u["task_success_count"] == 0
        assert u["cohort_attempt_count"] == 1
        assert u["successful_task_cost_usd"] is None
        assert u["successful_task_cost_status"] == "undefined_zero_successes"
        # Total cohort cost is nonzero ($0.015), proving failures incur cost
        # while success cost is undefined
        assert Decimal(u["calculated_cost_usd"]) == Decimal("0.01500000")

    def test_missing_pricing_retains_explicit_unknown_coverage(self) -> None:
        """Missing pricing does not display zero-cost success."""
        run_data = {
            "run_id": "run-unpriced-707",
            "status": "completed",
            "created_at": 7000.0,
            "updated_at": 7002.0,
            "events": [
                {
                    "seq": 1,
                    "type": "MODULE_INVOKE_STARTED",
                    "timestamp": 7000.1,
                    "data": {
                        "node_id": "n1",
                        "attempt_id": "a1",
                        "model_id": "custom-unpriced-llm",
                    },
                },
                {
                    "seq": 2,
                    "type": "MODULE_INVOKE_COMPLETED",
                    "timestamp": 7001.0,
                    "data": {
                        "node_id": "n1",
                        "attempt_id": "a1",
                        "model_id": "custom-unpriced-llm",
                        "payload": {"usage": {"prompt_tokens": 1000, "completion_tokens": 200}},
                    },
                },
            ],
        }

        # PricingTable with NO pricing for custom-unpriced-llm
        empty_pt = PricingTable(version="v1", date="2026-10-01")
        diag = describe_run(run_data, pricing_table=empty_pt)
        u = diag["usage"]

        assert u["cost_status"] == "not_reported"
        assert u["calculated_cost_usd"] is None
        assert u["successful_task_cost_usd"] is None
        assert u["successful_task_cost_status"] == "unpriced"


# ============================================================================
# 4. Redacted Context Capture & Configured Retention
# ============================================================================


class TestContextCaptureRedactionAndRetention:
    """Deliverable 4: Opt-in context capture, bounded payloads, and retention enforcement."""

    def test_redacted_context_capture_depth_and_bounds(self) -> None:
        sensitive_payload = {
            "api_key": "sk-secret-12345",
            "password": "super_secret_password",
            "auth_token": "token-9988",
            "cookie": "session_id=xyz",
            "public_key": "user_id_alpha",
            "deep": {"nested": {"token": "secret_inner"}},
            "long_text": "a" * 10000,
        }

        redacted = redact_run_content(sensitive_payload)
        assert redacted["api_key"] == "[REDACTED]"
        assert redacted["password"] == "[REDACTED]"
        assert redacted["auth_token"] == "[REDACTED]"
        assert redacted["cookie"] == "[REDACTED]"
        assert redacted["public_key"] == "user_id_alpha"
        assert redacted["deep"]["nested"]["token"] == "[REDACTED]"
        assert len(redacted["long_text"]) <= 8020
        assert "[TEXT TRUNCATED]" in redacted["long_text"]

    def test_configured_retention_purges_content(self) -> None:
        # Run created 300 seconds ago, retention configured to 60 seconds
        past_time = time.time() - 300.0
        run_data = {
            "run_id": "run-retention-808",
            "status": "completed",
            "created_at": past_time,
            "updated_at": past_time + 10.0,
            "events": [
                {
                    "seq": 1,
                    "type": "NODE_STARTED",
                    "timestamp": past_time + 1.0,
                    "data": {
                        "node_id": "secret_step",
                        "input_state": {"secret_data": "top_secret"},
                    },
                },
                {
                    "seq": 2,
                    "type": "NODE_COMPLETED",
                    "timestamp": past_time + 2.0,
                    "data": {"node_id": "secret_step", "state_update": {"out": "classified"}},
                },
            ],
        }

        diag = describe_run(
            run_data,
            include_content=True,
            retention_seconds=60.0,
        )

        assert diag["content_retention_status"] == "purged"
        assert diag["attempts"][0]["input"] == "[CONTENT PURGED BY RETENTION POLICY]"
        assert diag["attempts"][0]["output"] == "[CONTENT PURGED BY RETENTION POLICY]"

    def test_content_hidden_by_default(self) -> None:
        run_data = {
            "run_id": "run-hidden-909",
            "status": "completed",
            "created_at": time.time(),
            "events": [
                {
                    "seq": 1,
                    "type": "NODE_STARTED",
                    "timestamp": time.time(),
                    "data": {"node_id": "n1", "input_state": {"user_prompt": "hello"}},
                },
            ],
        }

        diag = describe_run(run_data, include_content=False)
        assert diag["content_included"] is False
        assert diag["attempts"][0]["input"] is None


# ============================================================================
# 5. Required Negative Cases & Workspace Scoping
# ============================================================================


class TestRequiredNegativesAndWorkspaceScoping:
    """Required Negatives: Repeated events, duplicate settlements, cross-workspace security."""

    def test_repeated_and_reordered_events_deduplicated(self) -> None:
        run_data = {
            "run_id": "run-dedup-1010",
            "status": "completed",
            "created_at": 1000.0,
            "events": [
                {
                    "seq": 1,
                    "type": "MODULE_INVOKE_STARTED",
                    "timestamp": 1000.1,
                    "data": {"node_id": "n1", "attempt_id": "a1"},
                },
                # Duplicate seq 1 replayed
                {
                    "seq": 1,
                    "type": "MODULE_INVOKE_STARTED",
                    "timestamp": 1000.1,
                    "data": {"node_id": "n1", "attempt_id": "a1"},
                },
                {
                    "seq": 2,
                    "type": "MODULE_INVOKE_COMPLETED",
                    "timestamp": 1001.0,
                    "data": {
                        "node_id": "n1",
                        "attempt_id": "a1",
                        "payload": {"usage": {"prompt_tokens": 100, "completion_tokens": 50}},
                    },
                },
                # Duplicate seq 2 replayed
                {
                    "seq": 2,
                    "type": "MODULE_INVOKE_COMPLETED",
                    "timestamp": 1001.0,
                    "data": {
                        "node_id": "n1",
                        "attempt_id": "a1",
                        "payload": {"usage": {"prompt_tokens": 100, "completion_tokens": 50}},
                    },
                },
            ],
        }

        diag = describe_run(run_data)
        assert diag["attempt_count"] == 1
        assert diag["usage"]["known_input_tokens"] == 100
        assert diag["usage"]["known_output_tokens"] == 50

    def test_duplicate_settlements_do_not_inflate_ledger(self, tmp_path: Path) -> None:
        ledger = UsageLedger(db_path=str(tmp_path / "ledger_dedup.sqlite3"))
        ledger.record_commit(
            workspace_id="ws_main",
            run_id="run-set-1",
            amount_usd=Decimal("0.50"),
            tokens=1000,
            provider_call_id="call_dup_123",
        )

        run_data = {
            "run_id": "run-set-1",
            "status": "completed",
            "created_at": 1000.0,
            "events": [
                {
                    "seq": 1,
                    "type": "MODULE_INVOKE_STARTED",
                    "timestamp": 1000.1,
                    "data": {
                        "node_id": "n1",
                        "attempt_id": "a1",
                        "provider_call_id": "call_dup_123",
                    },
                },
                {
                    "seq": 2,
                    "type": "MODULE_INVOKE_COMPLETED",
                    "timestamp": 1001.0,
                    "data": {
                        "node_id": "n1",
                        "attempt_id": "a1",
                        "provider_call_id": "call_dup_123",
                        "payload": {"usage": {"prompt_tokens": 800, "completion_tokens": 200}},
                    },
                },
            ],
        }

        diag = describe_run(run_data, ledger=ledger)
        assert Decimal(diag["usage"]["calculated_cost_usd"]) == Decimal("0.50000000")
        assert diag["attempts"][0]["provider_call_id"] == "call_dup_123"

    @pytest.mark.asyncio
    async def test_foreign_workspace_diagnostics_denied(self, test_app: FastAPI) -> None:
        store: StudioStore = test_app.state.store
        ws_repo = test_app.state.workspace_repo

        run_id = "run-corp-alpha-001"
        spec = WorkflowSpec(
            name="wf-1",
            entry_node="n1",
            nodes={"n1": WorkflowNode(id="n1", handler="dummy")},
            terminal_nodes=["n1"],
        )
        await store.record_run_start(run_id=run_id, workflow_id="wf-1", spec=spec, initial_state={})
        ws_repo.record_resource_ownership("run", run_id, "workspace_alpha", "admin_alpha")

        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Querying under workspace_beta MUST be rejected with 403 RUN_NOT_FOUND
            res = await client.get(
                f"/api/v1/runs/{run_id}/diagnostics",
                headers={"X-Workspace-ID": "workspace_beta"},
            )
            assert res.status_code == 403
            assert "CROSS_WORKSPACE_ACCESS_DENIED" in res.text


# ============================================================================
# 6. End-to-End Mixed-Outcome Run & Authoritative API Projection
# ============================================================================


class TestEndToEndMixedOutcomeRunAndClientConsistency:
    """Deliverable 5: Authoritative SDK projection for API and client consistency."""

    @pytest.mark.asyncio
    async def test_ledger_backed_diagnostics_via_api(
        self, test_app: FastAPI, operator_pricing: PricingTable
    ) -> None:
        store: StudioStore = test_app.state.store
        ws_repo = test_app.state.workspace_repo
        ledger: UsageLedger = UsageLedger(db_path=str(store.db_path))
        test_app.state.usage_ledger = ledger

        run_id = "run-e2e-mixed-99"
        ws_id = "default"
        spec = WorkflowSpec(
            name="wf_payout",
            entry_node="payout_step",
            nodes={"payout_step": WorkflowNode(id="payout_step", handler="dummy")},
            terminal_nodes=["payout_step"],
        )
        await store.record_run_start(
            run_id=run_id, workflow_id="wf_payout", spec=spec, initial_state={}
        )
        ws_repo.record_resource_ownership("run", run_id, ws_id, "local-owner")

        # Emit events for Attempt 1: Failed on gpt-4o
        await store.record_run_event(
            run_id=run_id,
            event_type="MODULE_INVOKE_STARTED",
            node_id="payout_step",
            data={
                "module_id": "nexura.models.openai",
                "attempt_id": "att_1",
                "model_id": "gpt-4o",
                "provider_call_id": "call_payout_1",
            },
        )
        await store.record_run_event(
            run_id=run_id,
            event_type="MODULE_INVOKE_FAILED",
            node_id="payout_step",
            data={
                "module_id": "nexura.models.openai",
                "attempt_id": "att_1",
                "model_id": "gpt-4o",
                "provider_call_id": "call_payout_1",
                "payload": {
                    "usage": {"prompt_tokens": 5000, "completion_tokens": 0},
                    "error": "Gateway timeout",
                },
            },
        )
        ledger.record_commit(
            workspace_id=ws_id,
            run_id=run_id,
            amount_usd=Decimal("0.015"),
            tokens=5000,
            usage_state="reported",
            provider_call_id="call_payout_1",
            model_id="gpt-4o",
        )

        # Emit events for Attempt 2: Succeeded on gpt-4o
        await store.record_run_event(
            run_id=run_id,
            event_type="MODULE_INVOKE_STARTED",
            node_id="payout_step",
            data={
                "module_id": "nexura.models.openai",
                "attempt_id": "att_2",
                "model_id": "gpt-4o",
                "provider_call_id": "call_payout_2",
            },
        )
        await store.record_run_event(
            run_id=run_id,
            event_type="MODULE_INVOKE_COMPLETED",
            node_id="payout_step",
            data={
                "module_id": "nexura.models.openai",
                "attempt_id": "att_2",
                "model_id": "gpt-4o",
                "provider_call_id": "call_payout_2",
                "payload": {
                    "usage": {"prompt_tokens": 5000, "completion_tokens": 1000},
                    "outputs": {"status": "paid", "secret_key": "top_secret_key"},
                },
            },
        )
        # Attempt 2 cost = 5,000 * 3.00 / 1M + 1,000 * 15.00 / 1M = 0.015 + 0.015 = 0.030
        ledger.record_commit(
            workspace_id=ws_id,
            run_id=run_id,
            amount_usd=Decimal("0.030"),
            tokens=6000,
            usage_state="reported",
            provider_call_id="call_payout_2",
            model_id="gpt-4o",
        )

        await store.update_run(
            run_id=run_id,
            status="completed",
            current_node_id="payout_step",
            current_state={"status": "paid"},
            step_count=2,
        )

        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            res = await client.get(
                f"/api/v1/runs/{run_id}/diagnostics",
                params={"include_content": "true"},
                headers={"X-Workspace-ID": ws_id},
            )
            assert res.status_code == 200
            data = res.json()

            assert data["run_id"] == run_id
            assert data["status"] == "completed"
            assert data["content_included"] is True

            usage = data["usage"]
            assert usage["known_input_tokens"] == 10000
            assert usage["known_output_tokens"] == 1000
            assert usage["cohort_attempt_count"] == 2
            assert usage["task_success_count"] == 1
            # Total cohort cost = 0.015 + 0.030 = 0.045
            assert Decimal(usage["calculated_cost_usd"]) == Decimal("0.04500000")
            # Successful task cost = 0.045 / 1 = 0.045
            assert Decimal(usage["successful_task_cost_usd"]) == Decimal("0.04500000")
            assert usage["successful_task_cost_status"] == "defined"

            # Check that secret_key in output was redacted
            att_success = data["attempts"][1]
            assert att_success["output"]["secret_key"] == "[REDACTED]"
            assert att_success["provider_call_id"] == "call_payout_2"
