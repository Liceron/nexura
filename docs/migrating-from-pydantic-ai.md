# Migrating from Pydantic AI

Pydantic AI and Nexura's agent SDK are close in shape. Both have a typed `Agent`, tools that
take a `RunContext` with dependencies, Pydantic output types, usage limits, a test model and
`agent.override(model=...)`. Most of a migration is moving tools off the agent object and
renaming a few calls. The larger differences are in approvals (deferred tools), durable
execution (Temporal, DBOS or Prefect in Pydantic AI; a checkpoint store in Nexura) and graphs.

Everything on this page runs offline with `FakeModel`. Nothing here was checked against a
live Pydantic AI install. The Pydantic AI code blocks are written from the Pydantic AI
documentation for version 2 (read on 2026-10-20, see [Sources](#sources)). They need the
`pydantic-ai` package and an API key, so the test suite does not run them.

Related: [Agents](agents.md), [Tools](tools.md), [Human approval](approvals.md),
[Durable runs](durable-runs.md), [Testing your agents](testing.md).

## Concept map

| Pydantic AI | Nexura | Notes |
| --- | --- | --- |
| `Agent("openai:...", instructions=..., deps_type=..., output_type=...)` | `Agent(name=..., model="openai:...", instructions=..., output_type=...)` | `name` is required. There is no `deps_type`: a tool declares its dependency type in its `RunContext[T]` annotation. |
| `agent.run_sync(prompt)` / `await agent.run(prompt)` | `agent.run(prompt)` / `await agent.run_async(prompt)` | Note the swap: Nexura's `run` is the synchronous one. |
| `agent.run_stream(...)` | `Runner().stream_async(agent, prompt)` | Yields run events, with text deltas when `stream_text=True` ([Streaming](agents.md#streaming)). |
| `result.output` | `result.output` | The validated value in both. `result.final_output` is the raw text. |
| `@agent.tool` with `ctx: RunContext[Deps]` | `@tool` with `ctx: RunContext[Deps]` as the first parameter, then `tools=[...]` | Tools are plain functions, not bound to one agent, so several agents can share them. |
| `@agent.tool_plain` | `@tool` without a `ctx` parameter | |
| `deps=` on a run | `deps=` on a run | `ctx.deps` in both. |
| `instructions=` (or `system_prompt=`) | `instructions=`, a string or a function | ([Instructions](agents.md#instructions)) |
| `message_history=result.all_messages()` | `history=result.messages`, or a session store with `session_id=` | ([Sessions](agents.md#sessions-and-conversation-memory)) |
| `UsageLimits(request_limit=..., output_tokens_limit=...)` passed as `usage_limits=` | `UsageLimits(request_limit=..., output_tokens_limit=...)` passed to `Runner(usage_limits=...)` | Nexura checks the limits after each model response and raises `UsageLimitExceededError`. |
| `UsageLimits(cost_limit=...)` | A budget: `Runner(budget=...)` with `BudgetConfig(max_cost_usd=...)` | A Nexura budget reserves an estimate before each call and refuses calls that do not fit ([Budgets and usage](budgets-and-usage.md)). |
| `TestModel`, `FunctionModel(fn)` | `FakeModel(responses=[...])`, `FakeModel(handler=fn)` | `FakeModel` replays scripted replies and tool calls. `handler=fn(messages, tools)` computes each reply. |
| `agent.override(model=TestModel())` | `agent.override(model=FakeModel(...))` | Same idea: a `with` block for tests. |
| `capture_run_messages()` | `result.messages` | Every result carries the full transcript. |
| `requires_approval=True` on a tool | `@tool(requires_approval=True)` | |
| `raise ApprovalRequired(...)` when the arguments call for it | `@tool(requires_approval=lambda ctx, args: ...)` | The function decides before the tool runs, from the arguments. |
| `output_type=[str, DeferredToolRequests]` and `result.output.approvals` | Nothing to add. `result.status == "paused"` and `result.interruption.pending` | |
| `DeferredToolResults` with `approvals[id] = True` or `ToolDenied("reason")`, passed with `message_history=` | `runner.resume(agent, interruption, approve=..., reject={id: "reason"})` | The interruption holds the transcript. Save it with `to_json()` and resume in another process. |
| `ToolApproved(override_args=...)` | `ApproveWith(arguments=..., digest=...)` | ([Approve with changes](approvals.md#approve-with-changes)) |
| `CallDeferred` (a result supplied from outside the run) | No direct equivalent | `Agent(ask_user=True)` covers a question for the user ([Asking the user a question](approvals.md#asking-the-user-a-question)). |
| Delegation: `await other.run(..., usage=ctx.usage)` inside a tool | `tools=[other.as_tool()]` | The sub-agent's usage is added to the caller's run automatically ([Agents as tools](agents.md#agents-as-tools)). |
| Programmatic hand-off between agents | `handoffs=[...]`, a [team](teams.md), or plain Python | |
| Pydantic Graphs | [Workflows](workflows.md) (`nexura_runtime.workflow`) | Durable graphs with approval gates, `interrupt()`, history and fork. |
| `TemporalDurability()`, `DBOSDurability()`, `PrefectDurability()` capabilities | `Runner(checkpoint_store=SQLiteCheckpointStore(path))` and `runner.recover_async(...)` | No outside engine or server. See the durable example below for what this covers and what it does not. |

## Example: an agent with dependencies, a tool and typed output

Pydantic AI:

```python notest
from dataclasses import dataclass

from pydantic import BaseModel
from pydantic_ai import Agent, RunContext, UsageLimits


@dataclass
class Shop:
    stock: dict[str, int]


class Answer(BaseModel):
    sku: str
    units: int


agent = Agent(
    "openai:gpt-4.1-mini",
    deps_type=Shop,
    output_type=Answer,
    instructions="Answer stock questions with check_stock.",
)


@agent.tool
def check_stock(ctx: RunContext[Shop], sku: str) -> int:
    """Return units in stock for a SKU."""
    return ctx.deps.stock[sku]


result = agent.run_sync(
    "How many A1?", deps=Shop(stock={"A1": 7}), usage_limits=UsageLimits(request_limit=3)
)
print(result.output)
```

Nexura. The tool is a module-level function, and the limits go on the `Runner`:

```python
from dataclasses import dataclass

from pydantic import BaseModel

from nexura_agents import Agent, FakeModel, RunContext, Runner, ToolCall, UsageLimits, tool


@dataclass
class Shop:
    stock: dict[str, int]


class Answer(BaseModel):
    sku: str
    units: int


@tool
def check_stock(ctx: RunContext[Shop], sku: str) -> int:
    """Return units in stock for a SKU."""
    return ctx.deps.stock[sku]


agent = Agent(
    name="Shop assistant",
    instructions="Answer stock questions with check_stock.",
    model=FakeModel(  # use model="openai:gpt-4.1-mini" in production
        responses=[
            [ToolCall(id="c1", name="check_stock", arguments={"sku": "A1"})],
            '{"sku": "A1", "units": 7}',
        ]
    ),
    tools=[check_stock],
    output_type=Answer,
)

runner = Runner(usage_limits=UsageLimits(request_limit=3))
result = runner.run(agent, "How many A1?", deps=Shop(stock={"A1": 7}))
assert result.output == Answer(sku="A1", units=7)
assert result.tool_calls_count == 1
```

What changed:

- `deps_type=Shop` is gone. `RunContext[Shop]` on the tool says what the tool expects.
- `@agent.tool` became `@tool` plus `tools=[check_stock]`.
- `run_sync(..., usage_limits=...)` became `Runner(usage_limits=...).run(...)`. A `Runner`
  also carries budgets, hooks, sessions and checkpoints.

## Example: tool approval

Pydantic AI returns `DeferredToolRequests` as the output, and you continue with
`DeferredToolResults` and the message history:

```python notest
from pydantic_ai import Agent, DeferredToolRequests, DeferredToolResults, ToolDenied

agent = Agent(
    "openai:gpt-4.1-mini",
    output_type=[str, DeferredToolRequests],
    instructions="Pay invoices when asked.",
)


@agent.tool_plain(requires_approval=True)
def pay(invoice: str) -> str:
    """Pay an invoice."""
    return f"paid {invoice}"


result = agent.run_sync("Pay INV-1 and INV-2")
messages = result.all_messages()
requests = result.output

results = DeferredToolResults()
for index, call in enumerate(requests.approvals):
    if index == 0:
        results.approvals[call.tool_call_id] = True
    else:
        results.approvals[call.tool_call_id] = ToolDenied("duplicate invoice")

final = agent.run_sync("Continue", message_history=messages, deferred_tool_results=results)
print(final.output)
```

Nexura pauses the run instead of changing the output type. Here the approval is only needed
above an amount, which replaces raising `ApprovalRequired` inside the tool:

```python
from nexura_agents import Agent, FakeModel, Runner, ToolCall, tool
from nexura_agents.types import RunInterruption

paid = []


@tool(requires_approval=lambda ctx, args: args["amount"] > 100)
def pay(invoice: str, amount: int) -> str:
    """Pay an invoice."""
    paid.append(invoice)
    return f"paid {invoice}"


def build() -> Agent:
    return Agent(
        name="Billing",
        instructions="Pay invoices when asked.",
        model=FakeModel(
            responses=[
                [
                    ToolCall(id="p1", name="pay", arguments={"invoice": "INV-1", "amount": 500}),
                    ToolCall(id="p2", name="pay", arguments={"invoice": "INV-2", "amount": 900}),
                ],
                "Paid INV-1. INV-2 was a duplicate.",
            ]
        ),
        tools=[pay],
    )


agent = build()
runner = Runner()
paused = runner.run(agent, "Pay INV-1 and INV-2")
assert paused.status == "paused" and paid == []
reviewed = paused.interruption.pending_digests()  # keep what the reviewer was shown
saved = paused.interruption.to_json()  # store it; resume later, even in another process

done = runner.resume(
    agent,
    RunInterruption.from_json(saved),
    approve={"p1": reviewed["p1"]},
    reject={"p2": "duplicate invoice"},
)
assert paid == ["INV-1"]
assert done.output == "Paid INV-1. INV-2 was a duplicate."
assert any("duplicate invoice" in m.content for m in done.messages if m.role == "tool")
```

What changed:

- No `DeferredToolRequests` in `output_type`. A paused run has `status == "paused"`, and
  `result.interruption.pending` lists the calls.
- `DeferredToolResults` with `True` and `ToolDenied(...)` became `approve=` and
  `reject={call_id: reason}` on `runner.resume`. Every pending call needs exactly one
  decision.
- The interruption carries the transcript, so you do not pass message history back.
- Approving with a digest from `pending_digests()` ties the approval to the exact
  arguments the reviewer saw. Approvals also expire (`Runner(approval_ttl=...)`) and can be
  signed with `approval_signing_secret` ([Human approval](approvals.md)).

## Example: testing with a scripted model

Pydantic AI tests swap the model with `override` and a `TestModel` or `FunctionModel`:

```python notest
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

with support_agent.override(model=TestModel()):
    ...
```

In Nexura, `FakeModel(handler=...)` plays the role of `FunctionModel`, and `override` works
the same way:

```python
from nexura_agents import Agent, FakeModel, Runner


def build_support_agent() -> Agent:  # your application code
    return Agent(name="Support", instructions="Help.", model="openai:gpt-4.1-mini")


def reply(messages, tools) -> str:
    return "Refunds take 5 days." if "refund" in messages[-1].content else "How can I help?"


support = build_support_agent()  # building the agent makes no network call
with support.override(model=FakeModel(handler=reply)):
    assert Runner().run(support, "When is my refund?").output == "Refunds take 5 days."
    assert Runner().run(support, "Hi").output == "How can I help?"
```

## Example: durable runs

Pydantic AI makes a run durable by adding a durability capability and running the agent
inside the engine's workflow. For Temporal, the docs add `TemporalDurability()` to the
agent's capabilities and run it inside a `PydanticAIWorkflow` on a Temporal server:

```python notest
from pydantic_ai import Agent
from pydantic_ai.durable_exec.temporal import TemporalDurability

agent = Agent("openai:gpt-4.1-mini", name="support", capabilities=[TemporalDurability()])
# ...plus a PydanticAIWorkflow, a Worker and a running Temporal server.
```

Nexura checkpoints the run itself, in a store you choose. After a crash, `recover_async`
continues from the last saved step:

```python
import asyncio

from nexura_agents import Agent, FakeModel, MemoryCheckpointStore, Runner, ToolCall, tool


class Crash(BaseException):  # stands in for a killed process
    pass


crashed = []


@tool(idempotent=True)
def lookup(order_id: str) -> str:
    """Read an order (safe to repeat)."""
    if not crashed:
        crashed.append(order_id)
        raise Crash
    return f"order {order_id}: shipped"


def build(model) -> Agent:
    return Agent(name="Support", instructions="Help with orders.", model=model, tools=[lookup])


async def main() -> None:
    store = MemoryCheckpointStore()  # SQLiteCheckpointStore(path) survives a restart
    runner = Runner(checkpoint_store=store)
    first = FakeModel(responses=[[ToolCall(id="c1", name="lookup", arguments={"order_id": "A1"})]])
    try:
        await runner.run_async(build(first), "Where is A1?")
    except Crash:
        pass

    (run_id,) = await store.list_incomplete()
    result = await runner.recover_async(build(FakeModel(responses=["It shipped."])), run_id)
    assert result.status == "completed" and result.output == "It shipped."
    assert await store.list_incomplete() == []


asyncio.run(main())
```

What this covers, and what it does not:

- The runner saves a checkpoint before each model call and around tool calls. Recovery
  repeats at most one model call and never reruns a tool call that already has a result.
- A tool that may have started before the crash is rerun only if it is marked safe to
  repeat (`idempotent=True` above). Otherwise recovery pauses for a decision by default,
  or fails, or tells the model the outcome is unknown, as you choose with `on_uncertain`
  ([Tool semantics: effect classes](durable-runs.md#tool-semantics-effect-classes)).
- You find and recover runs yourself (`list_incomplete()`). There is no engine that
  schedules workers, retries activities or wakes a run after a long wait, as Temporal does.

## What Nexura does not have

- **Durable execution engines.** No Temporal, DBOS, Prefect or other engine integration.
  Durability is the runner's own checkpoint store (memory, SQLite, or Postgres with
  `nexura-postgres`).
- **Capabilities and the Harness.** Pydantic AI 2 bundles instructions, tools, hooks and
  model settings into capabilities, with more of them in the separate Pydantic AI Harness
  package (memory, guardrails, context management, file system access and more). Nexura has no
  such unit. Tools, hooks, guardrails and settings are separate arguments on `Agent` and
  `Runner`.
- **Deferred external tool calls.** Nothing matches `CallDeferred` for a tool whose result
  comes from outside the run, such as a frontend or a background worker.
- **As many models and interfaces.** Nexura covers OpenAI, Anthropic, Gemini, Vertex AI,
  Sarvam and Ollama directly, and more through LiteLLM ([Models and providers](providers.md)).
  It has no realtime voice or image generation interface.
- **A hosted observability product.** Nexura exports OpenTelemetry spans to your own
  collector ([Observability](observability.md)). There is nothing like Logfire, which
  Pydantic AI's instrumentation sends traces to.
- **Maturity.** Nexura is alpha and unpublished, with no outside users yet. Pydantic AI has
  a large user base.

## Checklist

1. Move each `@agent.tool` and `@agent.tool_plain` function to module level with `@tool`,
   and list it in `tools=[...]`.
2. Give every agent a `name`. Drop `deps_type`; keep `RunContext[T]` on the tools.
3. Swap `run_sync` to `run` and `run` to `run_async`. Move `usage_limits=` to
   `Runner(usage_limits=...)`, and a `cost_limit` to a budget.
4. Replace deferred approvals with `requires_approval` and `runner.resume`. Store
   `interruption.to_json()` where you stored the message history before.
5. Replace `TestModel` and `FunctionModel` in tests with `FakeModel`.
6. If you used a durability capability, decide whether a checkpoint store and your own
   recovery loop are enough. If you depend on the engine's scheduling, keep that engine.
7. Compare answers on your own data with the real model before you switch.

## Sources

Read on 2026-10-20 from the Pydantic AI documentation, which now lives under
`pydantic.dev/docs/ai/`. Each fact used above was checked on these pages.

- [Agents](https://pydantic.dev/docs/ai/core-concepts/agent/): `Agent(model, deps_type,
  output_type, instructions)`, `run`, `run_sync`, `run_stream`, `result.output`,
  `@agent.tool` with `RunContext`, `@agent.tool_plain`, `UsageLimits(request_limit=...)`
  passed as `usage_limits=`, with `output_tokens_limit`, `tool_calls_limit` and
  `cost_limit`.
- [Deferred tools](https://pydantic.dev/docs/ai/tools-toolsets/deferred-tools/):
  `requires_approval=True`, `ApprovalRequired`, `CallDeferred`, `DeferredToolRequests` in
  `output_type`, `DeferredToolResults` with `approvals`, `ToolApproved(override_args=...)`,
  `ToolDenied(message)`, resuming with `message_history=` and `deferred_tool_results=`.
- [Testing](https://pydantic.dev/docs/ai/guides/testing/): `TestModel`, `FunctionModel`,
  `agent.override(model=...)`, `capture_run_messages()`.
- [Multi-agent applications](https://pydantic.dev/docs/ai/guides/multi-agent-applications/):
  delegation through tools with `usage=ctx.usage`, programmatic hand-off, graphs.
- [Durable execution with Temporal](https://pydantic.dev/docs/ai/capabilities/durable_execution/temporal/):
  the `TemporalDurability` capability, a Temporal workflow and server, and `TemporalAgent`
  deprecated for removal in v3. The sibling pages cover
  [DBOS](https://pydantic.dev/docs/ai/capabilities/durable_execution/dbos/) and
  [Prefect](https://pydantic.dev/docs/ai/capabilities/durable_execution/prefect/).
- [Pydantic AI v2 announcement](https://pydantic.dev/articles/pydantic-ai-v2) (2026-06-23):
  capabilities bundle instructions, tools, hooks and model settings; the separate
  `pydantic-ai-harness` package; instrumentation sends traces to Logfire.
