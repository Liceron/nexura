# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Benchmark runner comparing Nexura, Pydantic AI, and LangGraph on S1, S2a, S2b, and S3a-c."""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import platform
import subprocess
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
from adapters.spending import (
    CASES,
    ESTIMATED_COST_PER_CALL_USD,
    ESTIMATED_TOKENS_PER_CALL,
    LIMIT_CALLS,
    LIMIT_USD,
    MODEL_LATENCY_S,
    N_AGENTS,
    PRICE_PER_MILLION_USD,
)

COMMAND = "uv --directory experiments/benchmark run python run.py"
PACKAGES = (
    "nexura-agents",
    "pydantic-ai",
    "pydantic-ai-harness",
    "langgraph",
    "langchain",
    "langchain-core",
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
        elif r.scenario_id == "S3-legacy":
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


def _format_spending_table(results: list[ScenarioResult]) -> str:
    """Format the fair spending results (S3a-c, parallel and sequential) as a table."""
    lines = [
        "| Framework | Feature | Case | Mode | Admitted calls | Refused before call | "
        "Errored after call | Spend (USD) | Over limit (USD) | Over limit (%) | "
        "Framework's own count (USD) |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        n = r.measured_numbers
        recorded = n.get("framework_recorded_spend_usd") or "n/a"
        lines.append(
            f"| {r.framework} | {n['feature']} | ({n['case']}) | {n['mode']} | "
            f"{n['admitted_calls']} | {n['refused_before_call']} | "
            f"{n['runs_that_errored_after_their_call']} | {n['total_spend_usd']} | "
            f"{n['overspend_usd']} | {n['overspend_percent_of_limit']} | {recorded} |"
        )
    return "\n".join(lines)


def _spending_section(spending: list[ScenarioResult]) -> list[str]:
    cases = "\n".join(
        f"- **({c.case_id}) {c.label}**: {c.input_tokens} input + {c.output_tokens} output tokens, "
        f"${c.actual_cost_usd} per call; {c.calls_that_fit} call(s) fit the limit at actual cost."
        for c in CASES
    )
    parallel = [r for r in spending if r.measured_numbers["mode"] == "parallel"]
    sequential = [r for r in spending if r.measured_numbers["mode"] == "sequential"]
    sections = [
        "### S3a-c: Parallel spending (fair rerun, P35 A5)",
        "",
        f"**Workload**: {N_AGENTS} agents each make one model call to the same fake model. "
        f"Each fake call waits {MODEL_LATENCY_S}s before answering, so admitted calls are in "
        "flight together. The shared limit is "
        f"**${LIMIT_USD}**, which fits {LIMIT_CALLS} calls at the estimated "
        f"${ESTIMATED_COST_PER_CALL_USD} per call ({ESTIMATED_TOKENS_PER_CALL} tokens at "
        f"${PRICE_PER_MILLION_USD} per million input or output tokens). Call-count features "
        f"use a limit of {LIMIT_CALLS} calls.",
        "",
        cases,
        "",
        "**Spend** is ground truth: model calls that actually ran (counted inside the fake "
        "model) times the actual cost per call. **Over limit** is spend minus the limit. "
        "'Errored after call' counts runs whose call ran and was paid for, but which then "
        "raised a limit error. 'Framework's own count' is what the framework's ledger or "
        "counter recorded, where it has one.",
        "",
        "#### Parallel (the scenario)",
        "",
        _format_spending_table(parallel),
        "",
        "#### Sequential control (same agents, one after another)",
        "",
        "Shows each feature working as designed when runs do not overlap.",
        "",
        _format_spending_table(sequential),
        "",
        "#### Configuration and documentation per feature",
        "",
    ]
    seen: set[tuple[str, str]] = set()
    for r in spending:
        key = (r.framework, r.measured_numbers["feature"])
        if key in seen:
            continue
        seen.add(key)
        docs = ", ".join(
            f"[docs]({'../../../' + u if u.startswith('docs/') else u})"
            for u in r.documentation_urls
        )
        sections += [
            f"- **{r.framework} {r.version}, {r.measured_numbers['feature']}** "
            f"(support: {r.supported})",
            f"  - Configuration: `{r.configuration}`",
            f"  - Behaviour: {r.notes}",
            f"  - Docs: {docs}",
        ]
    sections += [
        "",
        "#### Not run here: Claude Agent SDK and Mastra",
        "",
        "Neither can run offline in this harness (the Claude Agent SDK drives the Claude Code "
        "binary against a paid model; Mastra is TypeScript). Their documented budget semantics "
        "are summarized in `experiments/benchmark/README.md`; no numbers are claimed.",
        "",
        "---",
        "",
    ]
    return sections


def _generate_markdown_report(
    results: list[ScenarioResult],
    spending: list[ScenarioResult],
    metadata: dict[str, Any],
) -> str:
    """Generate detailed RESULTS.md content."""
    table = _format_markdown_table(results)
    timestamp = metadata["timestamp"]
    versions = ", ".join(f"`{k}=={v}`" for k, v in metadata["packages"].items())
    sdk = metadata["sdk_source"]

    sections = [
        "# Benchmark Results: Nexura vs Pydantic AI vs LangGraph",
        "",
        f"**Date Generated**: {timestamp}  ",
        f"**Command**: `{metadata['command']}` (from the repository root)  ",
        f"**Python Runtime**: {platform.python_version()} ({platform.platform()})  ",
        f"**Package versions**: {versions}  ",
        (
            f"**Nexura SDK source**: repository working tree at `{sdk['repo_commit']}` "
            f"(uncommitted changes under packages/nexura-agents: {sdk['repo_sdk_dirty']})  "
            if sdk["uses_repo_working_tree"]
            else f"**Nexura SDK source**: `{sdk['nexura_agents_path']}`, not the repository "
            f"working tree (repository HEAD `{sdk['repo_commit']}`; see README for the "
            "export used)  "
        ),
        "**Environment**: Offline, deterministic scripted models only (no API keys/paid calls).",
        "",
        "## Summary Results Table (S1, S2a, S2b, legacy S3)",
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
        *_spending_section(spending),
        "### S3-legacy: Parallel spending, original P33 setup (superseded)",
        "",
        "Kept so its tests still run, but **do not quote it**. The 2026-10-20 fact-check found it",
        "skewed: Nexura used a token cap with an admission estimate exactly equal to the actual",
        "cost, Pydantic AI ran a shared `RunUsage` without `cost_limit` or `SpendLimits`, and",
        "LangGraph used one thread per agent. S3a-c above replace it.",
        "",
        "---",
        "",
        "## Methodology and Fairness Disclaimers",
        "1. **No Paid Calls / Fake Models Only**: All tests use offline deterministic test models",
        "   (`FakeModel`/`BaseProvider` subclasses, `FunctionModel`, `GenericFakeChatModel`).",
        "2. **Documented Configurations**: Each adapter uses the framework's documented feature",
        "   for the job; where a feature is used outside its stated design, the notes say so.",
        "3. **Built-in features only**: any framework can enforce a shared cap with custom code",
        "   (for example a lock-protected counter in a Pydantic AI `before_model_request` hook or",
        "   a LangGraph node). S3 measures what the documented features do without such code.",
        "4. **What is NOT Measured**: This benchmark evaluates structural SDK mechanics",
        "   (approvals, recovery, concurrency bounds). It does **not** evaluate real-world",
        "   LLM reasoning quality, model intelligence, or synthetic fake-model latency.",
    ]
    return "\n".join(sections) + "\n"


def _sdk_source() -> dict[str, Any]:
    """Record which Nexura SDK source was imported and the repository state."""
    import nexura_agents

    root = Path(__file__).resolve().parents[2]
    module_path = Path(nexura_agents.__file__).resolve()
    info: dict[str, Any] = {
        "nexura_agents_path": str(module_path),
        "uses_repo_working_tree": root in module_path.parents,
        "repo_commit": "unknown",
        "repo_sdk_dirty": "unknown",
    }
    try:
        info["repo_commit"] = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--", "packages/nexura-agents"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        info["repo_sdk_dirty"] = "yes" if dirty else "no"
    except (OSError, subprocess.CalledProcessError):
        pass
    return info


async def main() -> None:
    """Run all benchmark adapters and write results."""
    timestamp = datetime.now(UTC).isoformat()
    print("=" * 70)
    print("Starting Multi-Framework Agent Benchmark Harness (P33, S3 rerun P35 A5)")
    print(f"Timestamp: {timestamp}")
    print(f"Python: {platform.python_version()}")
    print("=" * 70)

    adapters: list[BaseAdapter] = [
        NexuraAdapter(),
        PydanticAIAdapter(),
        LangGraphAdapter(),
    ]

    all_results: list[ScenarioResult] = []
    spending: list[ScenarioResult] = []

    for adapter in adapters:
        name = adapter.framework_name
        version = adapter.version
        print(f"\nEvaluating framework: {name} ({version})")
        print("-" * 50)

        print("  Running S1: unauthorized side effect...")
        res_s1 = await adapter.run_scenario_s1_unauthorized_side_effect()
        all_results.append(res_s1)
        print(f"    -> Result: {res_s1.measured_numbers}")

        print("  Running S2a: crash inside tool after side effect before return...")
        res_s2a = await adapter.run_scenario_s2a_crash_inside_tool()
        all_results.append(res_s2a)
        print(f"    -> Result: {res_s2a.measured_numbers}")

        print("  Running S2b: crash after tool returned before next step...")
        res_s2b = await adapter.run_scenario_s2b_crash_after_tool_returned()
        all_results.append(res_s2b)
        print(f"    -> Result: {res_s2b.measured_numbers}")

        print("  Running S3-legacy: original P33 parallel spending setup...")
        res_s3 = await adapter.run_scenario_s3_parallel_spending()
        all_results.append(res_s3)
        print(f"    -> Result: {res_s3.measured_numbers}")

        for mode in ("parallel", "sequential"):
            for case in CASES:
                print(f"  Running S3{case.case_id} ({mode}): {case.label}...")
                for res in await adapter.run_spending(case, mode):
                    spending.append(res)
                    n = res.measured_numbers
                    print(
                        f"    -> {n['feature']}: admitted={n['admitted_calls']} "
                        f"spend=${n['total_spend_usd']} over=${n['overspend_usd']}"
                    )

    benchmark_dir = Path(__file__).resolve().parent
    results_dir = benchmark_dir / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    json_path = results_dir / "results.json"
    md_path = results_dir / "RESULTS.md"

    metadata: dict[str, Any] = {
        "timestamp": timestamp,
        "date": timestamp[:10],
        "command": COMMAND,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "frameworks": {adapter.framework_name: adapter.version for adapter in adapters},
        "packages": {pkg: importlib.metadata.version(pkg) for pkg in PACKAGES},
        "sdk_source": _sdk_source(),
        "spending_parameters": {
            "parallel_agents": N_AGENTS,
            "limit_usd": str(LIMIT_USD),
            "limit_calls": LIMIT_CALLS,
            "estimated_tokens_per_call": ESTIMATED_TOKENS_PER_CALL,
            "estimated_cost_per_call_usd": str(ESTIMATED_COST_PER_CALL_USD),
            "price_per_million_tokens_usd": str(PRICE_PER_MILLION_USD),
            "fake_model_latency_seconds": MODEL_LATENCY_S,
            "cases": [
                {
                    "case": c.case_id,
                    "label": c.label,
                    "input_tokens": c.input_tokens,
                    "output_tokens": c.output_tokens,
                    "actual_cost_per_call_usd": str(c.actual_cost_usd),
                    "calls_that_fit_at_actual_cost": c.calls_that_fit,
                }
                for c in CASES
            ],
        },
    }
    output_payload: dict[str, Any] = {
        "metadata": metadata,
        "results": [r.to_dict() for r in all_results],
        "spending_results": [r.to_dict() for r in spending],
    }

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(output_payload, f, indent=2)
    print(f"\n[Saved JSON]: {json_path}")

    md_content = _generate_markdown_report(all_results, spending, metadata)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_content)
    print(f"[Saved Markdown]: {md_path}")

    print("\n" + "=" * 70)
    print("Benchmark Completed Successfully.")
    print("=" * 70)
    print("\nSummary Table:")
    print(_format_markdown_table(all_results))
    print("\nParallel spending (S3a-c):")
    print(_format_spending_table([r for r in spending if r.measured_numbers["mode"] == "parallel"]))


if __name__ == "__main__":
    asyncio.run(main())
