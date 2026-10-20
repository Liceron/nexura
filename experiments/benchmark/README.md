# Reproducible Multi-Framework Agent Benchmark Harness (Task P33)

This benchmark provides an honest, strictly reproducible comparison between **Nexura**,
**Pydantic AI**, and **LangGraph** across four agent runtime failure and concurrency scenarios:
1. **S1 "unauthorized side effect"**: write actions requiring human approval.
2. **S2a "crash inside tool after side effect, before return"**: process interruptions during tool
   execution (in-flight side effect) and subsequent recovery behavior.
3. **S2b "crash after tool returned, before next step"**: process interruptions after tool
   completion before the next step commits, and subsequent recovery behavior.
4. **S3 "parallel spending"**: $N=10$ concurrent agents competing for a shared spend/model-call
   allowance ($K=3$).

All benchmarks are 100% offline, deterministic, and self-contained, using each framework's
documented test model without paid calls, external networks, or API keys.

---

## What is Measured and What is NOT

### What is Measured
- **Approval Gating (S1)**: Does the framework halt before executing a write tool marked for
  approval? Count of unauthorized side effects executed without human sign-off.
- **In-Flight Crash Recovery (S2a)**: When a process crashes inside a write tool after its side
  effect occurs, does recovery re-run the side effect (causing a duplicate), or does it refuse
  to re-run blindly and request human review? Count of duplicate write executions.
- **Post-Return Crash Recovery (S2b)**: When a process crashes after a write tool returns but before
  the subsequent step commits, does recovery reuse the checkpointed tool result or replay the tool?
  Count of duplicate write executions.
- **Concurrent Allowance Enforcement (S3)**: When $N=10$ parallel agents compete for a shared
  allowance of $K=3$ model calls, does the framework's spend/budget feature enforce the limit
  atomically? Count of model calls admitted above the allowance.

### What is NOT Measured
- **Real-Model Quality**: This harness does not evaluate reasoning benchmarks (e.g. MMLU,
  SWE-bench) or model intelligence. All tests use canned test models.
- **Latency / Performance Claims**: Synthetic fake models execute near-instantaneously in memory.
  No latency claims are made.
- **Third-Party Orchestrator Setups**: Only native framework capabilities (or documented standard
  features) are benchmarked. For example, Pydantic AI's external server integrations
  (Temporal/DBOS/Prefect) are noted, but not run without native support.

---

## Environment & Pinned Versions

The benchmark harness runs in its own isolated virtual environment located in `experiments/benchmark/`:

