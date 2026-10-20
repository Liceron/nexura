# Budgets and usage

Every run reports how many tokens it used, and you can cap a run (or a whole workspace) in tokens and money. This page covers the `Usage` object on a run result, how usage adds up across turns and sub-agents, and `RunBudget`, the budget object that provider models check before and after each call. The same classes are available from `nexura_agents` and from `nexura_runtime`.

## Usage on a run result

```python
from nexura_agents import Agent, FakeModel, Usage

model = FakeModel(
    responses=["Hello!"],
    default_usage=Usage(prompt_tokens=12, completion_tokens=5, total_tokens=17),
)
result = Agent(name="Bot", instructions="Hi.", model=model).run("Say hello")

assert result.usage.prompt_tokens == 12
assert result.usage.completion_tokens == 5
assert result.usage.total_tokens == 17
assert result.usage.is_complete
```

`RunResult.usage` is the sum over all model calls in the run. `Usage` is a frozen dataclass with these fields:

| Field | Meaning |
| --- | --- |
| `prompt_tokens` | Input tokens. |
| `completion_tokens` | Output tokens. |
| `total_tokens` | Total as reported by the provider. |
| `is_complete` | `False` if any turn did not report all three counts above. |
| `cached_prompt_tokens` | Input tokens served from a cache, if reported. |
| `cache_creation_input_tokens` | Tokens written to a cache (Anthropic style), if reported. |
| `cache_read_input_tokens` | Tokens read from a cache (Anthropic style), if reported. |
| `reasoning_tokens` | Hidden "thinking" tokens, if reported. |

`reasoning_tokens` is informational. Providers bill thinking as output, so these tokens are already inside `completion_tokens`. Do not add them again.

Every field except `is_complete` is `None` when the provider did not report it. Nexura never turns an unknown count into zero. `is_reported` is `True` if at least one token metric is not `None`.

## Adding usage up

`Usage.add(other)` returns a new `Usage`. It sums each field, keeps `None` only if both sides are `None`, and sets `is_complete` to `False` if either side is incomplete. Passing `None` (a turn with no usage at all) also marks the result incomplete.

```python
from nexura_agents import Usage

first = Usage(prompt_tokens=10, completion_tokens=20, total_tokens=30, reasoning_tokens=8)
second = Usage(prompt_tokens=5, completion_tokens=5, total_tokens=10)

both = first.add(second)
assert both.total_tokens == 40
assert both.reasoning_tokens == 8  # only one side reported it
assert both.is_complete

missing = both.add(None)  # a turn where the provider reported nothing
assert missing.total_tokens == 40
assert not missing.is_complete

partial = Usage(total_tokens=7)  # no prompt/completion split
assert not first.add(partial).is_complete
assert first.add(partial).total_tokens == 37
```

When you see `is_complete=False`, treat the totals as a lower bound.

### Sub-agents

An agent used as a tool (`Agent.as_tool()`) adds its usage to the calling run, so one `RunResult.usage` covers the whole tree. If you write a tool that does model work itself, call `ctx.add_usage(usage)` with the `Usage` you measured.

```python
from nexura_agents import Agent, FakeModel, RunContext, Runner, ToolCall, Usage, tool


@tool
def summarize(ctx: RunContext[None], text: str) -> str:
    """Summarize text with a helper model."""
    ctx.add_usage(Usage(prompt_tokens=100, completion_tokens=20, total_tokens=120))
    return text[:5]


step = Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
model = FakeModel(
    responses=[
        [ToolCall(id="1", name="summarize", arguments={"text": "long document"})],
        "Summary ready",
    ],
    default_usage=step,
)
agent = Agent(name="Lead", instructions="Summarize.", model=model, tools=[summarize])
result = Runner().run(agent, "Summarize it")

# two model calls (15 each) plus the helper's 120
assert result.usage.total_tokens == 150
```

During a run, `ctx.usage` on the `RunContext` holds the totals so far.

### Usage per agent and per model

`result.usage_by_agent` and `result.usage_by_model` split the run's model usage by the
agent that made each call and by model, which is useful when handoffs mix a small routing
model with a large specialist:

