# Quickstart: an approval-gated workflow in 10 minutes

This page goes from an empty folder to a workflow that drafts a refund reply with an agent,
spends from a token and cost budget, stops for a human before it sends anything, and resumes
after a restart. Every Python block runs offline with `FakeModel`, a scripted model, so you
need no API key and no network after the install. The test suite runs each block.

Related: [Getting started](getting-started.md), [Human approval](approvals.md),
[Budgets and usage](budgets-and-usage.md), [Workflows](workflows.md).

To start from a generated project instead, run `nexura-runtime init my-app --template workflow`
(or `agent`, `rag`): it writes a runnable project with offline tests, and `nexura-runtime dev`
opens it in the local Studio. See [Project templates](project-templates.md).

## 1. Install (2 minutes)

Once Nexura is published, the install will be:

```bash
pip install nexura-runtime        # the platform; it installs nexura-agents too
```

Until then, install from the repository. You need [uv](https://docs.astral.sh/uv/) and
Python 3.12:

```bash
git clone https://github.com/Liceron/nexura
cd nexura
uv sync
uv run python -c "import nexura_runtime, nexura_agents; print(nexura_agents.__version__)"
```

Run the examples below with `uv run python quickstart.py` from the checkout. If you only
need agents, without workflows, `pip install -e packages/nexura-agents` works on Python 3.11
to 3.13 ([Packages and installation](packages.md)).

## 2. A first agent (1 minute)

An agent is a name, instructions, a model and optional tools. `FakeModel` returns its
scripted responses in order, one per model call.

```python
from nexura_agents import Agent, FakeModel

agent = Agent(
    name="Support",
    instructions="Answer order questions briefly.",
    model=FakeModel(responses=["Order ORD-1 shipped on Monday."]),
)
result = agent.run("Where is ORD-1?")
assert result.status == "completed"
print(result.output)
```

For a real model, pass a string such as `model="openai:gpt-4.1-mini"` and set that
provider's API key ([Models and providers](providers.md)). The rest of this page does not
change.

## 3. A tool that needs approval (2 minutes)

`@tool(requires_approval=True)` stops the run before the tool executes. The result has
`status == "paused"` and an `interruption` you can show to a reviewer, save as JSON and
resume later.

```python
from nexura_agents import Agent, FakeModel, Runner, ToolCall, tool

refunds = []


@tool(requires_approval=True)
def issue_refund(order_id: str, amount: int) -> str:
    """Refund an order."""
    refunds.append((order_id, amount))
    return f"refunded {amount} for {order_id}"


agent = Agent(
    name="Billing",
    instructions="Refund orders when asked.",
    model=FakeModel(
        responses=[
            [ToolCall(id="c1", name="issue_refund", arguments={"order_id": "ORD-1", "amount": 40})],
            "Refunded 40 for ORD-1.",
        ]
    ),
    tools=[issue_refund],
)
runner = Runner()

paused = runner.run(agent, "Refund ORD-1, 40")
assert paused.status == "paused" and refunds == []  # nothing ran yet
print(paused.interruption.pending[0].name, paused.interruption.pending[0].arguments)

done = runner.resume(agent, paused.interruption, approve=True)
assert done.output == "Refunded 40 for ORD-1." and refunds == [("ORD-1", 40)]
```

To decline, pass `reject={"c1": "reason"}` instead. The model then sees the reason as the
tool result.

## 4. A budget (2 minutes)

A budget reserves an estimate before each model call and refuses the call when the
estimate does not fit. After the call it records what the model reported.

`FakeModel` reports itself as the model `fake-model`, which has no price. A budget with a
money cap refuses a model with no price, so this example adds an example price for it. Use
your own contract prices for real models.

```python
from decimal import Decimal

from nexura_agents import (
    Agent,
    BudgetConfig,
    BudgetManager,
    FakeModel,
    ModelPricing,
    ModelSettings,
    Runner,
    Usage,
)
from nexura_agents.usage import BudgetExceededError

manager = BudgetManager()
manager.pricing_table.add_model_pricing(
    ModelPricing(
        model_id="fake-model",
        input_cost_per_million=Decimal("2.50"),
        output_cost_per_million=Decimal("10"),
        source="example price for the quickstart",
    )
)
budget = manager.create_run_budget(
    "local", "quickstart", config=BudgetConfig(max_tokens=600, max_cost_usd=Decimal("0.01"))
)

model = FakeModel(
    responses=["first", "second", "third"],
    default_usage=Usage(prompt_tokens=40, completion_tokens=20, total_tokens=60),
)
# A max_tokens setting gives a tighter estimate; without it the estimate assumes 4096
# output tokens and would not fit this small budget.
agent = Agent(
    name="Writer",
    instructions="Write one line.",
    model=model,
    model_settings=ModelSettings(max_tokens=200),
)
runner = Runner(budget=budget)

answers = []
try:
    for _ in range(3):
        answers.append(runner.run(agent, "Write a line").output)
except BudgetExceededError as exc:
    print("refused before the call:", exc.limit_type)

status = budget.get_status()
print(answers, status.total_tokens, status.total_spent_usd)
assert answers and len(model.calls) == len(answers)  # a refused call never reached the model
assert status.total_tokens == 60 * len(answers)
```

`UsageLimits` is the simpler per-run cap on requests and tokens. A budget can also be shared
by many runs and sub-agents, and it can cap money
([Budgets and usage](budgets-and-usage.md)).

## 5. An approval-gated workflow with a budget (3 minutes)

A workflow is a graph of named steps that share one JSON state. The engine saves a
checkpoint after each step. A step with `requires_approval=True` pauses before its handler
runs, and any engine on the same database can resume it later, as after a restart.

Here the `draft` step runs the agent under the budget, and the `send` step needs approval.
Save this as `quickstart.py`:

```python
import asyncio
import tempfile
from decimal import Decimal
from pathlib import Path

from nexura_agents import (
    Agent,
    BudgetConfig,
    BudgetManager,
    FakeModel,
    ModelPricing,
    ModelSettings,
    Runner,
    Usage,
)
from nexura_runtime.workflow import (
    ModuleRegistry,
    SQLiteWorkflowStore,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
)

# Budget: 2,000 tokens and 1 US cent for the whole workflow.
manager = BudgetManager()
manager.pricing_table.add_model_pricing(
    ModelPricing(
        model_id="fake-model",
        input_cost_per_million=Decimal("2.50"),
        output_cost_per_million=Decimal("10"),
        source="example price for the quickstart",
    )
)
budget = manager.create_run_budget(
    "local", "refund-reply", config=BudgetConfig(max_tokens=2000, max_cost_usd=Decimal("0.01"))
)
runner = Runner(budget=budget)

writer = Agent(
    name="Writer",
    instructions="Draft a short, polite refund reply.",
    model=FakeModel(
        responses=["Your refund for ORD-1 is on its way."],
        default_usage=Usage(prompt_tokens=40, completion_tokens=20, total_tokens=60),
    ),
    model_settings=ModelSettings(max_tokens=200),
)

registry = ModuleRegistry()
outbox: list[str] = []


@registry.register_handler("draft")
async def draft(state: dict) -> dict:
    result = await runner.run_async(writer, f"Customer asks about {state['order_id']}")
    return {"draft": result.output}


@registry.register_handler("send")
def send(state: dict) -> dict:
    outbox.append(state["draft"])  # stands in for an email or chat message
    return {"sent": True}


spec = WorkflowSpec(
    name="Refund reply",
    entry_node="draft",
    nodes={
        "draft": WorkflowNode(id="draft", handler="draft", default_transition="send"),
        "send": WorkflowNode(id="send", handler="send", requires_approval=True),
    },
    terminal_nodes=["send"],
)


async def main() -> None:
    db = Path(tempfile.mkdtemp()) / "workflows.sqlite3"

    async with SQLiteWorkflowStore(db) as store:
        paused = await WorkflowEngine(spec, registry, store=store).run({"order_id": "ORD-1"})
    assert paused.status == "paused" and outbox == []
    approval = paused.pending_approval
    print("waiting for approval of", approval.node_id, "with", approval.input_state["draft"])

    # Later, in a new process: a new store and engine on the same file.
    async with SQLiteWorkflowStore(db) as store:
        done = await WorkflowEngine(spec, registry, store=store).resume(
            paused.workflow_run_id, approval.approval_id, decision="approve"
        )
    assert done.status == "completed" and outbox == ["Your refund for ORD-1 is on its way."]

    status = budget.get_status()
    print(done.status, status.total_tokens, "tokens,", status.total_spent_usd, "USD")
    assert status.total_tokens == 60 and status.remaining_tokens == 1940


asyncio.run(main())
```

What happened:

1. `run` executed `draft`. The agent call was admitted by the budget, then settled at the
   60 tokens the model reported.
2. The engine reached `send`, saw `requires_approval=True` and saved a paused checkpoint
   with a `pending_approval`. The send handler had not run.
3. A new engine on the same SQLite file resumed the run with `decision="approve"`. It ran
   `send` and finished. `decision="reject"` would end the run with `status="rejected"`.

An approval can be used once, and it expires after `approval_ttl` seconds (one hour by
default). Passing `state_update={...}` to `resume` corrects the state first; a change to
what the gated step reads needs a fresh approval ([Approval gates](workflows.md#approval-gates)).

The budget object above lives in memory. To keep spending across restarts, create the
`BudgetManager` on a SQLite `UsageLedger` ([Budgets and usage](budgets-and-usage.md)).

## Next steps

- [Project templates](project-templates.md): `nexura-runtime init`, `dev` and
  `deploy --target docker` for a project of your own.
- [Testing your agents](testing.md): script tool calls and assert on results.
- [Workflows](workflows.md): branches, retries, `interrupt()` for free-form human input,
  history and fork, sub-workflows and parallel steps.
- [Durable runs](durable-runs.md): checkpoint agent runs and recover them after a crash.
- [Service and Studio](studio-and-service.md): run the same workflow behind the local REST
  API and review approvals in the browser.
- Coming from another framework: [LangGraph](migrating-from-langgraph.md),
  [Pydantic AI](migrating-from-pydantic-ai.md), [CrewAI](migrating-from-crewai.md),
  [OpenAI Agent Builder](migrating-from-openai.md).
