# Reproducible Multi-Framework Agent Benchmark Harness (Task P33, S3 rerun P35 A5)

This benchmark provides an honest, strictly reproducible comparison between **Nexura**,
**Pydantic AI**, and **LangGraph** across four agent runtime failure and concurrency scenarios:
1. **S1 "unauthorized side effect"**: write actions requiring human approval.
2. **S2a "crash inside tool after side effect, before return"**: process interruptions during tool
   execution (in-flight side effect) and subsequent recovery behavior.
3. **S2b "crash after tool returned, before next step"**: process interruptions after tool
   completion before the next step commits, and subsequent recovery behavior.
4. **S3a-c "parallel spending"**: 10 concurrent agents share one spending limit that fits 3
   calls at the estimated cost, in three cases: the estimate equals, is lower than, or is higher
   than the actual cost. Rerun on 2026-10-20 (P35 A5) after a fact-check found the original S3
   setup skewed; see [S3a-c](#s3a-c-parallel-spending-fair-rerun-p35-a5).

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
- **Shared spending limit under concurrency (S3a-c)**: When 10 parallel agents share a $0.03
  limit, how many model calls does each framework's own documented budget feature let through,
  what do they cost, and how far over the limit does the total go? Measured with the estimate
  equal to, lower than and higher than the actual cost, plus a sequential control run.

### What is NOT Measured
- **Real-Model Quality**: This harness does not evaluate reasoning benchmarks (e.g. MMLU,
  SWE-bench) or model intelligence. All tests use canned test models.
- **Latency / Performance Claims**: Synthetic fake models execute near-instantaneously in memory
  (the S3a-c fake waits 20 ms only so that parallel calls overlap). No latency claims are made.
- **Third-Party Orchestrator Setups**: Only native framework capabilities (or documented standard
  features) are benchmarked. For example, Pydantic AI's external server integrations
  (Temporal/DBOS/Prefect) are noted, but not run without native support.

---

## Environment & Pinned Versions

The benchmark harness runs in its own isolated virtual environment located in `experiments/benchmark/`:

| Framework | Pinned Version | Test Model Used | Documentation |
|---|---|---|---|
| **Nexura** | `0.4.0` (local path) | `FakeModel` / `BaseProvider` | `docs/` |
| **Pydantic AI** | `2.57.0` (+ `pydantic-ai-harness==0.57.0` for `SpendLimits`) | `FunctionModel` | [Pydantic AI Docs](https://pydantic.dev/docs/ai/) |
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
- `experiments/benchmark/results/results.json` (Machine-readable raw data, with date, package
  versions, the command and the Nexura SDK source path in `metadata`)
- `experiments/benchmark/results/RESULTS.md` (Formatted markdown report)

The Nexura packages are installed editable from this repository, so a run uses whatever is in
the working tree. The published 2026-10-20 results were produced against a clean export of
commit `522ea21`, because other work in progress had left `runner.py` temporarily broken
(it failed S1 with a `NameError`). The exact commands were:

```bash
mkdir -p /tmp/a5-nexura-head
git archive HEAD packages/nexura-agents | tar -x -C /tmp/a5-nexura-head
PYTHONPATH=/tmp/a5-nexura-head/packages/nexura-agents/src \
  uv --directory experiments/benchmark run python run.py
```

The S3a-c numbers were also reproduced against the working tree of that day (the spending
tests pass there unchanged). `RESULTS.md` records which source was imported.

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

## Summary Results Table (S1, S2a, S2b and the superseded S3)

The S3 rows in this table are the original P33 setup (`S3-legacy` in `RESULTS.md`). Do not
quote them: they are kept only so their tests keep running. The fair rerun is
[S3a-c](#s3a-c-parallel-spending-fair-rerun-p35-a5) below.

| Framework | Version | Scenario | Supported | Key Metric | Configuration Used | Docs |
|---|---|---|---|---|---|---|
| **nexura** | 0.4.0 | S1 (unauthorized side effect) | Yes | unauthorized writes: 0 | `@tool(requires_approval=True) with Runne...` | [docs](docs/approvals.md) |
| **nexura** | 0.4.0 | S2a (crash inside tool after side effect before return) | Yes | duplicate writes: 0 | `Runner(checkpoint_store=MemoryCheckpoint...` | [docs](docs/durable-runs.md) |
| **nexura** | 0.4.0 | S2b (crash after tool returned before next step) | Yes | duplicate writes: 0 | `Runner(checkpoint_store=MemoryCheckpoint...` | [docs](docs/durable-runs.md) |
| **nexura** | 0.4.0 | S3-legacy (superseded) | Yes (shared allowance) | admitted: 3 (over: 0) | `BudgetManager.create_run_budget with chi...` | [docs](docs/budgets-and-usage.md) |
| **pydantic-ai** | 2.57.0 | S1 (unauthorized side effect) | Yes | unauthorized writes: 0 | `ApprovalRequired in tool body with outpu...` | [docs](https://ai.pydantic.dev/deferred-tools/#human-in-the-loop-tool-approval) |
| **pydantic-ai** | 2.57.0 | S2a (crash inside tool after side effect before return) | No | not supported natively | `None (durable execution requires externa...` | [docs](https://ai.pydantic.dev/durable-execution/) |
| **pydantic-ai** | 2.57.0 | S2b (crash after tool returned before next step) | No | not supported natively | `None (durable execution requires externa...` | [docs](https://ai.pydantic.dev/durable-execution/) |
| **pydantic-ai** | 2.57.0 | S3-legacy (superseded) | Partial (shared counter, not concurrent admission) | admitted: 10 (over: 7) | `Agent.run(usage_limits=UsageLimits(reque...` | [docs](https://ai.pydantic.dev/usage/) |
| **langgraph** | 1.2.16 | S1 (unauthorized side effect) | Yes | unauthorized writes: 0 | `StateGraph.compile(checkpointer=MemorySa...` | [docs](https://langchain-ai.github.io/langgraph/how-tos/human-in-the-loop/) |
| **langgraph** | 1.2.16 | S2a (crash inside tool after side effect before return) | Yes | duplicate writes: 1 | `StateGraph.compile(checkpointer=MemorySa...` | [docs](https://langchain-ai.github.io/langgraph/concepts/persistence/#replaying) |
| **langgraph** | 1.2.16 | S2b (crash after tool returned before next step) | Yes | duplicate writes: 0 | `StateGraph.compile(checkpointer=MemorySa...` | [docs](https://langchain-ai.github.io/langgraph/concepts/persistence/#replaying) |
| **langgraph** | 1.2.16 (langchain==1.4.6) | S3-legacy (superseded) | Partial (per-thread/run limit only) | admitted: 10 (over: 7) | `create_agent(model, middleware=[ModelCal...` | [docs](https://langchain-ai.github.io/langgraph/) |

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

### S3a-c: Parallel Spending (fair rerun, P35 A5)

#### Why it was rerun

The original S3 reported "Nexura admitted 3 of 10, the others 10 of 10". The 2026-10-20
fact-check (`.autopilot/research/factcheck.md`, row A3b) found that comparison skewed:

- Nexura used a token cap, and its fake provider's admission estimate was exactly the actual
  cost, so the estimate could never be wrong.
- Pydantic AI ran a shared `RunUsage` with only `request_limit`; its `cost_limit` and the
  harness `SpendLimits` were not configured.
- LangGraph ran one thread per agent, so its call limit could not count across agents.
- The Claude Agent SDK (`max_budget_usd`) and Mastra (`TokenCostControl`) were not considered.

The old setup is still in the code as `S3-legacy` (so its tests keep running) and is labelled
superseded in every report.

#### Method

- **Workload**: 10 agents, each makes one model call to the same fake model. The fake call waits
  20 ms before answering, like a network round trip, so all admitted calls are in flight at
  the same time. Without that wait, asyncio would run the "parallel" agents one after another.
- **Price**: a fictitious `bench-fake-model` at $2.00 per million tokens, the same for input and
  output (so Nexura's 50/50 reservation split is not wrong merely because of the split).
- **Estimate**: 5,000 tokens = **$0.01 per call**. **Limit: $0.03**, which fits 3 calls at the
  estimate. Features that count calls instead of money get a limit of 3 calls.
- **Cases** (the fake model reports this usage; the estimate never changes):

  | Case | What happens | Actual cost per call | Calls that fit in $0.03 |
  |---|---|---|---|
  | (a) | estimate equals actual | $0.01 | 3 |
  | (b) | model replies longer than estimated | $0.02 (2x) | 1 |
  | (c) | model replies shorter than estimated | $0.005 (0.5x) | 6 |

- **Spend** is ground truth: calls that actually ran (counted inside the fake model) times the
  actual cost per call. It does not rely on any framework's accounting. Each framework's own
  counter is reported next to it in `RESULTS.md`; all of them matched the ground truth.
- **Sequential control**: the same 10 agents run one after another, to show each feature doing
  what it is designed to do when runs do not overlap.
- **Each framework's documented feature, checked against the installed source**
  (`experiments/benchmark/.venv/lib/python3.12/site-packages/...`):

  | Framework | Feature and configuration | Source checked |
  |---|---|---|
  | Nexura 0.4.0 | `BudgetManager(pricing_table=...)`, root run budget `BudgetConfig(max_cost_usd=Decimal("0.03"))`, one `create_child_budget` per agent, `Runner(budget=child)`. The fake provider's estimate is fixed at 5,000 tokens. ([docs/budgets-and-usage.md](../../docs/budgets-and-usage.md)) | `nexura_agents/usage/ledger.py` `reserve` (checks spent + reserved + estimate under one lock) and `reconcile` (settles the reported cost) |
  | Pydantic AI 2.57.0 | `agent.run(usage_limits=UsageLimits(request_limit=3, cost_limit=Decimal("0.03")), usage=shared_run_usage)`: one `RunUsage` passed to every run, the documented [agent delegation](https://pydantic.dev/docs/ai/guides/multi-agent-applications/#agent-delegation) pattern. The fake response carries its cost, because genai-prices cannot price a `FunctionModel`. ([usage limits](https://pydantic.dev/docs/ai/core-concepts/agent/#usage-limits)) | `pydantic_ai/usage.py` `UsageLimits.check_before_request` and `check_cost` |
  | pydantic-ai-harness 0.57.0 (pins pydantic-ai 2.57.0) | `Agent(..., capabilities=[SpendLimits(budgets=[Budget(usd=Decimal("0.03"), window="total")], price=..., on_unpriced="raise")])`, one capability instance (one `InMemorySpendStore`) shared by every run. `SpendLimits` is not in pydantic-ai itself, so the harness package was added to this project only. ([spend docs](https://pydantic.dev/docs/ai/harness/spend/)) | `pydantic_ai_harness/spend/_capability.py` and its README, "What the gate guarantees" |
  | LangGraph 1.2.16 / LangChain 1.4.6 | (1) `ModelCallLimitMiddleware(thread_limit=3)` with every invocation on **one shared** `thread_id`, the only way its counter spans invocations; (2) `ModelCallLimitMiddleware(run_limit=3)` with a thread per agent; (3) `recursion_limit=3` on one graph that fans out to 10 model calls with `Send`. ([model call limit](https://docs.langchain.com/oss/python/langchain/middleware/built-in#model-call-limit), [recursion limit](https://docs.langchain.com/oss/python/langgraph/graph-api#recursion-limit)) | `langchain/agents/middleware/model_call_limit.py` (`before_model` checks, `after_model` increments). LangChain has no built-in money or token budget middleware |

#### Results (2026-10-20, all calls in parallel)

"Calls run" is how many of the 10 model calls actually happened. "Over limit" is how far the
total went past $0.03.

| Framework and feature | (a) estimate = actual | (b) estimate too low | (c) estimate too high |
|---|---|---|---|
| **Nexura** shared USD budget | 3 calls, $0.03, **not over** | 3 calls, $0.06, **$0.03 over (+100%)** | 3 calls, $0.015, not over (6 would have fit) |
| **Pydantic AI** `UsageLimits` + shared `RunUsage` | 10 calls, $0.10, $0.07 over (+233%) | 10 calls, $0.20, $0.17 over (+567%) | 10 calls, $0.05, $0.02 over (+67%) |
| **Pydantic AI harness** `SpendLimits` | 10 calls, $0.10, $0.07 over | 10 calls, $0.20, $0.17 over | 10 calls, $0.05, $0.02 over |
| **LangGraph** `ModelCallLimitMiddleware(thread_limit=3)`, shared thread | 10 calls, $0.10, $0.07 over | 10 calls, $0.20, $0.17 over | 10 calls, $0.05, $0.02 over |
| **LangGraph** `ModelCallLimitMiddleware(run_limit=3)`, thread per agent | 10 calls, $0.10, $0.07 over | 10 calls, $0.20, $0.17 over | 10 calls, $0.05, $0.02 over |
| **LangGraph** `recursion_limit=3`, fan-out graph | 10 calls, $0.10, $0.07 over | 10 calls, $0.20, $0.17 over | 10 calls, $0.05, $0.02 over |

With `UsageLimits`, most runs (8 to 10 of 10, depending on how responses interleave) raised
`UsageLimitExceeded` **after** their call had already run and been paid for.

#### Sequential control (same agents, one after another)

| Framework and feature | (a) | (b) | (c) |
|---|---|---|---|
| **Nexura** shared USD budget | 3 calls, $0.03 | 2 calls, $0.04, $0.01 over | 5 calls, $0.025 |
| **Pydantic AI** `UsageLimits` (request_limit=3, cost_limit) | 3 calls, $0.03 | 2 calls, $0.04, $0.01 over | 3 calls, $0.015 (request limit) |
| **Pydantic AI harness** `SpendLimits` | 3 calls, $0.03 | 2 calls, $0.04, $0.01 over | 6 calls, $0.03 |
| **LangGraph** `thread_limit=3`, shared thread | 3 calls, $0.03 | 3 calls, $0.06, $0.03 over | 3 calls, $0.015 |
| **LangGraph** `run_limit=3`, thread per agent | 10 calls, $0.10, $0.07 over | 10 calls, $0.20, $0.17 over | 10 calls, $0.05, $0.02 over |

#### What the numbers say, plainly

- **Run one after another, every money-aware feature works.** Nexura, `UsageLimits` and
  `SpendLimits` all stop at the limit; all three let the run that crosses it finish, so all
  three end $0.01 over in case (b). `SpendLimits` uses the leftover money best in case (c).
- **Run in parallel, only Nexura holds the limit**, because it is the only one of these features
  that reserves money before the call. The others check a counter that is still zero while all
  10 calls are in flight. Pydantic AI documents this: `SpendLimits` "reserves nothing" and
  "concurrent runs can each pass the check before any of them records anything", and
  `cost_limit` is not "a hard billing guarantee".
- **Nexura is not a hard cap either.** It reserves the *estimate*. When replies cost more than
  estimated (case b), the 3 admitted calls cost $0.06 against a $0.03 limit: 100% over. When
  replies cost less (case c), it is conservative and lets only 3 run although 6 would have fit.
  How close Nexura gets depends on how good the estimate is. The SDK's default estimator
  (in `providers/base.py`) is a heuristic, and this benchmark fixes the estimate instead of using it.
- **LangGraph and LangChain have no shared money budget.** Their limits count calls or graph
  steps per thread, run or invocation; none of them bounds 10 parallel calls.
- A fair one-line summary is: *with a shared budget and parallel agents, Nexura admitted 3 of
  10 calls while the others admitted 10 of 10, but Nexura still overspends by as much as the
  estimate is wrong.* "Nexura never overspends" is **not** supported.

#### Not run here: Claude Agent SDK and Mastra

These two were not run, because neither can run offline in this harness. No numbers are claimed
for them. Their documented semantics, read on 2026-10-20:

- **Claude Agent SDK, `max_budget_usd` / `maxBudgetUsd`** (needs the bundled Claude Code
  binary and a paid Claude model). The [agent loop docs](https://code.claude.com/docs/en/agent-sdk/agent-loop#turns-and-budget)
  describe it as the "estimated spend at which the loop stops". The run ends with result subtype
  `error_max_budget_usd`. Spend is compared with the cap "after model responses arrive", so "the
  response that reaches the cap still completes" and spend "can therefore pass the cap by up to
  the cost of that one response, plus anything that subagents still running at that moment spend
  before they stop". The cap covers subagents within one session. Nothing documents a budget
  shared by separate `query()` sessions running in parallel, so 10 independent sessions would
  each have their own cap. Example: [max_budget_usd.py](https://github.com/anthropics/claude-agent-sdk-python/blob/main/examples/max_budget_usd.py).
- **Mastra, `TokenCostControl` processor** (TypeScript). The [reference](https://mastra.ai/reference/processors/token-cost-control)
  says it "checks cumulative estimated cost against the resolved maxCost before each LLM call",
  with scopes `run`, `resource`, user, organization and session over a time window, and strategy
  `block` (aborts with a TripWire) or `warn`. It "requires observability storage with
  `getMetricAggregate` support". It is documented as approximate: "Cost checks are approximate
  due to metric persistence delays ... the cost control may not detect a limit breach until one
  or more steps after the actual cost exceeded the threshold." Some missing-context cases fail
  open (the check is skipped).

Both are post-hoc checks against recorded spend, like `SpendLimits`. By their own docs, neither
reserves before a call, so they would be expected to behave like the Pydantic AI rows under
parallel load. That is an inference from the docs, not a measurement.

#### Limitations

- Fake models only: no real provider, no real token counts or prices, no network latency
  variance. The 20 ms fake latency is what makes the calls overlap; with a zero-wait fake model
  every framework behaves like the sequential control.
- One shape of workload: 10 agents x 1 call. Longer agent loops would let post-hoc checks stop
  the later calls of each run, which narrows the gap.
- The Nexura estimate is fixed per case to control the estimate/actual relation. Real
  estimates come from the SDK heuristic. Other P35 work is changing that heuristic and budget
  coverage (A1), so rerun this benchmark after A1 lands.
- Built-in, documented features only. Any of these frameworks can enforce a shared cap with
  custom code, such as a lock-protected counter in a Pydantic AI `before_model_request` hook or
  a LangGraph node, or with a provider-side spend limit.
- The count of `UsageLimits` runs that raise after their call (8 to 10) depends on scheduling.
  Admitted calls and spend were identical across repeated runs.
- Single process, in-memory stores (`InMemorySpendStore`, `MemorySaver`, the SDK's in-memory
  ledger). Multi-process behaviour (for example `RedisSpendStore`, a SQLite ledger) was not measured.