```python
from nexura_agents import Agent, FakeModel, Runner, ToolCall
from nexura_agents.types import Usage

small = Usage(prompt_tokens=10, completion_tokens=2, total_tokens=12)
large = Usage(prompt_tokens=100, completion_tokens=20, total_tokens=120)
billing = Agent(
    name="Billing", instructions="i", model=FakeModel(responses=["paid"], default_usage=large)
)
triage = Agent(
    name="Triage",
    instructions="i",
    model=FakeModel(
        responses=[[ToolCall(id="h", name="transfer_to_billing", arguments={})]],
        default_usage=small,
    ),
    handoffs=[billing],
)
result = Runner().run(triage, "pay")
assert result.usage_by_agent["Triage"].total_tokens == 12
assert result.usage_by_agent["Billing"].total_tokens == 120
assert result.usage.total_tokens == 132
```

- Models are keyed by their `model` name (for example `gpt-4.1-mini`), or by class name when
  they have none.
- The breakdown covers this run's own model calls. Usage of an agent used as a tool is
  added to the total (`result.usage`), as described above, but not split here.
- It is kept across approval pauses (including `to_json`/`from_json`) and durable recovery.

### Tags and metadata

`run(..., tags=["support", "eu"], metadata={"customer": "c-9"})` labels a run. Tags are on
`ctx.tags` and `result.tags`; metadata starts `ctx.metadata`, where tools and hooks can read
it. With tracing on, the run span gets `nexura.run.tags`, and plain metadata values
(strings, numbers, booleans) become `nexura.run.metadata.<key>` attributes. Tags must be a
list of strings.

## Budgets

`UsageLimits` ([Guardrails, hooks and limits](guardrails-hooks-limits.md)) caps tokens and requests only. A budget is the tool for money and for sharing a cap between runs.

- `BudgetConfig`: the limits. All are optional: `max_cost_usd` (a `Decimal`), `max_tokens`, `max_turns`, `max_tool_calls`. Other fields: `soft_limit_percent` (default `0.80`), `conservative_unreported_tokens` (default 1000), `conservative_unreported_cost_usd` (default `0.05`) and `deny_unknown_pricing` (default `True`).
- `BudgetManager`: creates budgets, holds a `PricingTable` and a ledger of what was spent. By default it uses `create_operator_standard_pricing_table()`, a dated table with a few reference models such as `gpt-4o`. Check the prices against your own contract; supply your own `PricingTable` for anything you bill from.
- `RunBudget`: the budget for one run, from `manager.create_run_budget(workspace_id, run_id, config=..., default_model=...)`.

Provider calls go through two steps. `admit_provider_call` reserves an estimated amount before the call; if that would break a limit it raises. After the call, `settle_provider_call` replaces the reservation with a cost commitment using validated usage and the reservation's recorded model, regardless of the budget's default model or a conflicting settlement model argument.

### What is guaranteed

The shared budget is an **admission allowance with reservations**. Before dispatch,
admission atomically checks committed usage plus all outstanding reservations plus
the new estimate against every configured token and monetary allowance in scope.
Parallel agents, sub-agents and application retries consume the same allowance
when they use the same `RunBudget`, or descendants created with
`create_child_budget`. Provider settlements and direct `record_usage` on descendants
at every depth count against their ancestors. `is_exhausted()` checks all ancestor
and workspace caps; `remaining_tokens()` returns the smallest scoped remainder.
Each retry must pass admission again. `UsageLimits` is a separate per-run,
post-response check; it is not a shared reservation ledger.

The in-memory ledger serializes threads and async tasks sharing that ledger with
a threading lock, including threads running independent event loops.
SQLite serializes admission and settlement transactions across separate handles
and processes using the same database file. Handles must use the same workspace,
scope IDs and configured policies; policy objects and parent-budget registrations
must be recreated in each manager after restart. SQLite persists usage,
reservation identity and ancestry, not the managers' policy registry. Separate
in-memory ledgers do not share an allowance.
`create_run_budget` retrieves the original handle for an existing run ID and retains
its original config and default model, even if later arguments conflict. A different
workspace or explicit parent is rejected; repeated creation does not reset caps.

- A known pre-dispatch cancellation releases the unused reservation without charge.
  `release` is only for work known not to have been dispatched.
