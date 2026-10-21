---
name: nexura
description: Build, test and debug Python agents and approval-gated workflows with Nexura (nexura_agents, nexura_runtime). Use when code imports nexura_agents or nexura_runtime, or when the user asks for a Nexura agent, tool, approval, budget, durable run or workflow.
---

# Building with Nexura

Nexura is an MIT Python SDK. Two packages:

- `nexura_agents` (package `nexura-agents`, Python 3.11 to 3.13, needs only httpx and
  pydantic): `Agent`, `@tool`, `Runner`, teams, handoffs, structured output, approvals,
  budgets, durable runs, `FakeModel`.
- `nexura_runtime` (package `nexura-runtime`, Python 3.12): durable workflows in
  `nexura_runtime.workflow`, RAG, evaluations, the local REST service and Studio. It
  re-exports the agent SDK, but new code should import agents from `nexura_agents`.

The project is alpha and not on PyPI yet. Install from the repository:
`git clone https://github.com/Liceron/nexura && cd nexura && uv sync`. Once published,
`pip install nexura-runtime` (or `nexura-agents` for the SDK alone).

Before you use an API you are not sure about, look it up. Do not guess names.

- `docs/api-reference.md`: every public name with its signature.
- `docs/README.md`: the guide index. `llms-full.txt` has every guide in one file.

## Core API

```python
from pydantic import BaseModel

from nexura_agents import Agent, FakeModel, RunContext, Runner, ToolCall, tool


class Answer(BaseModel):
    sku: str
    units: int


@tool  # the schema comes from the type hints, the description from the docstring
def check_stock(ctx: RunContext[dict], sku: str) -> int:
    """Return units in stock for a SKU."""
    return ctx.deps[sku]  # ctx is not shown to the model


agent = Agent(
    name="Shop",  # required
    instructions="Answer stock questions with check_stock.",
    model=FakeModel(  # in production: model="openai:gpt-4.1-mini", "anthropic:...", ...
        responses=[
            [ToolCall(id="c1", name="check_stock", arguments={"sku": "A1"})],
            '{"sku": "A1", "units": 7}',
        ]
    ),
    tools=[check_stock],
    output_type=Answer,
)
result = Runner().run(agent, "How many A1?", deps={"A1": 7})
assert result.output == Answer(sku="A1", units=7)  # validated; result.final_output is raw text
assert result.status == "completed" and result.tool_calls_count == 1
```

- `agent.run(prompt)` is synchronous. Inside an event loop use
  `await agent.run_async(prompt)`.
- `Runner(...)` carries `usage_limits=`, `budget=`, `session_store=`, `checkpoint_store=`
  and hooks. `agent.run` is a shortcut for `Runner().run(agent, ...)`.
- Model strings: `openai:`, `anthropic:`, `gemini:` (or `google:`), `vertex:` (needs
  `nexura-vertex`), `sarvam:`, `ollama:`; `litellm:` with `nexura-agents[litellm]`.
- Multi-agent: `handoffs=[other]` (the other agent finishes the run), `other.as_tool()`
  (the caller keeps control), or `SequentialAgent`, `ParallelAgent`, `LoopAgent`.

## Approvals

`@tool(requires_approval=True)` (or a function `(ctx, args) -> bool`) pauses the run before
the tool executes.

```python
from nexura_agents import Agent, FakeModel, Runner, ToolCall, tool
from nexura_agents.types import RunInterruption

refunds = []


@tool(requires_approval=True)
def issue_refund(order_id: str, amount: int) -> str:
    """Refund an order."""
    refunds.append(order_id)
    return f"refunded {amount}"


def build() -> Agent:  # rebuild with the same name and tools to resume elsewhere
    return Agent(
        name="Billing",
        instructions="Refund when asked.",
        model=FakeModel(
            responses=[
                [ToolCall(id="c1", name="issue_refund", arguments={"order_id": "A1", "amount": 40})],
                "Refunded.",
            ]
        ),
        tools=[issue_refund],
    )


agent, runner = build(), Runner()
paused = runner.run(agent, "Refund A1")
assert paused.status == "paused" and refunds == []
reviewed = paused.interruption.pending_digests()  # keep what the reviewer saw
saved = paused.interruption.to_json()  # store server-side

done = runner.resume(agent, RunInterruption.from_json(saved), approve=reviewed)
assert done.output == "Refunded." and refunds == ["A1"]
```

- Decide every pending call exactly once: `approve=True`, a list of ids, or
  `{call_id: digest}`; `reject={call_id: "reason"}`; `ApproveWith(arguments=..., digest=...)`
  to approve with edits. Otherwise `ApprovalDecisionError`.
- If one call in a model response needs approval, the whole batch waits.
- Approvals expire (`Runner(approval_ttl=3600)`) and can be signed
  (`approval_signing_secret=`). The SDK does not record that a stored interruption was
  used; enforce single use in your storage, or use a checkpoint store.
- A sub-agent called through `as_tool()` cannot wait for approval: the call raises
  `ToolError`. Use a team or handoffs when members need approval.
- `Agent(ask_user=True)` lets the model ask the user a question; resume with `answers=`.

## Workflows

