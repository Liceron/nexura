# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Fair parallel-spending scenario (P35 A5): three estimate/actual cases, every framework."""

from __future__ import annotations

from decimal import Decimal

import pytest
from adapters.base import ScenarioResult
from adapters.langgraph_adapter import LangGraphAdapter
from adapters.nexura_adapter import NexuraAdapter
from adapters.pydantic_ai_adapter import PydanticAIAdapter
from adapters.spending import CASES, LIMIT_USD, N_AGENTS, SpendingCase

CASE = {c.case_id: c for c in CASES}


def _numbers(results: list[ScenarioResult], feature_prefix: str) -> dict:
    matches = [r for r in results if r.measured_numbers["feature"].startswith(feature_prefix)]
    assert len(matches) == 1, [r.measured_numbers["feature"] for r in results]
    return matches[0].measured_numbers


def _check_accounting(n: dict, case: SpendingCase) -> None:
    """Spend and overspend follow from admitted calls and the case's actual cost."""
    spend = case.actual_cost_usd * n["admitted_calls"]
    assert Decimal(n["total_spend_usd"]) == spend
    assert Decimal(n["overspend_usd"]) == max(Decimal(0), spend - LIMIT_USD)
    assert n["limit_usd"] == "0.0300"
    assert n["parallel_agents"] == N_AGENTS


def test_case_definitions() -> None:
    """The three cases are estimate == actual, estimate < actual and estimate > actual."""
    assert [c.case_id for c in CASES] == ["a", "b", "c"]
    assert CASE["a"].actual_cost_usd == Decimal("0.01")
    assert CASE["b"].actual_cost_usd == Decimal("0.02")
    assert CASE["c"].actual_cost_usd == Decimal("0.005")
    assert [c.calls_that_fit for c in CASES] == [3, 1, 6]
    assert Decimal("0.03") == LIMIT_USD


# (case, admitted, spend, overspend) for Nexura's shared USD budget, in parallel.
NEXURA_PARALLEL = [
    ("a", 3, "0.0300", "0.0000"),
    # Estimate too low: admission reserved 3 x $0.01, the calls cost 3 x $0.02.
    ("b", 3, "0.0600", "0.0300"),
    # Estimate too high: only 3 run although 6 would have fit.
    ("c", 3, "0.0150", "0.0000"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("case_id", "admitted", "spend", "over"), NEXURA_PARALLEL)
async def test_nexura_parallel(case_id: str, admitted: int, spend: str, over: str) -> None:
    """Nexura admits by estimate; it overspends when the estimate is too low."""
    case = CASE[case_id]
    (res,) = await NexuraAdapter().run_spending(case, "parallel")
    n = res.measured_numbers
    _check_accounting(n, case)
    assert n["admitted_calls"] == admitted
    assert n["refused_before_call"] == N_AGENTS - admitted
    assert n["total_spend_usd"] == spend
    assert n["overspend_usd"] == over
    # The SDK ledger agrees with ground truth and holds no leftover reservation.
    assert n["framework_recorded_spend_usd"] == spend
    assert Decimal(n["framework_reserved_after_run_usd"]) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(("case_id", "admitted"), [("a", 3), ("b", 2), ("c", 5)])
async def test_nexura_sequential_control(case_id: str, admitted: int) -> None:
    """Sequentially, settled actual cost is visible to the next admission."""
    case = CASE[case_id]
    (res,) = await NexuraAdapter().run_spending(case, "sequential")
    n = res.measured_numbers
    _check_accounting(n, case)
    assert n["admitted_calls"] == admitted


@pytest.mark.asyncio
@pytest.mark.parametrize("case_id", ["a", "b", "c"])
async def test_pydantic_ai_parallel(case_id: str) -> None:
    """UsageLimits and SpendLimits both check without reserving: all parallel calls run."""
    case = CASE[case_id]
    results = await PydanticAIAdapter().run_spending(case, "parallel")
    usage_limits = _numbers(results, "UsageLimits")
    spend_limits = _numbers(results, "SpendLimits")
    for n in (usage_limits, spend_limits):
        _check_accounting(n, case)
        assert n["admitted_calls"] == N_AGENTS
        assert n["refused_before_call"] == 0
        assert n["framework_recorded_spend_usd"] == n["total_spend_usd"]
    # cost_limit is checked after each response, so runs raise after paying for their call.
    # How many raise depends on how responses and checks interleave (8 to 10 observed), but
    # the last check always sees the full total, which is over the limit in every case.
    assert usage_limits["runs_that_errored_after_their_call"] >= 1
    assert spend_limits["runs_that_errored_after_their_call"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case_id", "usage_limits_admitted", "spend_limits_admitted"),
    [("a", 3, 3), ("b", 2, 2), ("c", 3, 6)],
)
async def test_pydantic_ai_sequential_control(
    case_id: str, usage_limits_admitted: int, spend_limits_admitted: int
) -> None:
    """Sequentially both features stop the run; case (c) UsageLimits stops at request_limit=3."""
    case = CASE[case_id]
    results = await PydanticAIAdapter().run_spending(case, "sequential")
    assert _numbers(results, "UsageLimits")["admitted_calls"] == usage_limits_admitted
    assert _numbers(results, "SpendLimits")["admitted_calls"] == spend_limits_admitted


@pytest.mark.asyncio
@pytest.mark.parametrize("case_id", ["a", "b", "c"])
async def test_langgraph_parallel(case_id: str) -> None:
    """No LangGraph/LangChain call or recursion limit bounds parallel calls across agents."""
    case = CASE[case_id]
    results = await LangGraphAdapter().run_spending(case, "parallel")
    assert len(results) == 3
    for prefix in (
        "ModelCallLimitMiddleware(thread_limit)",
        "ModelCallLimitMiddleware(run_limit)",
        "recursion_limit",
    ):
        n = _numbers(results, prefix)
        _check_accounting(n, case)
        assert n["admitted_calls"] == N_AGENTS


@pytest.mark.asyncio
@pytest.mark.parametrize("case_id", ["a", "b", "c"])
async def test_langgraph_sequential_control(case_id: str) -> None:
    """Sequentially, a shared-thread thread_limit holds at 3 calls whatever they cost."""
    case = CASE[case_id]
    results = await LangGraphAdapter().run_spending(case, "sequential")
    assert len(results) == 2  # the fan-out graph has no sequential form
    assert _numbers(results, "ModelCallLimitMiddleware(thread_limit)")["admitted_calls"] == 3
    assert _numbers(results, "ModelCallLimitMiddleware(run_limit)")["admitted_calls"] == N_AGENTS