- An in-flight cancellation, timeout or caught provider error settles once using
  at least the reserved cost/tokens and the configured conservative fallback.
  Unpriced models settle without a pricing lookup failure, retaining recorded
  reservation cost/tokens and the largest applicable monetary fallback. Their
  cost remains an estimate even when token counts were reported. Pre-dispatch
  release also works without known pricing and cancellation preserves CancelledError.
  It does not refund work whose billing outcome is uncertain.
- Abrupt worker/process death cannot run cleanup. With SQLite, the outstanding
  reservation remains charged against admission after restart, even after its
  timeout. Recovery must settle it with observed usage or conservative cancellation;
  elapsed time alone never restores allowance. An in-memory ledger cannot survive
  process death. An unresolved durable reservation is a retained commitment, not
  an automatically released lease.
- Settlement is idempotent by reservation ID: concurrent or contradictory repeats
  return the first persisted amount/state and do not add usage again. A SQLite
  transaction updates the reservation and its settlement entry together.
  Positive monetary commitments round upward to USD 0.00000001 for both reservation
  and settlement, including direct usage; tiny positive costs never round to zero.
- Missing provider usage stays explicitly `unknown`; partial usage is `estimated`.
  Unknown/partial settlement retains the larger of the reserved monetary estimate
  and the computed cost. Missing usage or unknown pricing uses the applicable
  configured fallback for that computation; partial reports use the pricing
  calculator's partial-usage policy. Its token commitment
  is the maximum of the reservation, reported total or prompt/completion sum and
  `conservative_unreported_tokens`. These are policy commitments, not fabricated
  provider observations. Prompt/completion counts remain absent when unreported.
  Complete prompt/completion counts can supply their sum when total is omitted.
  For unpriced models, the recorded reservation supplies the token fallback.
  Negative token metrics are rejected by `TokenUsage`; invalid SDK provider reports
  are discarded during settlement and settled as unknown usage. An inconsistent
  total below the prompt/completion sum, or an all-zero report for reserved work,
  retains conservative cost/tokens rather than refunding known commitments.
  A monetary scope with `deny_unknown_pricing=True` refuses an unpriced model through
  admission, public `reserve`/`reserve_async` and the usage module's `reserve` op;
  a child cannot override an ancestor's refusal or reduce its monetary fallback.

This is **not a provider-invoice ceiling**. Reported usage can exceed the admission
estimate; settlement records it even when that exceeds the allowance, and subsequent
admissions are refused. Provider pricing changes, unobserved billing and retries
hidden inside a custom transport cannot be bounded by the ledger. Every billable
attempt must use the admission boundary; disable hidden retries or explicitly admit
each attempt. Custom models/tools that bypass that boundary have no shared-budget
guarantee. In particular, `FakeModel` does not automatically check a budget.
For `Agent.as_tool()`, pass a `Runner(budget=shared_or_child_budget)` explicitly;
its default independent runner does not inherit the caller's budget.

Run `uv run --frozen python examples/budget_parallel_demo.py` for ten offline
FakeModel agents sharing an explicitly reserved allowance. It prints spent,
reserved, remaining and refused counts before and after settlement. Turn and
tool-call limits remain per-handle counters; the shared admission guarantee here
covers tokens and monetary estimates.

```python
from decimal import Decimal

from nexura_agents import BudgetConfig, BudgetManager, Usage
from nexura_agents.usage import BudgetExceededError, InvalidPricingError

manager = BudgetManager()
budget = manager.create_run_budget(
    "workspace-1",
    "run-1",
    config=BudgetConfig(max_cost_usd=Decimal("0.01"), max_tokens=5000),
    default_model="gpt-4o",
)

reservation = budget.admit_provider_call("gpt-4o", estimated_tokens=1000)
cost = budget.settle_provider_call(
    reservation,
    usage=Usage(prompt_tokens=1000, completion_tokens=500, total_tokens=1500),
    model_id="gpt-4o",
)
assert cost.amount_usd == Decimal("0.00750000")  # 1000 in at $2.50/M, 500 out at $10/M
assert not cost.is_estimate

status = budget.get_status()
assert status.total_tokens == 1500
assert status.remaining_tokens == 3500
assert status.total_spent_usd == Decimal("0.0075")
assert not status.is_exhausted

# The next call would reserve more than the 0.01 USD left, so it is refused up front.
try:
    budget.admit_provider_call("gpt-4o", estimated_tokens=1000)
except BudgetExceededError as exc:
    assert exc.limit_type == "cost"
else:
    raise AssertionError("expected BudgetExceededError")

# Under a money cap, a model with no known price is refused too.
try:
    budget.admit_provider_call("model-without-a-price", estimated_tokens=10)
except InvalidPricingError:
    pass
else:
    raise AssertionError("expected InvalidPricingError")
```

