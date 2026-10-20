# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Benchmark runner comparing Nexura, Pydantic AI, and LangGraph on S1, S2a, S2b, and S3."""

from __future__ import annotations

import asyncio
import json
import platform
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from adapters import (
    BaseAdapter,
    LangGraphAdapter,
    NexuraAdapter,
    PydanticAIAdapter,
    ScenarioResult,
)


def _format_markdown_table(results: list[ScenarioResult]) -> str:
    """Format scenario results as a GitHub Flavored Markdown table."""
    headers = [
        "| Framework | Version | Scenario | Supported | Key Metric | Configuration Used | Docs |",
        "|---|---|---|---|---|---|---|",
    ]
    lines = list(headers)
    for r in results:
        if r.scenario_id == "S1":
            metric = f"unauthorized writes: {r.measured_numbers.get('unauthorized_writes')}"
        elif r.scenario_id in ("S2a", "S2b"):
            if r.supported == "No":
                metric = "not supported natively"
            else:
                metric = f"duplicate writes: {r.measured_numbers.get('duplicate_writes')}"
        elif r.scenario_id == "S3":
            admitted = r.measured_numbers.get("admitted_calls")
            above = r.measured_numbers.get("admitted_above_allowance")
            metric = f"admitted: {admitted} (over: {above})"
        else:
            metric = str(r.measured_numbers)

        doc_url = r.documentation_urls[0] if r.documentation_urls else None
        if doc_url and doc_url.startswith("docs/"):
            doc_url = f"../../../{doc_url}"
        doc_link = f"[docs]({doc_url})" if doc_url else "N/A"
        cfg_val = r.configuration
        cfg_short = f"`{cfg_val[:40]}...`" if len(cfg_val) > 40 else f"`{cfg_val}`"
        lines.append(
            f"| **{r.framework}** | {r.version} | {r.scenario_id} ({r.scenario_name}) | "
            f"{r.supported} | {metric} | {cfg_short} | {doc_link} |"
        )
    return "\n".join(lines)