```python
import asyncio

from nexura_runtime.workflow import (
    MemoryWorkflowStore,
    ModuleRegistry,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
)

registry = ModuleRegistry()


@registry.register_handler("draft")
def draft(state: dict) -> dict:
    return {"reply": f"Refund for {state['order_id']} sent."}  # return only the updates


@registry.register_handler("send")
def send(state: dict) -> dict:
    return {"sent": True}


spec = WorkflowSpec(
    name="Reply",
    entry_node="draft",
    nodes={
        "draft": WorkflowNode(id="draft", handler="draft", default_transition="send"),
        "send": WorkflowNode(id="send", handler="send", requires_approval=True),
    },
    terminal_nodes=["send"],
)


async def main() -> None:
    engine = WorkflowEngine(spec, registry, store=MemoryWorkflowStore())  # SQLiteWorkflowStore(path)
    paused = await engine.run({"order_id": "A1"})
    assert paused.status == "paused" and "sent" not in paused.state
    done = await engine.resume(
        paused.workflow_run_id, paused.pending_approval.approval_id, decision="approve"
    )
    assert done.status == "completed" and done.state["sent"] is True


asyncio.run(main())
```

- State and updates must be JSON-serializable. A handler may be sync or async.
- Branch with `transitions={"a": "node_a", ...}` and return `{"_branch": "a"}`.
- `interrupt(value)` inside a handler pauses for free-form input; answer with
  `engine.resume_interrupt(run_id, interrupt_id, answer)`. The node runs again from the
  top, so put side effects after the `interrupt()` call and never catch `BaseException`
  around it.
- Without a store a run cannot pause. A `workflow_run_id` you pass must be new.
- `retry={"max_attempts": 3, ...}` per node; `max_steps` (default 50) stops cycles.
- `engine.history(run_id)` lists checkpoints oldest first; `engine.fork(...)` starts a new
  run from one; `engine.recover(run_id)` continues after a crash.

## Budgets and limits

- `Runner(usage_limits=UsageLimits(request_limit=..., total_tokens_limit=...))` checks after
  each response; the call that crosses the line has already happened.
- A budget (`BudgetManager().create_run_budget(workspace_id, run_id, config=BudgetConfig(...))`
  passed as `Runner(budget=...)`) reserves an estimate before each model call and refuses
  calls that do not fit (`BudgetExceededError` from `nexura_agents.usage`). It is admission
  control, not an invoice ceiling.
- Set `ModelSettings(max_tokens=...)`: without it the estimate assumes 4096 output tokens.
- A money cap refuses a model with no price. `FakeModel` is `fake-model` and has no price:
  add a `ModelPricing` to `manager.pricing_table` in tests, or use a token-only cap.
- `BudgetConfig.max_turns` and `max_tool_calls` are not enforced by `Runner`; use
  `Agent(max_turns=..., max_tool_calls=...)`.

## Durable agent runs

`Runner(checkpoint_store=SQLiteCheckpointStore(path))` saves the run at every turn. After a
crash, `await store.list_incomplete()` and `await runner.recover_async(agent, run_id)` with
the same agent definition. Declare tool effects: `@tool(effect="pure")` for reads,
`idempotent=True` for safe repeats. Undeclared tools are `non_idempotent` and are never
rerun silently; recovery pauses for a decision by default (`on_uncertain=`).

## Testing

- `FakeModel(responses=[...])`: each item is a reply string or a list of `ToolCall`s. When
  it runs out it replies `"FakeModel: No more canned responses."`, so a test with too few
  responses fails visibly. `handler=fn(messages, tools)` computes replies.
  `default_usage=Usage(...)` sets token counts. `model.calls` records each request.
- `with agent.override(model=FakeModel(...)):` swaps the model in tests only. In production
  use `agent.clone(...)`.
- Assert on `result.output`, `result.status`, `result.tool_calls_count`, `result.messages`
  and `result.usage`.
- Never call real providers in tests. Live checks need the user's own keys and are not part
  of the test suite.

## Common mistakes

- Leaving out `name` or `model` on `Agent`. Both are required.
- Calling `agent.run` from async code (it raises inside a running loop).
- Tools without type hints or a docstring: the model gets a poor schema.
- Resuming with a different agent name or tool set: `ApprovalDecisionError` or a recovery
  error.
- Treating `result.final_output` as the typed value. Use `result.output`.
- Putting side effects before `interrupt()` in a workflow handler.
- Retrying a non-idempotent step: engines refuse retries on modules declared
  `effect_policy="non_idempotent_write"`.
- Expecting `UsageLimits` to stop a call before it is made. Budgets do that; limits do not.
- Using the bare `nexura` command. That is a separate cloud client; this project's CLI is
  `nexura-runtime`.
- Assuming multi-user or public deployment. The open-source edition is single user on one
  machine.

## Docs

All under `docs/` in the repository: `quickstart-10-minutes.md`, `agents.md`, `tools.md`,
`approvals.md`, `workflows.md`, `budgets-and-usage.md`, `durable-runs.md`, `testing.md`,
`providers.md`, `teams.md`, `troubleshooting.md`, and migration guides from LangGraph,
Pydantic AI, CrewAI and OpenAI Agent Builder.