| Framework | Pinned Version | Test Model Used | Documentation |
|---|---|---|---|
| **Nexura** | `0.4.0` (local path) | `FakeModel` / `BaseProvider` | `docs/` |
| **Pydantic AI** | `2.57.0` | `FunctionModel` | [Pydantic AI Docs](https://ai.pydantic.dev/) |
| **LangGraph** | `1.2.16` | `GenericFakeChatModel` (`langchain==1.4.6`, `langchain-core==1.6.12`) | [LangGraph Docs](https://langchain-ai.github.io/langgraph/) |

---

## How to Reproduce

From the project root:

### 1. Initialize and Sync Environment
```bash
uv --directory experiments/benchmark venv --python 3.12
uv --directory experiments/benchmark sync
```

### 2. Run the Benchmark
```bash
uv --directory experiments/benchmark run python run.py
```

Results are printed to stdout and saved to:
- `experiments/benchmark/results/results.json` (Machine-readable raw data)
- `experiments/benchmark/results/RESULTS.md` (Formatted markdown report)

### 3. Run the Verification Tests
```bash
uv --directory experiments/benchmark run pytest -q
```

### 4. Run Style and Formatting Checks
From the project root:
```bash
uv run --frozen ruff check experiments/benchmark
uv run --frozen ruff format --check experiments/benchmark
```

---

## Summary Results Table

| Framework | Version | Scenario | Supported | Key Metric | Configuration Used | Docs |
|---|---|---|---|---|---|---|
| **nexura** | 0.4.0 | S1 (unauthorized side effect) | Yes | unauthorized writes: 0 | `@tool(requires_approval=True) with Runne...` | [docs](docs/approvals.md) |
| **nexura** | 0.4.0 | S2a (crash inside tool after side effect before return) | Yes | duplicate writes: 0 | `Runner(checkpoint_store=MemoryCheckpoint...` | [docs](docs/durable-runs.md) |
| **nexura** | 0.4.0 | S2b (crash after tool returned before next step) | Yes | duplicate writes: 0 | `Runner(checkpoint_store=MemoryCheckpoint...` | [docs](docs/durable-runs.md) |
| **nexura** | 0.4.0 | S3 (parallel spending) | Yes (shared allowance) | admitted: 3 (over: 0) | `BudgetManager.create_run_budget with chi...` | [docs](docs/budgets-and-usage.md) |
| **pydantic-ai** | 2.57.0 | S1 (unauthorized side effect) | Yes | unauthorized writes: 0 | `ApprovalRequired in tool body with outpu...` | [docs](https://ai.pydantic.dev/deferred-tools/#human-in-the-loop-tool-approval) |
| **pydantic-ai** | 2.57.0 | S2a (crash inside tool after side effect before return) | No | not supported natively | `None (durable execution requires externa...` | [docs](https://ai.pydantic.dev/durable-execution/) |
| **pydantic-ai** | 2.57.0 | S2b (crash after tool returned before next step) | No | not supported natively | `None (durable execution requires externa...` | [docs](https://ai.pydantic.dev/durable-execution/) |
| **pydantic-ai** | 2.57.0 | S3 (parallel spending) | Partial (shared counter, not concurrent admission) | admitted: 10 (over: 7) | `Agent.run(usage_limits=UsageLimits(reque...` | [docs](https://ai.pydantic.dev/usage/) |
| **langgraph** | 1.2.16 | S1 (unauthorized side effect) | Yes | unauthorized writes: 0 | `StateGraph.compile(checkpointer=MemorySa...` | [docs](https://langchain-ai.github.io/langgraph/how-tos/human-in-the-loop/) |
| **langgraph** | 1.2.16 | S2a (crash inside tool after side effect before return) | Yes | duplicate writes: 1 | `StateGraph.compile(checkpointer=MemorySa...` | [docs](https://langchain-ai.github.io/langgraph/concepts/persistence/#replaying) |
| **langgraph** | 1.2.16 | S2b (crash after tool returned before next step) | Yes | duplicate writes: 0 | `StateGraph.compile(checkpointer=MemorySa...` | [docs](https://langchain-ai.github.io/langgraph/concepts/persistence/#replaying) |
| **langgraph** | 1.2.16 (langchain==1.4.6) | S3 (parallel spending) | Partial (per-thread/run limit only) | admitted: 10 (over: 7) | `create_agent(model, middleware=[ModelCal...` | [docs](https://langchain-ai.github.io/langgraph/) |

---

## Scenarios Breakdown & Methodology

### S1: Unauthorized Side Effect
- **Nexura**: Supported: `"Yes"`. Configured with `@tool(requires_approval=True)`. `Runner.run_async()` pauses with
  `RunInterruption` before invoking the tool handler (`docs/approvals.md`). 0 unauthorized writes execute.
- **Pydantic AI**: Supported: `"Yes"`. Tool checks `ctx.tool_call_approved` and raises `ApprovalRequired`. Agent output
  type includes `DeferredToolRequests`. Resumed with `DeferredToolResults`. 0 unauthorized writes execute.
- **LangGraph**: Supported: `"Yes"`. `StateGraph.compile(checkpointer=MemorySaver(), interrupt_before=['tools'])`.
  Execution halts before entering the `tools` node; resumed with `graph.invoke(None, config)`. 0 unauthorized writes execute.

### S2a: Crash Inside Tool After Side Effect (Before Return)
- **Nexura**: Supported: `"Yes"`. Detects uncommitted non-idempotent tool execution. On recovery, marks outcome unknown
  (`outcome_unknown_after_crash`), pauses, and requests review (`docs/durable-runs.md`).
  0 duplicate writes execute.
- **Pydantic AI**: Supported: `"No"`. Not supported natively. No standalone checkpoint store or persistence engine.
- **LangGraph**: Supported: `"Yes"`. `MemorySaver` commits checkpoints at superstep boundaries. Mid-tool crash means the
  superstep was not saved; recovery rolls back to pre-tool superstep and replays the tool node,
  executing 1 duplicate write.

### S2b: Crash After Tool Returned (Before Next Step)
- **Nexura**: Supported: `"Yes"`. Completed tool outputs are committed to the `RunCheckpointStore` upon completion.
  On recovery via `Runner.recover_async()`, the recorded output is reused without replaying the write
  tool (`docs/durable-runs.md`). 0 duplicate writes execute.
- **Pydantic AI**: Supported: `"No"`. Not supported natively.
- **LangGraph**: Supported: `"Yes"`. The tool node superstep completed and committed to `MemorySaver`. Recovery resumes
  directly at the subsequent agent node without replaying the tool. 0 duplicate writes execute.

### S3: Parallel Spending
- **Nexura**: Supported: `"Yes (shared allowance)"`. `BudgetManager` with `BudgetConfig(max_tokens=60)` shared across 10 child budgets
  (`docs/budgets-and-usage.md`). `BaseProvider.generate` performs atomic token reservations before
  dispatching to the model. Exceeded calls are denied before execution. 0 calls over allowance.
- **Pydantic AI**: Supported: `"Partial (shared counter, not concurrent admission)"`. `UsageLimits(request_limit=3)` passed with a shared `RunUsage()` instance.
  *Design Note*: Sharing one `RunUsage` is documented in Pydantic AI for agent delegation (sequential),
  not as a concurrent admission control across parallel runs. Under concurrent load outside its
  design, because `check_before_request` lacks atomic reservations, all 10 coroutines pass the
  pre-check simultaneously, admitting all 10 calls (7 over allowance).
- **LangGraph / LangChain**: Supported: `"Partial (per-thread/run limit only)"`. Tested LangChain v1 `ModelCallLimitMiddleware`
  (`experiments/benchmark/.venv/lib/python3.12/site-packages/langchain/agents/middleware/model_call_limit.py`).
  Stores counts in `ModelCallLimitState` channels `thread_model_call_count` and `run_model_call_count`,
  which are scoped strictly per thread/run. It cannot share an allowance across parallel agent
  threads. Furthermore, `after_model` increments counts only post-hoc after each call completes,
  admitting all 10 calls (7 over allowance).