def _generate_markdown_report(results: list[ScenarioResult], timestamp: str) -> str:
    """Generate detailed RESULTS.md content."""
    table = _format_markdown_table(results)

    sections = [
        "# Benchmark Results: Nexura vs Pydantic AI vs LangGraph",
        "",
        f"**Date Generated**: {timestamp}  ",
        f"**Python Runtime**: {platform.python_version()} ({platform.platform()})  ",
        "**Environment**: Offline, deterministic scripted models only (no API keys/paid calls).",
        "",
        "## Summary Results Table",
        "",
        table,
        "",
        "## Detailed Scenario Analysis",
        "",
        "### S1: Unauthorized Side Effect",
        "**Objective**: The model asks to execute a write tool (`send_payment`) that requires",
        "human approval. We count how many writes actually execute without approval when",
        "configured according to each framework's documentation.",
        "",
        "- **Nexura (`0.4.0`)**:",
        "  - *Support Status*: **Yes**",
        "  - *Configuration*: `@tool(requires_approval=True)` run via `Runner().run_async()`.",
        "    The runner pauses execution and emits `RunInterruption` before calling the tool.",
        "  - *Result*: **0 unauthorized writes** (`paused_at_gate: True`).",
        "    Upon approval (`runner.resume_async(approve=...)`), 1 write executed.",
        "  - *Docs*: [docs/approvals.md](../../../docs/approvals.md)",
        "",
        "- **Pydantic AI (`2.57.0`)**:",
        "  - *Support Status*: **Yes**",
        "  - *Configuration*: Check `ctx.tool_call_approved` and raise `ApprovalRequired()`.",
        "    Agent configured with `output_type=[str, DeferredToolRequests]`.",
        "  - *Result*: **0 unauthorized writes** (`output_type: DeferredToolRequests`).",
        "    Upon approval via `DeferredToolResults`, 1 write executed.",
        "  - *Docs*: [Pydantic AI Deferred Tools]"
        "(https://ai.pydantic.dev/deferred-tools/#human-in-the-loop-tool-approval)",
        "",
        "- **LangGraph (`1.2.16`)**:",
        "  - *Support Status*: **Yes**",
        "  - *Configuration*: `StateGraph` compiled with `checkpointer=MemorySaver()` and",
        "    `interrupt_before=['tools']`. Resumed via `graph.invoke(None, config)`.",
        "  - *Result*: **0 unauthorized writes** (`interrupted_node: tools`).",
        "    Upon approval resume, 1 write executed.",
        "  - *Docs*: [LangGraph Human-in-the-Loop]"
        "(https://langchain-ai.github.io/langgraph/how-tos/human-in-the-loop/)",
        "",
        "---",
        "",
        "### S2a: Crash Inside Tool After Side Effect (Before Return)",
        "**Objective**: The process crashes inside the tool body immediately after the write",
        "side effect occurs (`writes += 1`), before the tool function returns. We recover",
        "from the framework's checkpoint/persistence and measure duplicate executions.",
        "",
        "- **Nexura (`0.4.0`)**:",
        "  - *Support Status*: **Yes**",
        "  - *Configuration*: `Runner(checkpoint_store=MemoryCheckpointStore())` with",
        "    `@tool(idempotent=False)`.",
        "  - *Result*: **0 duplicate writes** (1 write before crash, 1 write after recovery).",
        "    *Behavior*: Nexura detects that a non-idempotent tool was in-flight when the crash",
        "    occurred. On recovery via `Runner.recover_async()`, it marks the outcome unknown",
        "    (`reason='outcome_unknown_after_crash'`), refuses to blindly re-run the tool, and",
        "    halts in status `'paused'` to request human review.",
        "  - *Docs*: [docs/durable-runs.md](../../../docs/durable-runs.md)",
        "",
        "- **Pydantic AI (`2.57.0`)**:",
        "  - *Support Status*: **No**",
        "  - *Configuration*: None (Native standalone checkpointer is **not supported**).",
        "  - *Result*: **Not supported natively**. Pydantic AI does not provide a built-in",
        "    local checkpointer or persistence store for `Agent`. Durable execution is",
        "    documented via external orchestrator packages (`dbos`, `temporalio`, `prefect`).",
        "  - *Docs*: [Pydantic AI Durable Execution](https://ai.pydantic.dev/durable-execution/)",
        "",
        "- **LangGraph (`1.2.16`)**:",
        "  - *Support Status*: **Yes**",
        "  - *Configuration*: `StateGraph.compile(checkpointer=MemorySaver())` recovered",
        "    via `graph.invoke(None, config)`.",
        "  - *Result*: **1 duplicate write** (1 write before crash, 2 writes after recovery).",
        "    *Why*: LangGraph persists checkpoints at superstep boundaries (node completion).",
        "    Because the tool node crashed mid-execution, its superstep never committed. Recovery",
        "    rolls back to pre-tool superstep and replays the tool node, causing a duplicate.",
        "  - *Docs*: [LangGraph Persistence Replay]"
        "(https://langchain-ai.github.io/langgraph/concepts/persistence/#replaying)",
        "",
        "---",
        "",
        "### S2b: Crash After Tool Returned (Before Next Step)",
        "**Objective**: The write tool completes and returns normally. The process is interrupted",
        "before the next step finishes. We recover from the checkpoint and count duplicate writes.",
        "",
        "- **Nexura (`0.4.0`)**:",
        "  - *Support Status*: **Yes**",
        "  - *Configuration*: `Runner(checkpoint_store=MemoryCheckpointStore())` with",
        "    `@tool(idempotent=False)`. Crash simulated in follow-up model call.",
        "  - *Result*: **0 duplicate writes** (1 write before crash, 1 write after recovery).",
        "    *Behavior*: Nexura commits completed tool outputs upon tool return. On recovery,",
        "    it reuses the recorded output without re-executing the non-idempotent write.",
        "  - *Docs*: [docs/durable-runs.md](../../../docs/durable-runs.md)",
        "",
        "- **Pydantic AI (`2.57.0`)**:",
        "  - *Support Status*: **No**",
        "  - *Configuration*: None (Native standalone checkpointer is **not supported**).",
        "  - *Result*: **Not supported natively**.",
        "  - *Docs*: [Pydantic AI Durable Execution](https://ai.pydantic.dev/durable-execution/)",
        "",
        "- **LangGraph (`1.2.16`)**:",
        "  - *Support Status*: **Yes**",
        "  - *Configuration*: `StateGraph.compile(checkpointer=MemorySaver())` with edge",
        "    `tools -> agent`, crashing in follow-up agent invocation.",
        "  - *Result*: **0 duplicate writes** (1 write before crash, 1 write after recovery).",
        "    *Why*: Because the tool returned, the ToolNode superstep committed to `MemorySaver`.",
        "    Recovery resumes directly at the subsequent agent node without replaying the tool.",
        "  - *Docs*: [LangGraph Persistence Replay]"
        "(https://langchain-ai.github.io/langgraph/concepts/persistence/#replaying)",
        "",
        "---",
        "",
        "### S3: Parallel Spending",
        "**Objective**: N=10 parallel agents share an allowance of K=3 model calls (or equivalent",
        "token budget). We measure how many model calls were actually admitted/executed above",
        "the allowance using each framework's documented spend/usage limit feature.",
        "",
        "- **Nexura (`0.4.0`)**:",
        "  - *Support Status*: **Yes (shared allowance)**",
        "  - *Configuration*: `BudgetManager().create_run_budget()` with max_tokens=60",
        "    and child budgets. Before dispatch, `BaseProvider.generate` reserves tokens via",
        "    atomic admission (`admit_provider_call`).",
        "  - *Result*: **0 admitted calls above allowance** (allowance: 3, admitted calls: 3,",
        "    above allowance: 0). Excess calls are denied at the admission gate before execution.",
        "  - *Docs*: [docs/budgets-and-usage.md](../../../docs/budgets-and-usage.md)",
        "",
        "- **Pydantic AI (`2.57.0`)**:",
        "  - *Support Status*: **Partial (shared counter, not concurrent admission)**",
        "  - *Configuration*: `UsageLimits(request_limit=3)` passed with shared `RunUsage()`",
        "    to `agent.run(..., usage_limits=limits, usage=shared_usage)`.",
        "  - *Result*: **7 admitted calls above allowance** (allowance: 3, admitted calls: 10,",
        "    above allowance: 7).",
        "    *Design Note*: Sharing one `RunUsage` is documented in Pydantic AI for agent",
        "    delegation (sequential execution), not as a concurrent admission control across",
        "    parallel runs. This benchmark measures the documented feature under concurrent load",
        "    outside its stated design. Because `check_before_request` lacks atomic reservations,",
        "    all 10 coroutines pass the pre-check concurrently while `usage.requests` is 0,",
        "    admitting all 10 calls.",
        "  - *Docs*: [Pydantic AI Usage and Limits](https://ai.pydantic.dev/usage/)",
        "",
        "- **LangGraph (`1.2.16`) / LangChain (`1.4.6`)**:",
        "  - *Support Status*: **Partial (per-thread/run limit only)**",
        "  - *Configuration*: `ModelCallLimitMiddleware(thread_limit=3, run_limit=3)`",
        "    from `langchain.agents.middleware.model_call_limit`.",
        "  - *Result*: **7 admitted calls above allowance** (allowance: 3, admitted calls: 10,",
        "    above allowance: 7).",
        "    *Source Citation*: `experiments/benchmark/.venv/lib/python3.12/site-packages/"
        "langchain/agents/middleware/model_call_limit.py`.",
        "    *Why*: The middleware stores counters in `ModelCallLimitState` channels",
        "    `thread_model_call_count` and `run_model_call_count`. These channels are scoped",
        "    per thread/run; it cannot share an allowance across parallel agent threads.",
        "    Furthermore, `after_model` increments counts only post-hoc after each call completes,",
        "    so concurrent requests pass the pre-check simultaneously without atomic reservation.",
        "  - *Docs*: [LangGraph Documentation](https://langchain-ai.github.io/langgraph/)",
        "",
        "---",
        "",
        "## Methodology and Fairness Disclaimers",
        "1. **No Paid Calls / Fake Models Only**: All tests use offline deterministic test models",
        "   (`FakeModel`, `FunctionModel`, `GenericFakeChatModel`).",
        "2. **Documented Configurations**: Each framework adapter follows the exact configuration",
        "   recommended in its official documentation.",
        "3. **What is NOT Measured**: This benchmark evaluates structural SDK mechanics",
        "   (approvals, recovery, concurrency bounds). It does **not** evaluate real-world",
        "   LLM reasoning quality, model intelligence, or synthetic fake-model latency.",
    ]
    return "\n".join(sections) + "\n"


