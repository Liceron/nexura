# Migrating from LangGraph

LangGraph builds an agent or a workflow as a graph of nodes over a shared state, with a
checkpointer that saves the state after each step. Nexura splits this into two parts:

- **Workflows** (`nexura_runtime.workflow`): a graph of named steps over one JSON state, saved
  after every step. This is the part that maps to `StateGraph`.
- **Agents** (`nexura_agents`): the model and tool loop, with approvals, sessions and budgets.
  This maps to the agents you build with `create_agent` from `langchain.agents`, or to the
  agent nodes inside your graph.

Everything on this page runs offline with `FakeModel` and the workflow engine. Nothing here
was checked against a live LangGraph install. The LangGraph code blocks are written from the
LangGraph documentation (read on 2026-10-20, see [Sources](#sources)). They need the
`langgraph` package, so the test suite does not run them.

Related: [Workflows](workflows.md), [Human approval](approvals.md),
[Durable runs](durable-runs.md), [Agents](agents.md).

## Concept map

| LangGraph | Nexura | Notes |
| --- | --- | --- |
| `StateGraph(State)` with a `TypedDict` state | `WorkflowSpec(name, entry_node, nodes, terminal_nodes)` over a dict | Nexura has no state schema. State and updates must be JSON-serializable, or the step fails. |
| `builder.add_node("name", fn)` | `@registry.register_handler("name")` and `WorkflowNode(id=..., handler="name")` | A handler is a sync or async function that takes the state and returns a dict of updates, like a LangGraph node. |
| `add_edge(START, "a")`, `add_edge("a", "b")`, `END` | `entry_node="a"`, `default_transition="b"`, no next node | A run ends when a node has no next node. |
| `add_conditional_edges("a", route, {...})` | `transitions={...}` on the node, and the handler returns `{"_branch": "name"}` | The handler picks the branch. There is no separate routing function. |
| `Command(update=..., goto=...)` from a node | `HandlerOutput(state_update=..., branch=...)` | `branch` must be a key in the node's `transitions`. |
| Reducers, such as `Annotated[list[str], add]` | `reducers={"key": "extend"}` on a `parallel` or `map_over` node | Only those nodes combine values. Anywhere else an update replaces the key. Built in: `append`, `extend`, `sum`, `merge`, `max`, `min`. |
| `Send("node", {...})` fan-out | `map_over="items"` | Each item sees `item` and `item_index`. `max_concurrency` bounds it. |
| Several nodes running in the same step | `parallel=["h1", "h2"]` | All branches get the same state. Two branches writing one key without a reducer fail the step. |
| Subgraphs | `subgraph=<WorkflowSpec>` with `input_mapping` / `output_mapping` | Each sub-workflow run has its own history ([Sub-workflows](workflows.md#sub-workflows)). |
| `compile(checkpointer=InMemorySaver())` | `WorkflowEngine(spec, registry, store=MemoryWorkflowStore())` | Without a store, a run cannot pause or resume. |
| `SqliteSaver` (`langgraph-checkpoint-sqlite`), `PostgresSaver` | `SQLiteWorkflowStore(path)` | The open-source edition has no Postgres workflow store. |
| `config={"configurable": {"thread_id": ...}}` | `workflow_run_id=` on `run`, then the same id on `resume` | A LangGraph thread keeps its state across calls, for example chat history. A Nexura run id names one run, and `run` refuses an id that is already in the store. |
| `interrupt(value)`, resumed with `Command(resume=answer)` | `interrupt(value)`, resumed with `engine.resume_interrupt(run_id, interrupt_id, answer)` | Same rule in both: the node runs again from the top, so code before `interrupt()` runs again. A Nexura answer can be used once. |
| An approval node built on `interrupt()`, or static breakpoints | `WorkflowNode(requires_approval=True)` and `engine.resume(run_id, approval_id, decision=...)` | The approval expires (`approval_ttl`), can be used once, and is tied to the inputs the step will read ([Approval gates](workflows.md#approval-gates)). |
| `graph.get_state_history(config)` | `await engine.history(run_id)` | LangGraph lists newest first. Nexura lists oldest first. |
| `update_state(checkpoint_config, values)`, then `invoke(None, fork_config)` | `await engine.fork(run_id, index, state_update={...})` | The fork is a new run, and the source run does not change. Approval gates after the fork point ask again. |
| `invoke(None, checkpoint_config)` (replay) | `await engine.fork(run_id, index)` | Steps after the checkpoint run again in both. |
| `add_node(..., retry_policy=RetryPolicy(max_attempts=3))` | `WorkflowNode(retry={"max_attempts": 3})` | Nexura refuses a retry on a module declared `non_idempotent_write` ([Retries](workflows.md#retries)). |
| `recursion_limit` (default 1000 steps since 1.0.6) | `WorkflowEngine(max_steps=...)` (default 50) | Raise `max_steps` for long loops. |
| `graph.stream(...)` | `engine.stream(initial_state)` | Yields node started, completed and paused events ([Streaming progress](workflows.md#streaming-progress)). |
| `create_agent(model=..., tools=..., system_prompt=...)` | `Agent(name=..., model=..., tools=..., instructions=...)` | `name` is required. A model string such as `"openai:gpt-4.1-mini"` works in both. |
| `HumanInTheLoopMiddleware(interrupt_on={"write_file": True})` | `@tool(requires_approval=True)` | The run pauses before the tool; resume with `runner.resume(...)` ([Human approval](approvals.md)). |
| A checkpointer plus `thread_id` for chat history | `Runner(session_store=SQLiteSessionStore(path))` and `session_id=` | ([Sessions](agents.md#sessions-and-conversation-memory)) |

## Example: a graph with a branch

A LangGraph graph that routes large amounts to a manual step:

```python notest
from typing import TypedDict

from langgraph.graph import END, START, StateGraph


class State(TypedDict, total=False):
    amount: int
    route: str


def triage(state: State) -> dict:
    return {}


def route(state: State) -> bool:
    return state["amount"] > 1000


def auto(state: State) -> dict:
    return {"route": "auto"}


def manual(state: State) -> dict:
    return {"route": "manual"}


builder = StateGraph(State)
builder.add_node("triage", triage)
builder.add_node("auto", auto)
builder.add_node("manual", manual)
builder.add_edge(START, "triage")
builder.add_conditional_edges("triage", route, {False: "auto", True: "manual"})
builder.add_edge("auto", END)
builder.add_edge("manual", END)
graph = builder.compile()

print(graph.invoke({"amount": 5000})["route"])  # manual
```

In Nexura, the routing function and the triage node become one handler that returns a branch
name:

```python
import asyncio

from nexura_runtime.workflow import ModuleRegistry, WorkflowEngine, WorkflowNode, WorkflowSpec

registry = ModuleRegistry()


@registry.register_handler("triage")
def triage(state: dict) -> dict:
    return {"_branch": "manual" if state["amount"] > 1000 else "auto"}


@registry.register_handler("auto")
def auto(state: dict) -> dict:
    return {"route": "auto"}


@registry.register_handler("manual")
def manual(state: dict) -> dict:
    return {"route": "manual"}


spec = WorkflowSpec(
    name="Triage",
    entry_node="triage",
    nodes={
        "triage": WorkflowNode(
            id="triage", handler="triage", transitions={"auto": "auto", "manual": "manual"}
        ),
        "auto": WorkflowNode(id="auto", handler="auto"),
        "manual": WorkflowNode(id="manual", handler="manual"),
    },
    terminal_nodes=["auto", "manual"],
)


async def main() -> None:
    engine = WorkflowEngine(spec, registry)
    assert (await engine.run({"amount": 5000})).state["route"] == "manual"
    assert (await engine.run({"amount": 50})).state["route"] == "auto"


asyncio.run(main())
```

What changed:

- Nodes are registered by name in a `ModuleRegistry`, and the spec refers to them by name.
  The spec is plain data and can be saved as JSON ([JSON specs](workflows.md#json-specs)).
- `_branch` is removed from the update, so it never appears in the state.
- Building the engine checks the spec, so an unknown handler name fails before the run.

## Example: interrupt and resume

LangGraph pauses a node with `interrupt()` and resumes it with `Command(resume=...)` on the
same thread:

```python notest
from typing import TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt


class State(TypedDict, total=False):
    draft: str
    reply: str


def review(state: State) -> dict:
    reply = interrupt({"question": "Edit or approve this reply", "draft": state["draft"]})
    return {"reply": reply}


builder = StateGraph(State)
builder.add_node("review", review)
builder.add_edge(START, "review")
builder.add_edge("review", END)
graph = builder.compile(checkpointer=InMemorySaver())

config = {"configurable": {"thread_id": "ticket-7"}}
result = graph.invoke({"draft": "Refund in full"}, config=config)
print(result["__interrupt__"][0].value["question"])

final = graph.invoke(Command(resume="Refunded. Sorry for the wait!"), config=config)
print(final["reply"])
```

The Nexura version. The node also runs again from the top on resume, so `interrupt()`
returns the answer the second time:

```python
import asyncio

from nexura_runtime.workflow import (
    MemoryWorkflowStore,
    ModuleRegistry,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
    interrupt,
)

registry = ModuleRegistry()


@registry.register_handler("review")
def review(state: dict) -> dict:
    reply = interrupt({"question": "Edit or approve this reply", "draft": state["draft"]})
    return {"reply": reply}


spec = WorkflowSpec(
    name="Review",
    entry_node="review",
    nodes={"review": WorkflowNode(id="review", handler="review")},
    terminal_nodes=["review"],
)


async def main() -> None:
    engine = WorkflowEngine(spec, registry, store=MemoryWorkflowStore())
    paused = await engine.run({"draft": "Refund in full"}, workflow_run_id="ticket-7")
    assert paused.status == "paused"
    assert paused.interrupt["value"]["question"] == "Edit or approve this reply"

    done = await engine.resume_interrupt(
        "ticket-7", paused.interrupt["interrupt_id"], "Refunded. Sorry for the wait!"
    )
    assert done.state["reply"] == "Refunded. Sorry for the wait!"


asyncio.run(main())
```

What changed:

- The checkpointer becomes `store=`, and `thread_id` becomes `workflow_run_id`.
- `result["__interrupt__"][0]` becomes `paused.interrupt`, a dict with `interrupt_id`,
  `node_id` and `value`.
- `Command(resume=answer)` becomes `resume_interrupt(run_id, interrupt_id, answer)`. The
  answer is claimed once; a second answer to the same interrupt raises
  `ApprovalAlreadyClaimedError`.
- For a yes or no before a step, use an approval gate instead (next example).

## Example: approve a step, then fork from a checkpoint

A LangGraph approval node reads `interrupt()`'s answer and chooses the next node. In Nexura,
`requires_approval=True` on the risky step does that. The engine pauses before the handler
runs, and the approval is tied to the state the step will read.

History and fork replace `get_state_history` and `update_state` followed by
`invoke(None, fork_config)`:

```python notest
history = list(graph.get_state_history(config))  # newest first
before = next(s for s in history if s.next == ("tax",))
fork_config = graph.update_state(before.config, values={"price": 100})
print(graph.invoke(None, fork_config)["total"])
```

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
refunds: list[str] = []


@registry.register_handler("price")
def price(state: dict) -> dict:
    return {"price": state["qty"] * 10}


@registry.register_handler("tax")
def tax(state: dict) -> dict:
    return {"total": state["price"] + state["price"] // 10}


@registry.register_handler("refund")
def refund(state: dict) -> dict:
    refunds.append(state["order_id"])
    return {"refunded": state["total"]}


spec = WorkflowSpec(
    name="Quote and refund",
    entry_node="price",
    nodes={
        "price": WorkflowNode(id="price", handler="price", default_transition="tax"),
        "tax": WorkflowNode(id="tax", handler="tax", default_transition="refund"),
        "refund": WorkflowNode(id="refund", handler="refund", requires_approval=True),
    },
    terminal_nodes=["refund"],
)


async def main() -> None:
    engine = WorkflowEngine(spec, registry, store=MemoryWorkflowStore())
    paused = await engine.run({"order_id": "ORD-1", "qty": 2}, workflow_run_id="q-1")
    assert paused.status == "paused" and refunds == []
    assert paused.pending_approval.input_state["total"] == 22

    done = await engine.resume(
        "q-1", paused.pending_approval.approval_id, decision="approve"
    )
    assert done.state["refunded"] == 22 and refunds == ["ORD-1"]

    # History is oldest first. Checkpoint 0 was saved after "price", before "tax".
    history = await engine.history("q-1")
    assert history[0].current_node_id == "tax"

    # Fork: run "tax" again with a new price. The approval gate asks again.
    fork = await engine.fork("q-1", 0, state_update={"price": 100}, new_run_id="q-1-what-if")
    assert fork.status == "paused" and fork.pending_approval.input_state["total"] == 110
    assert refunds == ["ORD-1"]  # the fork did not reuse the first approval


asyncio.run(main())
```

What changed:

- `get_state_history` returns `StateSnapshot`s newest first. `engine.history` returns
  `WorkflowCheckpoint`s oldest first, each with `state`, `current_node_id` (the next step)
  and `status`.
- `update_state` plus `invoke(None, ...)` is one call, `engine.fork(...)`. It returns the
  new run's result.
- An approval granted in the source run does not carry over to the fork.

## Example: an agent with a tool that needs approval

`create_agent` with `HumanInTheLoopMiddleware` pauses before chosen tools:

```python notest
from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langgraph.checkpoint.memory import InMemorySaver


def issue_refund(order_id: str) -> str:
    """Refund an order."""
    return f"refunded {order_id}"


agent = create_agent(
    model="openai:gpt-4.1-mini",
    tools=[issue_refund],
    system_prompt="Refund orders when asked.",
    middleware=[HumanInTheLoopMiddleware(interrupt_on={"issue_refund": True})],
    checkpointer=InMemorySaver(),
)
```

In Nexura the approval is set on the tool. The pause can be saved as JSON and resumed in
another process ([Human approval](approvals.md)):

```python
from nexura_agents import Agent, FakeModel, Runner, ToolCall, tool
from nexura_agents.types import RunInterruption

refunds = []


@tool(requires_approval=True)
def issue_refund(order_id: str) -> str:
    """Refund an order."""
    refunds.append(order_id)
    return f"refunded {order_id}"


agent = Agent(
    name="Billing",
    instructions="Refund orders when asked.",
    model=FakeModel(
        responses=[
            [ToolCall(id="c1", name="issue_refund", arguments={"order_id": "ORD-1"})],
            "Refunded ORD-1.",
        ]
    ),
    tools=[issue_refund],
)
runner = Runner()

paused = runner.run(agent, "Refund ORD-1")
assert paused.status == "paused" and refunds == []
saved = paused.interruption.to_json()  # store it anywhere

done = runner.resume(agent, RunInterruption.from_json(saved), approve=True)
assert done.output == "Refunded ORD-1." and refunds == ["ORD-1"]
```

## What Nexura does not have

- **Typed graph state.** State is a JSON dict. There is no `TypedDict` or Pydantic state
  schema and no per-key reducer on ordinary steps; reducers apply only to parallel and
  `map_over` steps.
- **A functional API.** There is no equivalent of `@entrypoint` and `@task`. Steps are
  registered handlers named in a spec, so a function-style LangGraph workflow has to be
  split into steps.
- **Interrupts in parallel work.** `interrupt()` is not allowed inside parallel branches or
  `map_over` items. LangGraph can resume several parallel interrupts in one call.
- **Threads that span many calls.** A workflow run id is one run. For multi-turn chat, use
  agent sessions instead.
- **A Postgres workflow store** in the open-source edition. Workflows store checkpoints in
  memory or SQLite. Agent runs have a Postgres checkpoint store in `nexura-postgres`.
- **A hosted deployment and debugging product** like LangGraph's platform and LangSmith.
  Nexura has a local REST service and Studio for one user on one machine; the team and
  public-endpoint editions are separate ([Service and Studio](studio-and-service.md)).
- **The LangChain integrations.** Nexura has its own providers, MCP and OpenAPI tools and
  15 connectors. LangChain models, retrievers and tools do not plug in directly.
- **Maturity.** Nexura is alpha and unpublished, with no outside users yet. LangGraph is
  widely used.

## Checklist

1. List each graph's nodes, edges and conditional edges. Write a `WorkflowSpec` for it and
   register each node function as a handler that returns a dict of updates.
2. Turn routing functions into `_branch` returns, and reducers into `parallel` or
   `map_over` steps with `reducers=`.
3. Replace the checkpointer with `SQLiteWorkflowStore`, and `thread_id` with a run id you
   store with your own records.
4. Keep `interrupt()` where you ask for free-form input. Use `requires_approval=True` where
   the answer is yes or no before a step.
5. Move agents to `nexura_agents.Agent`, and tools that need a human to
   `@tool(requires_approval=True)`.
6. Test with `FakeModel` and the in-memory store ([Testing your agents](testing.md)), then
   compare results on your own data with a real model before you switch.

## Sources

Read on 2026-10-20. Each fact used above was checked on these pages.

- [Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts): `interrupt()`
  needs a checkpointer and a `thread_id`; resume with `Command(resume=...)`; the node
  restarts from the beginning; `result["__interrupt__"]`; parallel interrupts resumed by id;
  the imports used above.
- [Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api): `StateGraph`,
  `TypedDict` state, reducers with `Annotated`, `add_node`, `add_edge`, `START`, `END`,
  `add_conditional_edges`, `compile(checkpointer=...)`, `Send`, `Command`, and the default
  `recursion_limit` of 1000 since version 1.0.6.
- [Persistence](https://docs.langchain.com/oss/python/langgraph/persistence) and
  [Checkpointers](https://docs.langchain.com/oss/python/langgraph/checkpointers):
  `InMemorySaver`, `SqliteSaver` in `langgraph-checkpoint-sqlite`, `PostgresSaver`,
  `get_state`, `get_state_history`, `update_state`.
- [Time travel](https://docs.langchain.com/oss/python/langgraph/use-time-travel): history is
  newest first; replay with `invoke(None, checkpoint_config)`; fork with `update_state` then
  `invoke(None, fork_config)`; interrupts trigger again on replay.
- [Fault tolerance](https://docs.langchain.com/oss/python/langgraph/fault-tolerance):
  `RetryPolicy` from `langgraph.types`, passed as `retry_policy=` to `add_node`.
- [Functional API](https://docs.langchain.com/oss/python/langgraph/functional-api):
  `@entrypoint` and `@task` from `langgraph.func`.
- [LangChain agents](https://docs.langchain.com/oss/python/langchain/agents): `create_agent`
  from `langchain.agents`, `HumanInTheLoopMiddleware(interrupt_on=...)`, a checkpointer for
  `thread_id` history.
