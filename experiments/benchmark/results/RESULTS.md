# Benchmark Results: Nexura vs Pydantic AI vs LangGraph

**Date Generated**: 2026-10-20T12:22:45.469948+00:00  
**Command**: `uv --directory experiments/benchmark run python run.py` (from the repository root)  
**Python Runtime**: 3.12.14 (Linux-7.0.0-1014-gcp-x86_64-with-glibc2.43)  
**Package versions**: `nexura-agents==0.4.0`, `pydantic-ai==2.57.0`, `pydantic-ai-harness==0.57.0`, `langgraph==1.2.16`, `langchain==1.4.6`, `langchain-core==1.6.12`  
**Nexura SDK source**: `/tmp/a5-nexura-head/packages/nexura-agents/src/nexura_agents/__init__.py`, not the repository working tree (repository HEAD `522ea21`; see README for the export used)  
**Environment**: Offline, deterministic scripted models only (no API keys/paid calls).

## Summary Results Table (S1, S2a, S2b, legacy S3)

| Framework | Version | Scenario | Supported | Key Metric | Configuration Used | Docs |
|---|---|---|---|---|---|---|
| **nexura** | 0.4.0 | S1 (unauthorized side effect) | Yes | unauthorized writes: 0 | `@tool(requires_approval=True) with Runne...` | [docs](../../../docs/approvals.md) |
| **nexura** | 0.4.0 | S2a (crash inside tool after side effect before return) | Yes | duplicate writes: 0 | `Runner(checkpoint_store=MemoryCheckpoint...` | [docs](../../../docs/durable-runs.md) |
| **nexura** | 0.4.0 | S2b (crash after tool returned before next step) | Yes | duplicate writes: 0 | `Runner(checkpoint_store=MemoryCheckpoint...` | [docs](../../../docs/durable-runs.md) |
| **nexura** | 0.4.0 | S3-legacy (parallel spending (legacy P33 setup, superseded by S3a-c)) | Yes (shared allowance) | admitted: 3 (over: 0) | `BudgetManager.create_run_budget with chi...` | [docs](../../../docs/budgets-and-usage.md) |
| **pydantic-ai** | 2.57.0 | S1 (unauthorized side effect) | Yes | unauthorized writes: 0 | `ApprovalRequired in tool body with outpu...` | [docs](https://ai.pydantic.dev/deferred-tools/#human-in-the-loop-tool-approval) |
| **pydantic-ai** | 2.57.0 | S2a (crash inside tool after side effect before return) | No | not supported natively | `None (durable execution requires externa...` | [docs](https://ai.pydantic.dev/durable-execution/) |
| **pydantic-ai** | 2.57.0 | S2b (crash after tool returned before next step) | No | not supported natively | `None (durable execution requires externa...` | [docs](https://ai.pydantic.dev/durable-execution/) |
| **pydantic-ai** | 2.57.0 | S3-legacy (parallel spending (legacy P33 setup, superseded by S3a-c)) | Partial (shared counter, not concurrent admission) | admitted: 10 (over: 7) | `Agent.run(usage_limits=UsageLimits(reque...` | [docs](https://ai.pydantic.dev/usage/) |
| **langgraph** | 1.2.16 | S1 (unauthorized side effect) | Yes | unauthorized writes: 0 | `StateGraph.compile(checkpointer=MemorySa...` | [docs](https://langchain-ai.github.io/langgraph/how-tos/human-in-the-loop/) |
| **langgraph** | 1.2.16 | S2a (crash inside tool after side effect before return) | Yes | duplicate writes: 1 | `StateGraph.compile(checkpointer=MemorySa...` | [docs](https://langchain-ai.github.io/langgraph/concepts/persistence/#replaying) |
| **langgraph** | 1.2.16 | S2b (crash after tool returned before next step) | Yes | duplicate writes: 0 | `StateGraph.compile(checkpointer=MemorySa...` | [docs](https://langchain-ai.github.io/langgraph/concepts/persistence/#replaying) |
| **langgraph** | 1.2.16 (langchain==1.4.6) | S3-legacy (parallel spending (legacy P33 setup, superseded by S3a-c)) | Partial (per-thread/run limit only) | admitted: 10 (over: 7) | `create_agent(model, middleware=[ModelCal...` | [docs](https://langchain-ai.github.io/langgraph/) |

## Detailed Scenario Analysis

### S1: Unauthorized Side Effect
**Objective**: The model asks to execute a write tool (`send_payment`) that requires
human approval. We count how many writes actually execute without approval when
configured according to each framework's documentation.

- **Nexura (`0.4.0`)**:
  - *Support Status*: **Yes**
  - *Configuration*: `@tool(requires_approval=True)` run via `Runner().run_async()`.
    The runner pauses execution and emits `RunInterruption` before calling the tool.
  - *Result*: **0 unauthorized writes** (`paused_at_gate: True`).
    Upon approval (`runner.resume_async(approve=...)`), 1 write executed.
  - *Docs*: [docs/approvals.md](../../../docs/approvals.md)

- **Pydantic AI (`2.57.0`)**:
  - *Support Status*: **Yes**
  - *Configuration*: Check `ctx.tool_call_approved` and raise `ApprovalRequired()`.
    Agent configured with `output_type=[str, DeferredToolRequests]`.
  - *Result*: **0 unauthorized writes** (`output_type: DeferredToolRequests`).
    Upon approval via `DeferredToolResults`, 1 write executed.
  - *Docs*: [Pydantic AI Deferred Tools](https://ai.pydantic.dev/deferred-tools/#human-in-the-loop-tool-approval)

- **LangGraph (`1.2.16`)**:
  - *Support Status*: **Yes**
  - *Configuration*: `StateGraph` compiled with `checkpointer=MemorySaver()` and
    `interrupt_before=['tools']`. Resumed via `graph.invoke(None, config)`.
  - *Result*: **0 unauthorized writes** (`interrupted_node: tools`).
    Upon approval resume, 1 write executed.
  - *Docs*: [LangGraph Human-in-the-Loop](https://langchain-ai.github.io/langgraph/how-tos/human-in-the-loop/)

---

### S2a: Crash Inside Tool After Side Effect (Before Return)
**Objective**: The process crashes inside the tool body immediately after the write
side effect occurs (`writes += 1`), before the tool function returns. We recover
from the framework's checkpoint/persistence and measure duplicate executions.

- **Nexura (`0.4.0`)**:
  - *Support Status*: **Yes**
  - *Configuration*: `Runner(checkpoint_store=MemoryCheckpointStore())` with
    `@tool(idempotent=False)`.
  - *Result*: **0 duplicate writes** (1 write before crash, 1 write after recovery).
    *Behavior*: Nexura detects that a non-idempotent tool was in-flight when the crash
    occurred. On recovery via `Runner.recover_async()`, it marks the outcome unknown
    (`reason='outcome_unknown_after_crash'`), refuses to blindly re-run the tool, and
    halts in status `'paused'` to request human review.
  - *Docs*: [docs/durable-runs.md](../../../docs/durable-runs.md)

- **Pydantic AI (`2.57.0`)**:
  - *Support Status*: **No**
  - *Configuration*: None (Native standalone checkpointer is **not supported**).
  - *Result*: **Not supported natively**. Pydantic AI does not provide a built-in
    local checkpointer or persistence store for `Agent`. Durable execution is
    documented via external orchestrator packages (`dbos`, `temporalio`, `prefect`).
  - *Docs*: [Pydantic AI Durable Execution](https://ai.pydantic.dev/durable-execution/)

- **LangGraph (`1.2.16`)**:
  - *Support Status*: **Yes**
  - *Configuration*: `StateGraph.compile(checkpointer=MemorySaver())` recovered
    via `graph.invoke(None, config)`.
  - *Result*: **1 duplicate write** (1 write before crash, 2 writes after recovery).
    *Why*: LangGraph persists checkpoints at superstep boundaries (node completion).
    Because the tool node crashed mid-execution, its superstep never committed. Recovery
    rolls back to pre-tool superstep and replays the tool node, causing a duplicate.
  - *Docs*: [LangGraph Persistence Replay](https://langchain-ai.github.io/langgraph/concepts/persistence/#replaying)

---

### S2b: Crash After Tool Returned (Before Next Step)
**Objective**: The write tool completes and returns normally. The process is interrupted
before the next step finishes. We recover from the checkpoint and count duplicate writes.

- **Nexura (`0.4.0`)**:
  - *Support Status*: **Yes**
  - *Configuration*: `Runner(checkpoint_store=MemoryCheckpointStore())` with
    `@tool(idempotent=False)`. Crash simulated in follow-up model call.
  - *Result*: **0 duplicate writes** (1 write before crash, 1 write after recovery).
    *Behavior*: Nexura commits completed tool outputs upon tool return. On recovery,
    it reuses the recorded output without re-executing the non-idempotent write.
  - *Docs*: [docs/durable-runs.md](../../../docs/durable-runs.md)

- **Pydantic AI (`2.57.0`)**:
  - *Support Status*: **No**
  - *Configuration*: None (Native standalone checkpointer is **not supported**).
  - *Result*: **Not supported natively**.
  - *Docs*: [Pydantic AI Durable Execution](https://ai.pydantic.dev/durable-execution/)

- **LangGraph (`1.2.16`)**:
  - *Support Status*: **Yes**
  - *Configuration*: `StateGraph.compile(checkpointer=MemorySaver())` with edge
    `tools -> agent`, crashing in follow-up agent invocation.
  - *Result*: **0 duplicate writes** (1 write before crash, 1 write after recovery).
    *Why*: Because the tool returned, the ToolNode superstep committed to `MemorySaver`.
    Recovery resumes directly at the subsequent agent node without replaying the tool.
  - *Docs*: [LangGraph Persistence Replay](https://langchain-ai.github.io/langgraph/concepts/persistence/#replaying)

---

### S3a-c: Parallel spending (fair rerun, P35 A5)

**Workload**: 10 agents each make one model call to the same fake model. Each fake call waits 0.02s before answering, so admitted calls are in flight together. The shared limit is **$0.03**, which fits 3 calls at the estimated $0.01 per call (5000 tokens at $2.00 per million input or output tokens). Call-count features use a limit of 3 calls.

- **(a) estimate equals actual cost**: 2500 input + 2500 output tokens, $0.01 per call; 3 call(s) fit the limit at actual cost.
- **(b) estimate lower than actual (reply 2x the estimated cost)**: 2500 input + 7500 output tokens, $0.02 per call; 1 call(s) fit the limit at actual cost.
- **(c) estimate higher than actual (reply 0.5x the estimated cost)**: 1250 input + 1250 output tokens, $0.005 per call; 6 call(s) fit the limit at actual cost.

**Spend** is ground truth: model calls that actually ran (counted inside the fake model) times the actual cost per call. **Over limit** is spend minus the limit. 'Errored after call' counts runs whose call ran and was paid for, but which then raised a limit error. 'Framework's own count' is what the framework's ledger or counter recorded, where it has one.

#### Parallel (the scenario)

| Framework | Feature | Case | Mode | Admitted calls | Refused before call | Errored after call | Spend (USD) | Over limit (USD) | Over limit (%) | Framework's own count (USD) |
|---|---|---|---|---|---|---|---|---|---|---|
| nexura | shared run budget (USD) | (a) | parallel | 3 | 7 | 0 | 0.0300 | 0.0000 | 0 | 0.0300 |
| nexura | shared run budget (USD) | (b) | parallel | 3 | 7 | 0 | 0.0600 | 0.0300 | 100 | 0.0600 |
| nexura | shared run budget (USD) | (c) | parallel | 3 | 7 | 0 | 0.0150 | 0.0000 | 0 | 0.0150 |
| pydantic-ai | UsageLimits + shared RunUsage | (a) | parallel | 10 | 0 | 8 | 0.1000 | 0.0700 | 233 | 0.1000 |
| pydantic-ai | SpendLimits (harness) | (a) | parallel | 10 | 0 | 0 | 0.1000 | 0.0700 | 233 | 0.1000 |
| pydantic-ai | UsageLimits + shared RunUsage | (b) | parallel | 10 | 0 | 10 | 0.2000 | 0.1700 | 567 | 0.2000 |
| pydantic-ai | SpendLimits (harness) | (b) | parallel | 10 | 0 | 0 | 0.2000 | 0.1700 | 567 | 0.2000 |
| pydantic-ai | UsageLimits + shared RunUsage | (c) | parallel | 10 | 0 | 9 | 0.0500 | 0.0200 | 67 | 0.0500 |
| pydantic-ai | SpendLimits (harness) | (c) | parallel | 10 | 0 | 0 | 0.0500 | 0.0200 | 67 | 0.0500 |
| langgraph | ModelCallLimitMiddleware(thread_limit), one shared thread | (a) | parallel | 10 | 0 | 0 | 0.1000 | 0.0700 | 233 | n/a |
| langgraph | ModelCallLimitMiddleware(run_limit), thread per agent | (a) | parallel | 10 | 0 | 0 | 0.1000 | 0.0700 | 233 | n/a |
| langgraph | recursion_limit on a fan-out graph | (a) | parallel | 10 | 0 | 0 | 0.1000 | 0.0700 | 233 | n/a |
| langgraph | ModelCallLimitMiddleware(thread_limit), one shared thread | (b) | parallel | 10 | 0 | 0 | 0.2000 | 0.1700 | 567 | n/a |
| langgraph | ModelCallLimitMiddleware(run_limit), thread per agent | (b) | parallel | 10 | 0 | 0 | 0.2000 | 0.1700 | 567 | n/a |
| langgraph | recursion_limit on a fan-out graph | (b) | parallel | 10 | 0 | 0 | 0.2000 | 0.1700 | 567 | n/a |
| langgraph | ModelCallLimitMiddleware(thread_limit), one shared thread | (c) | parallel | 10 | 0 | 0 | 0.0500 | 0.0200 | 67 | n/a |
| langgraph | ModelCallLimitMiddleware(run_limit), thread per agent | (c) | parallel | 10 | 0 | 0 | 0.0500 | 0.0200 | 67 | n/a |
| langgraph | recursion_limit on a fan-out graph | (c) | parallel | 10 | 0 | 0 | 0.0500 | 0.0200 | 67 | n/a |

#### Sequential control (same agents, one after another)

Shows each feature working as designed when runs do not overlap.

| Framework | Feature | Case | Mode | Admitted calls | Refused before call | Errored after call | Spend (USD) | Over limit (USD) | Over limit (%) | Framework's own count (USD) |
|---|---|---|---|---|---|---|---|---|---|---|
| nexura | shared run budget (USD) | (a) | sequential | 3 | 7 | 0 | 0.0300 | 0.0000 | 0 | 0.0300 |
| nexura | shared run budget (USD) | (b) | sequential | 2 | 8 | 0 | 0.0400 | 0.0100 | 33 | 0.0400 |
| nexura | shared run budget (USD) | (c) | sequential | 5 | 5 | 0 | 0.0250 | 0.0000 | 0 | 0.0250 |
| pydantic-ai | UsageLimits + shared RunUsage | (a) | sequential | 3 | 7 | 0 | 0.0300 | 0.0000 | 0 | 0.0300 |
| pydantic-ai | SpendLimits (harness) | (a) | sequential | 3 | 7 | 0 | 0.0300 | 0.0000 | 0 | 0.0300 |
| pydantic-ai | UsageLimits + shared RunUsage | (b) | sequential | 2 | 8 | 1 | 0.0400 | 0.0100 | 33 | 0.0400 |
| pydantic-ai | SpendLimits (harness) | (b) | sequential | 2 | 8 | 0 | 0.0400 | 0.0100 | 33 | 0.0400 |
| pydantic-ai | UsageLimits + shared RunUsage | (c) | sequential | 3 | 7 | 0 | 0.0150 | 0.0000 | 0 | 0.0150 |
| pydantic-ai | SpendLimits (harness) | (c) | sequential | 6 | 4 | 0 | 0.0300 | 0.0000 | 0 | 0.0300 |
| langgraph | ModelCallLimitMiddleware(thread_limit), one shared thread | (a) | sequential | 3 | 7 | 0 | 0.0300 | 0.0000 | 0 | n/a |
| langgraph | ModelCallLimitMiddleware(run_limit), thread per agent | (a) | sequential | 10 | 0 | 0 | 0.1000 | 0.0700 | 233 | n/a |
| langgraph | ModelCallLimitMiddleware(thread_limit), one shared thread | (b) | sequential | 3 | 7 | 0 | 0.0600 | 0.0300 | 100 | n/a |
| langgraph | ModelCallLimitMiddleware(run_limit), thread per agent | (b) | sequential | 10 | 0 | 0 | 0.2000 | 0.1700 | 567 | n/a |
| langgraph | ModelCallLimitMiddleware(thread_limit), one shared thread | (c) | sequential | 3 | 7 | 0 | 0.0150 | 0.0000 | 0 | n/a |
| langgraph | ModelCallLimitMiddleware(run_limit), thread per agent | (c) | sequential | 10 | 0 | 0 | 0.0500 | 0.0200 | 67 | n/a |

#### Configuration and documentation per feature

- **nexura 0.4.0, shared run budget (USD)** (support: Yes (reserves the estimate before each call))
  - Configuration: `BudgetManager(pricing_table=...).create_run_budget(config=BudgetConfig(max_cost_usd=Decimal('0.03'))); root.create_child_budget(...) per agent; Runner(budget=child)`
  - Behaviour: Admission reserves the estimated cost atomically before dispatch and settles the reported cost afterwards. It cannot refuse a call whose actual cost turns out larger than its reservation, so case (b) overspends; in case (c) the unused reservation is returned only after the calls settle, so in parallel fewer calls run than would have fit.
  - Docs: [docs](../../../docs/budgets-and-usage.md)
- **pydantic-ai 2.57.0, UsageLimits + shared RunUsage** (support: Partial (per-run limit; sharing is the delegation pattern))
  - Configuration: `agent.run(usage_limits=UsageLimits(request_limit=3, cost_limit=Decimal('0.03')), usage=shared_run_usage)`
  - Behaviour: UsageLimits checks the request count and cost before each request and the cost again after each response. It reserves nothing, so parallel runs all pass the pre-check while the shared counter is still zero. A run whose response pushes the shared cost over the limit raises UsageLimitExceeded after the call was already made and paid for. The docs say: 'Don't rely on cost_limit as a hard billing guarantee.'
  - Docs: [docs](https://pydantic.dev/docs/ai/core-concepts/agent/#usage-limits), [docs](https://pydantic.dev/docs/ai/guides/multi-agent-applications/#agent-delegation)
- **pydantic-ai 2.57.0 (pydantic-ai-harness==0.57.0), SpendLimits (harness)** (support: Partial (documented as a brake, not a reservation))
  - Configuration: `Agent(..., capabilities=[SpendLimits(budgets=[Budget(usd=Decimal('0.03'), window='total')], price=..., on_unpriced='raise')])`
  - Behaviour: SpendLimits records each priced response and refuses the next request once a window is spent. Its docs say: 'The request that crosses the line completes, and concurrent runs can each pass the check before any of them records anything' and 'Admission is all that call does: it reserves nothing.'
  - Docs: [docs](https://pydantic.dev/docs/ai/harness/spend/)
- **langgraph 1.2.16 (langchain==1.4.6), ModelCallLimitMiddleware(thread_limit), one shared thread** (support: Partial (call count per thread; no cost limit))
  - Configuration: `create_agent(model, middleware=[ModelCallLimitMiddleware(thread_limit=3, exit_behavior='error')], checkpointer=MemorySaver()); all invocations use the same thread_id`
  - Behaviour: Source: langchain/agents/middleware/model_call_limit.py in the benchmark venv. before_model compares the counts in graph state with the limits; after_model increments them. Counting is in calls, not money. Parallel invocations on one thread each read the checkpointed count before any of them writes it back, so the limit only holds when invocations run one after another.
  - Docs: [docs](https://docs.langchain.com/oss/python/langchain/middleware/built-in#model-call-limit)
- **langgraph 1.2.16 (langchain==1.4.6), ModelCallLimitMiddleware(run_limit), thread per agent** (support: No shared limit (limit is per run))
  - Configuration: `create_agent(model, middleware=[ModelCallLimitMiddleware(run_limit=3, exit_behavior='error')], checkpointer=MemorySaver()); one thread_id per agent`
  - Behaviour: Source: langchain/agents/middleware/model_call_limit.py in the benchmark venv. before_model compares the counts in graph state with the limits; after_model increments them. Counting is in calls, not money. Each agent's run makes one call, far below its own limit; nothing counts across agents.
  - Docs: [docs](https://docs.langchain.com/oss/python/langchain/middleware/built-in#model-call-limit)
- **langgraph 1.2.16 (langchain==1.4.6), recursion_limit on a fan-out graph** (support: No (bounds graph steps, not calls or cost))
  - Configuration: `StateGraph with Send(...) to 10 worker nodes; graph.ainvoke(..., {'recursion_limit': 3})`
  - Behaviour: recursion_limit caps the number of supersteps in one invocation. All parallel Send branches run in the same superstep, so it does not limit how many model calls run at once.
  - Docs: [docs](https://docs.langchain.com/oss/python/langgraph/graph-api#recursion-limit)

#### Not run here: Claude Agent SDK and Mastra

Neither can run offline in this harness (the Claude Agent SDK drives the Claude Code binary against a paid model; Mastra is TypeScript). Their documented budget semantics are summarized in `experiments/benchmark/README.md`; no numbers are claimed.

---

### S3-legacy: Parallel spending, original P33 setup (superseded)

Kept so its tests still run, but **do not quote it**. The 2026-10-20 fact-check found it
skewed: Nexura used a token cap with an admission estimate exactly equal to the actual
cost, Pydantic AI ran a shared `RunUsage` without `cost_limit` or `SpendLimits`, and
LangGraph used one thread per agent. S3a-c above replace it.

---

## Methodology and Fairness Disclaimers
1. **No Paid Calls / Fake Models Only**: All tests use offline deterministic test models
   (`FakeModel`/`BaseProvider` subclasses, `FunctionModel`, `GenericFakeChatModel`).
2. **Documented Configurations**: Each adapter uses the framework's documented feature
   for the job; where a feature is used outside its stated design, the notes say so.
3. **Built-in features only**: any framework can enforce a shared cap with custom code
   (for example a lock-protected counter in a Pydantic AI `before_model_request` hook or
   a LangGraph node). S3 measures what the documented features do without such code.
4. **What is NOT Measured**: This benchmark evaluates structural SDK mechanics
   (approvals, recovery, concurrency bounds). It does **not** evaluate real-world
   LLM reasoning quality, model intelligence, or synthetic fake-model latency.