`get_status()` returns a `BudgetStatus` with `total_spent_usd`, `total_reserved_usd`, `effective_cost_usd` (spent plus reserved), `total_tokens` (settled policy commitments), `total_reserved_tokens`, `total_turns`, `total_tool_calls`, the matching `remaining_*` values (`None` when no limit is set), `is_exhausted`, `soft_limit_reached` and `unknown_usage_count`. Remaining tokens subtract both settled and reserved tokens. `budget.to_dict()` gives the same information as plain strings and numbers.

Costs use `Decimal`. If the provider reports no usage, settlement retains the reservation and conservative fallback commitments and marks usage as unknown. This works for unpriced models too. A zero monetary commitment remains possible when the reservation and every applicable configured fallback are explicitly zero; missing usage is never treated as evidence of a free call.

`budget.record_turn()` and `budget.record_tool_call()` raise `BudgetExceededError` once `max_turns` or `max_tool_calls` is reached. Nothing in `Runner` or the provider models calls them, so `BudgetConfig.max_turns` and `max_tool_calls` are enforced only if your own code calls these methods. For agent runs, use `Agent(max_turns=..., max_tool_calls=...)` instead.

### Budgets across runs and sub-runs

Set a workspace envelope with `manager.set_workspace_budget(workspace_id, BudgetConfig(...))`; every run budget in that workspace is also checked against it. `budget.create_child_budget(child_run_id, config)` creates a budget linked to its parent, so a sub-agent counts against the parent's limits. `manager.get_workspace_status(workspace_id)` gives the totals for the workspace.

## Using a budget with a runner

Give the budget to the runner. Provider models (such as `OpenAIChatModel`) then admit and settle every call against it and stop when a limit is reached. This needs a real provider model, so the example is not run in the tests.

```python notest
from decimal import Decimal

from nexura_agents import Agent, BudgetConfig, BudgetManager, Runner

manager = BudgetManager()
budget = manager.create_run_budget(
    "workspace-1",
    "run-1",
    config=BudgetConfig(max_cost_usd=Decimal("0.50")),
    default_model="gpt-4o-mini",
)
agent = Agent(name="Bot", instructions="Be brief.", model="openai:gpt-4o-mini")
result = Runner(budget=budget).run(agent, "Hello")
print(budget.get_status().total_spent_usd)
```

The runner passes the budget to the model inside an `ExecutionContext`, the per-call object defined in `nexura_agents.contracts` (also exported from `nexura_runtime`). It carries `workspace_id`, `run_id`, `node_id`, an optional `deadline`, cancellation, and the `budget`. The runner builds one for each model call; you create one yourself only when you call a provider model directly or write your own module.

`FakeModel` does not read the budget, so a `Runner(budget=...)` with `FakeModel` records nothing. To test budget behaviour offline, call `admit_provider_call` and `settle_provider_call` directly as above.

Two caveats from the source:

- The conversion from `Usage` to the budget's own `TokenUsage` model does not carry `reasoning_tokens`. Reasoning tokens are already part of `completion_tokens`, so cost is unaffected.
- `RunBudget` keeps its turn and tool-call counters in memory. A `RunInterruption` saves usage and the request count ([Human approval](approvals.md)), not the budget object. Recreate the budget when you resume.

## See also

- [Guardrails, hooks and limits](guardrails-hooks-limits.md): `UsageLimits` for tokens and requests.
- [Human approval](approvals.md): how `requests` and usage survive a pause.
- [Models and providers](providers.md)
- [Workflows](workflows.md)
- [Testing your agents](testing.md)
