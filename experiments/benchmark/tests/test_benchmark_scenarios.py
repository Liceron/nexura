# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Pytest suite executing and asserting benchmark scenario invariants."""

from __future__ import annotations

import pytest
from adapters.langgraph_adapter import LangGraphAdapter
from adapters.nexura_adapter import NexuraAdapter
from adapters.pydantic_ai_adapter import PydanticAIAdapter


@pytest.mark.asyncio
async def test_nexura_s1_unauthorized_side_effect() -> None:
    """Nexura S1: Write tool marked requires_approval=True halts without unauthorized execution."""
    adapter = NexuraAdapter()
    res = await adapter.run_scenario_s1_unauthorized_side_effect()
    assert res.supported == "Yes"
    assert res.measured_numbers["unauthorized_writes"] == 0
    assert res.measured_numbers["approved_resumed_writes"] == 1
    assert res.measured_numbers["paused_at_gate"] is True


@pytest.mark.asyncio
async def test_nexura_s2a_crash_inside_tool() -> None:
    """Nexura S2a: In-flight crash inside tool halts on recovery as outcome_unknown_after_crash."""
    adapter = NexuraAdapter()
    res = await adapter.run_scenario_s2a_crash_inside_tool()
    assert res.supported == "Yes"
    assert res.measured_numbers["writes_before_crash"] == 1
    assert res.measured_numbers["total_writes_after_recovery"] == 1
    assert res.measured_numbers["duplicate_writes"] == 0
    assert res.measured_numbers["recovered_status"] == "paused"
    assert res.measured_numbers["interruption_reason"] == "outcome_unknown_after_crash"


@pytest.mark.asyncio
async def test_nexura_s2b_crash_after_tool_returned() -> None:
    """Nexura S2b: Crash after tool returns reuses checkpointed output without duplicate writes."""
    adapter = NexuraAdapter()
    res = await adapter.run_scenario_s2b_crash_after_tool_returned()
    assert res.supported == "Yes"
    assert res.measured_numbers["writes_before_crash"] == 1
    assert res.measured_numbers["total_writes_after_recovery"] == 1
    assert res.measured_numbers["duplicate_writes"] == 0
    assert res.measured_numbers["recovered_status"] == "completed"


@pytest.mark.asyncio
async def test_nexura_s3_parallel_spending() -> None:
    """Nexura S3: Atomic reservation strictly caps parallel model calls to allowance K=3."""
    adapter = NexuraAdapter()
    res = await adapter.run_scenario_s3_parallel_spending()
    assert res.supported == "Yes (shared allowance)"
    assert res.measured_numbers["allowance_calls"] == 3
    assert res.measured_numbers["parallel_agents"] == 10
    assert res.measured_numbers["admitted_calls"] == 3
    assert res.measured_numbers["admitted_above_allowance"] == 0
    assert res.measured_numbers["successful_runs"] == 3


@pytest.mark.asyncio
async def test_pydantic_ai_scenarios() -> None:
    """Verify Pydantic AI benchmark measurements."""
    adapter = PydanticAIAdapter()

    res_s1 = await adapter.run_scenario_s1_unauthorized_side_effect()
    assert res_s1.supported == "Yes"
    assert res_s1.measured_numbers["unauthorized_writes"] == 0
    assert res_s1.measured_numbers["approved_resumed_writes"] == 1

    res_s2a = await adapter.run_scenario_s2a_crash_inside_tool()
    assert res_s2a.supported == "No"

    res_s2b = await adapter.run_scenario_s2b_crash_after_tool_returned()
    assert res_s2b.supported == "No"

    res_s3 = await adapter.run_scenario_s3_parallel_spending()
    assert res_s3.supported == "Partial (shared counter, not concurrent admission)"
    assert res_s3.measured_numbers["admitted_calls"] == 10
    assert res_s3.measured_numbers["admitted_above_allowance"] == 7


@pytest.mark.asyncio
async def test_langgraph_scenarios() -> None:
    """Verify LangGraph benchmark measurements."""
    adapter = LangGraphAdapter()

    res_s1 = await adapter.run_scenario_s1_unauthorized_side_effect()
    assert res_s1.supported == "Yes"
    assert res_s1.measured_numbers["unauthorized_writes"] == 0
    assert res_s1.measured_numbers["approved_resumed_writes"] == 1

    # S2a: Crash in tool replays tool on recovery (1 duplicate write)
    res_s2a = await adapter.run_scenario_s2a_crash_inside_tool()
    assert res_s2a.supported == "Yes"
    assert res_s2a.measured_numbers["duplicate_writes"] == 1

    # S2b: Crash after tool return commits tool superstep, so no duplicate on recovery
    res_s2b = await adapter.run_scenario_s2b_crash_after_tool_returned()
    assert res_s2b.supported == "Yes"
    assert res_s2b.measured_numbers["duplicate_writes"] == 0

    # S3: ModelCallLimitMiddleware limits per thread/run, admitting all 10 parallel calls
    res_s3 = await adapter.run_scenario_s3_parallel_spending()
    assert res_s3.supported == "Partial (per-thread/run limit only)"
    assert res_s3.measured_numbers["admitted_calls"] == 10
    assert res_s3.measured_numbers["admitted_above_allowance"] == 7
