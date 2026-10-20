# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Shared definitions for the fair parallel-spending scenario (P35 A5).

Every framework sees the same workload: ``N_AGENTS`` agents each make one model call to the
same fake model, all calls in flight at the same time (each fake call waits
``MODEL_LATENCY_S`` before answering, like a network round trip). The shared limit is
``LIMIT_USD``, which fits ``LIMIT_CALLS`` calls at the *estimated* cost per call.

Three cases vary how the actual cost of a call relates to that estimate:

- (a) the actual cost equals the estimate;
- (b) the model replies longer than estimated, so each call costs twice the estimate;
- (c) the model replies shorter than estimated, so each call costs half the estimate.

Spend is ground truth: model calls that actually executed (counted inside the fake model)
times the actual cost per call. It does not depend on any framework's own accounting.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from .base import ScenarioResult

Mode = Literal["parallel", "sequential"]

FAKE_MODEL_ID = "bench-fake-model"
# One price for input and output tokens, so a reservation that assumes a 50/50 split
# is not wrong merely because of the split. USD per one million tokens.
PRICE_PER_MILLION_USD = Decimal("2.00")
ESTIMATED_TOKENS_PER_CALL = 5_000
N_AGENTS = 10
LIMIT_CALLS = 3
MODEL_LATENCY_S = 0.02


def tokens_cost_usd(tokens: int) -> Decimal:
    """Price a token count with the benchmark's fake price."""
    return Decimal(tokens) * PRICE_PER_MILLION_USD / Decimal(1_000_000)


ESTIMATED_COST_PER_CALL_USD = tokens_cost_usd(ESTIMATED_TOKENS_PER_CALL)  # 0.01
LIMIT_USD = ESTIMATED_COST_PER_CALL_USD * LIMIT_CALLS  # 0.03


@dataclass(frozen=True)
class SpendingCase:
    """One relation between estimated and actual cost per call."""

    case_id: str
    label: str
    input_tokens: int
    output_tokens: int

    @property
    def actual_tokens(self) -> int:
        """Tokens the fake model reports for one call."""
        return self.input_tokens + self.output_tokens

    @property
    def actual_cost_usd(self) -> Decimal:
        """Actual cost of one call."""
        return tokens_cost_usd(self.actual_tokens)

    @property
    def calls_that_fit(self) -> int:
        """Most calls whose actual cost stays within the limit."""
        return min(N_AGENTS, int(LIMIT_USD // self.actual_cost_usd))


CASES: tuple[SpendingCase, ...] = (
    SpendingCase("a", "estimate equals actual cost", 2_500, 2_500),
    SpendingCase("b", "estimate lower than actual (reply 2x the estimated cost)", 2_500, 7_500),
    SpendingCase("c", "estimate higher than actual (reply 0.5x the estimated cost)", 1_250, 1_250),
)


def _usd(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.0001'))}"


def make_spending_result(
    *,
    framework: str,
    version: str,
    feature: str,
    case: SpendingCase,
    mode: Mode,
    admitted_calls: int,
    refused_runs: int,
    errored_after_call_runs: int,
    supported: str,
    configuration: str,
    documentation_urls: list[str],
    notes: str,
    framework_recorded_spend_usd: Decimal | None = None,
    extra: dict[str, Any] | None = None,
) -> ScenarioResult:
    """Build a uniform result for one framework feature, case and mode."""
    spend = case.actual_cost_usd * admitted_calls
    overspend = max(Decimal(0), spend - LIMIT_USD)
    numbers: dict[str, Any] = {
        "feature": feature,
        "case": case.case_id,
        "case_label": case.label,
        "mode": mode,
        "parallel_agents": N_AGENTS,
        "limit_usd": _usd(LIMIT_USD),
        "limit_calls_at_estimate": LIMIT_CALLS,
        "estimated_cost_per_call_usd": _usd(ESTIMATED_COST_PER_CALL_USD),
        "actual_cost_per_call_usd": _usd(case.actual_cost_usd),
        "calls_that_fit_at_actual_cost": case.calls_that_fit,
        "admitted_calls": admitted_calls,
        "refused_before_call": refused_runs,
        "runs_that_errored_after_their_call": errored_after_call_runs,
        "total_spend_usd": _usd(spend),
        "overspend_usd": _usd(overspend),
        "overspend_percent_of_limit": int((overspend / LIMIT_USD * 100).to_integral_value()),
        "framework_recorded_spend_usd": (
            _usd(framework_recorded_spend_usd) if framework_recorded_spend_usd is not None else None
        ),
    }
    if extra:
        numbers.update(extra)
    scenario_id = f"S3{case.case_id}" + ("" if mode == "parallel" else "-seq")
    scenario_name = (
        f"parallel spending, {case.label}"
        if mode == "parallel"
        else f"sequential control, {case.label}"
    )
    return ScenarioResult(
        framework=framework,
        version=version,
        scenario_id=scenario_id,
        scenario_name=scenario_name,
        supported=supported,
        measured_numbers=numbers,
        configuration=configuration,
        documentation_urls=documentation_urls,
        notes=notes,
    )