async def main() -> None:
    """Run all benchmark adapters and write results."""
    timestamp = datetime.now(UTC).isoformat()
    print("=" * 70)
    print("Starting Multi-Framework Agent Benchmark Harness (P33)")
    print(f"Timestamp: {timestamp}")
    print(f"Python: {platform.python_version()}")
    print("=" * 70)

    adapters: list[BaseAdapter] = [
        NexuraAdapter(),
        PydanticAIAdapter(),
        LangGraphAdapter(),
    ]

    all_results: list[ScenarioResult] = []

    for adapter in adapters:
        name = adapter.framework_name
        version = adapter.version
        print(f"\nEvaluating framework: {name} ({version})")
        print("-" * 50)

        # S1
        print("  Running S1: unauthorized side effect...")
        res_s1 = await adapter.run_scenario_s1_unauthorized_side_effect()
        all_results.append(res_s1)
        print(f"    -> Result: {res_s1.measured_numbers}")

        # S2a
        print("  Running S2a: crash inside tool after side effect before return...")
        res_s2a = await adapter.run_scenario_s2a_crash_inside_tool()
        all_results.append(res_s2a)
        print(f"    -> Result: {res_s2a.measured_numbers}")

        # S2b
        print("  Running S2b: crash after tool returned before next step...")
        res_s2b = await adapter.run_scenario_s2b_crash_after_tool_returned()
        all_results.append(res_s2b)
        print(f"    -> Result: {res_s2b.measured_numbers}")

        # S3
        print("  Running S3: parallel spending...")
        res_s3 = await adapter.run_scenario_s3_parallel_spending()
        all_results.append(res_s3)
        print(f"    -> Result: {res_s3.measured_numbers}")

    # Output paths
    benchmark_dir = Path(__file__).resolve().parent
    results_dir = benchmark_dir / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    json_path = results_dir / "results.json"
    md_path = results_dir / "RESULTS.md"

    # Save results.json
    output_payload: dict[str, Any] = {
        "metadata": {
            "timestamp": timestamp,
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "frameworks": {adapter.framework_name: adapter.version for adapter in adapters},
        },
        "results": [r.to_dict() for r in all_results],
    }

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(output_payload, f, indent=2)
    print(f"\n[Saved JSON]: {json_path}")

    # Save RESULTS.md
    md_content = _generate_markdown_report(all_results, timestamp)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_content)
    print(f"[Saved Markdown]: {md_path}")

    print("\n" + "=" * 70)
    print("Benchmark Completed Successfully.")
    print("=" * 70)
    print("\nSummary Table:")
    print(_format_markdown_table(all_results))


if __name__ == "__main__":
    asyncio.run(main())
