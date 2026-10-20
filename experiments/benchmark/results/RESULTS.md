# Benchmark Results: Nexura vs Pydantic AI vs LangGraph

**Date Generated**: 2026-10-18T20:28:31.158919+00:00  
**Python Runtime**: 3.12.14 (Linux-7.0.0-1014-gcp-x86_64-with-glibc2.43)  
**Environment**: Offline, deterministic scripted models only (no API keys/paid calls).

## Summary Results Table

| Framework | Version | Scenario | Supported | Key Metric | Configuration Used | Docs |
|---|---|---|---|---|---|---|
| **nexura** | 0.4.0 | S1 (unauthorized side effect) | Yes | unauthorized writes: 0 | `@tool(requires_approval=True) with Runne...` | [docs](../../../docs/approvals.md) |
| **nexura** | 0.4.0 | S2a (crash inside tool after side effect before return) | Yes | duplicate writes: 0 | `Runner(checkpoint_store=MemoryCheckpoint...` | [docs](../../../docs/durable-runs.md) |
| **nexura** | 0.4.0 | S2b (crash after tool returned before next step) | Yes | duplicate writes: 0 | `Runner(checkpoint_store=MemoryCheckpoint...` | [docs](../../../docs/durable-runs.md) |
| **nexura** | 0.4.0 | S3 (parallel spending) | Yes (shared allowance) | admitted: 3 (over: 0) | `BudgetManager.create_run_budget with chi...` | [docs](../../../docs/budgets-and-usage.md) |
| **pydantic-ai** | 2.57.0 | S1 (unauthorized side effect) | Yes | unauthorized writes: 0 | `ApprovalRequired in tool body with outpu...` | [docs](https://ai.pydantic.dev/deferred-tools/#human-in-the-loop-tool-approval) |
| **pydantic-ai** | 2.57.0 | S2a (crash inside tool after side effect before return) | No | not supported natively | `None (durable execution requires externa...` | [docs](https://ai.pydantic.dev/durable-execution/) |
| **pydantic-ai** | 2.57.0 | S2b (crash after tool returned before next step) | No | not supported natively | `None (durable execution requires externa...` | [docs](https://ai.pydantic.dev/durable-execution/) |
| **pydantic-ai** | 2.57.0 | S3 (parallel spending) | Partial (shared counter, not concurrent admission) | admitted: 10 (over: 7) | `Agent.run(usage_limits=UsageLimits(reque...` | [docs](https://ai.pydantic.dev/usage/) |
| **langgraph** | 1.2.16 | S1 (unauthorized side effect) | Yes | unauthorized writes: 0 | `StateGraph.compile(checkpointer=MemorySa...` | [docs](https://langchain-ai.github.io/langgraph/how-tos/human-in-the-loop/) |
| **langgraph** | 1.2.16 | S2a (crash inside tool after side effect before return) | Yes | duplicate writes: 1 | `StateGraph.compile(checkpointer=MemorySa...` | [docs](https://langchain-ai.github.io/langgraph/concepts/persistence/#replaying) |
| **langgraph** | 1.2.16 | S2b (crash after tool returned before next step) | Yes | duplicate writes: 0 | `StateGraph.compile(checkpointer=MemorySa...` | [docs](https://langchain-ai.github.io/langgraph/concepts/persistence/#replaying) |
| **langgraph** | 1.2.16 (langchain==1.4.6) | S3 (parallel spending) | Partial (per-thread/run limit only) | admitted: 10 (over: 7) | `create_agent(model, middleware=[ModelCal...` | [docs](https://langchain-ai.github.io/langgraph/) |

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

### S3: Parallel Spending
**Objective**: N=10 parallel agents share an allowance of K=3 model calls (or equivalent
token budget). We measure how many model calls were actually admitted/executed above
the allowance using each framework's documented spend/usage limit feature.

- **Nexura (`0.4.0`)**:
  - *Support Status*: **Yes (shared allowance)**
  - *Configuration*: `BudgetManager().create_run_budget()` with max_tokens=60
    and child budgets. Before dispatch, `BaseProvider.generate` reserves tokens via
    atomic admission (`admit_provider_call`).
  - *Result*: **0 admitted calls above allowance** (allowance: 3, admitted calls: 3,
    above allowance: 0). Excess calls are denied at the admission gate before execution.
  - *Docs*: [docs/budgets-and-usage.md](../../../docs/budgets-and-usage.md)

- **Pydantic AI (`2.57.0`)**:
  - *Support Status*: **Partial (shared counter, not concurrent admission)**
  - *Configuration*: `UsageLimits(request_limit=3)` passed with shared `RunUsage()`
    to `agent.run(..., usage_limits=limits, usage=shared_usage)`.
  - *Result*: **7 admitted calls above allowance** (allowance: 3, admitted calls: 10,
    above allowance: 7).
    *Design Note*: Sharing one `RunUsage` is documented in Pydantic AI for agent
    delegation (sequential execution), not as a concurrent admission control across
    parallel runs. This benchmark measures the documented feature under concurrent load
    outside its stated design. Because `check_before_request` lacks atomic reservations,
    all 10 coroutines pass the pre-check concurrently while `usage.requests` is 0,
    admitting all 10 calls.
  - *Docs*: [Pydantic AI Usage and Limits](https://ai.pydantic.dev/usage/)

- **LangGraph (`1.2.16`) / LangChain (`1.4.6`)**:
  - *Support Status*: **Partial (per-thread/run limit only)**
  - *Configuration*: `ModelCallLimitMiddleware(thread_limit=3, run_limit=3)`
    from `langchain.agents.middleware.model_call_limit`.
  - *Result*: **7 admitted calls above allowance** (allowance: 3, admitted calls: 10,
    above allowance: 7).
    *Source Citation*: `experiments/benchmark/.venv/lib/python3.12/site-packages/langchain/agents/middleware/model_call_limit.py`.
    *Why*: The middleware stores counters in `ModelCallLimitState` channels
    `thread_model_call_count` and `run_model_call_count`. These channels are scoped
    per thread/run; it cannot share an allowance across parallel agent threads.
    Furthermore, `after_model` increments counts only post-hoc after each call completes,
    so concurrent requests pass the pre-check simultaneously without atomic reservation.
  - *Docs*: [LangGraph Documentation](https://langchain-ai.github.io/langgraph/)

---

## Methodology and Fairness Disclaimers
1. **No Paid Calls / Fake Models Only**: All tests use offline deterministic test models
   (`FakeModel`, `FunctionModel`, `GenericFakeChatModel`).
2. **Documented Configurations**: Each framework adapter follows the exact configuration
   recommended in its official documentation.
3. **What is NOT Measured**: This benchmark evaluates structural SDK mechanics
   (approvals, recovery, concurrency bounds). It does **not** evaluate real-world
   LLM reasoning quality, model intelligence, or synthetic fake-model latency.
