# Migrating from CrewAI

CrewAI has two building blocks. A **crew** is a set of agents, each with a role, a goal and a
backstory, working through tasks in a sequential or hierarchical process. A **flow** is
event-driven Python: methods marked `@start`, `@listen` and `@router` run as earlier methods
finish, over a shared state, with optional persistence and human feedback.

In Nexura:

- A crew maps to agents in `nexura_agents`: an `Agent` per role, and a
  [team](teams.md) (`SequentialAgent`, `ParallelAgent`, `LoopAgent`) or handoffs for the
  process.
- A flow maps to a [workflow](workflows.md) in `nexura_runtime.workflow`: named steps over
  one JSON state, saved after every step, with approval gates and `interrupt()` for people.

Everything on this page runs offline with `FakeModel` and the workflow engine. Nothing here
was checked against a live CrewAI install. The CrewAI code blocks are written from the CrewAI
documentation (read on 2026-10-20, see [Sources](#sources)). They need the `crewai` package
and a model key, so the test suite does not run them.

Related: [Agents](agents.md), [Teams](teams.md), [Workflows](workflows.md),
[Human approval](approvals.md).

## Concept map

### Crews

| CrewAI | Nexura | Notes |
| --- | --- | --- |
| `Agent(role=..., goal=..., backstory=..., tools=[...])` | `Agent(name=..., instructions=..., model=..., tools=[...])` | Write the role, goal and backstory into `instructions`. `model` is required. |
| `Task(description=..., expected_output=..., agent=...)` | The prompt you pass to `run`, or the member's `instructions` | There is no task object. Put the expected output in the prompt, or use `output_type`. |
| `Task(output_pydantic=Model)` | `Agent(output_type=Model)` | ([Structured output](structured-output.md)) |
| `Task(context=[earlier_task])` | `SequentialAgent` passes each member's output to the next | `handoff=fn` builds the next prompt yourself ([Teams](teams.md)). |
| `Task(guardrail=fn)` | `output_guardrails=[...]` or `output_validators=[...]` on the agent | A validator sends a failed answer back to the model to retry ([Output validators](structured-output.md#output-validators)). |
| `Task(async_execution=True)` | `ParallelAgent`, or a `parallel=` workflow step | |
| `Task(human_input=True)` | A workflow approval gate or `interrupt()` after the agent's step | See the flow example below. |
| `Crew(..., process=Process.sequential)` | `SequentialAgent("name", [agents])` | |
| `Process.hierarchical` with `manager_llm` or `manager_agent` | A lead `Agent` with `tools=[member.as_tool(), ...]` or `handoffs=[...]` | The lead model decides whom to call. Nexura does not plan or assign tasks for you. |
| `crew.kickoff(inputs={...})`, `akickoff(...)` | `team.run(prompt)`, `await team.run_async(prompt)` | Build the prompt from your inputs yourself. |
| `kickoff_for_each(inputs=[...])` | A loop, `ParallelAgent`, or a workflow `map_over` step | |
| `crew.usage_metrics` | `result.usage` on the team result | Agent runs also have `usage_by_agent` and `usage_by_model` ([Budgets and usage](budgets-and-usage.md)). |
| `Crew(max_rpm=...)` | `RateLimitedModel(model, RateLimiter(requests_per_minute=...))` | Share one limiter between agents to limit them together ([Models and providers](providers.md)). |
| `Crew(memory=...)` | [Long-term memory](memory.md) and [sessions](agents.md#sessions-and-conversation-memory) | Different design: facts per user namespace, recalled automatically. |

### Flows

| CrewAI | Nexura | Notes |
| --- | --- | --- |
| `class MyFlow(Flow[MyState])` with a Pydantic state | `WorkflowSpec(...)` over a dict state | No state model. State must be JSON-serializable. |
| `@start()` | `entry_node="..."` | One entry step. For several starts that run together, make the first step `parallel=[...]`. |
| `@listen(previous_method)` | `default_transition="next"` on the previous step | |
| `@router(method)` returning a label, and `@listen("label")` | `transitions={"label": "step"}`, and the handler returns `{"_branch": "label"}` | |
| `and_(a, b)` | A `parallel=["a", "b"]` step; the next step runs after both | Use `reducers=` when both write the same key. |
| `or_(a, b)` | No direct equivalent | A step has one next step per branch. Route each path to a shared step instead. |
| `@persist` (`SQLiteFlowPersistence` by default) | `store=SQLiteWorkflowStore(path)` | Every step is saved; there is no per-method opt-in. |
| `kickoff(inputs={"id": ...})` to resume | `engine.resume(...)` or `engine.resume_interrupt(...)` for a paused run, `engine.recover(run_id)` after a crash | ([Recovering after a crash](workflows.md#recovering-after-a-crash)) |
| `kickoff(restore_from_state_id=...)` to fork | `engine.fork(run_id, index, state_update=...)` | The fork is a new run; the source run does not change. |
| `@human_feedback(message=..., emit=[...], llm=...)` | `requires_approval=True` for approve or reject, or `interrupt()` for free-form feedback | CrewAI uses an LLM to turn free-form feedback into one of the `emit` outcomes. In Nexura your handler reads the answer and picks the branch. |
| `HumanFeedbackPending`, `Flow.from_pending(flow_id)`, `flow.resume(feedback)` | `result.status == "paused"`, then `engine.resume(...)` on any engine with the same store | |
| `flow.plot()` | `spec.to_mermaid()` | A Mermaid flowchart ([Diagrams](workflows.md#diagrams)). |

## Example: a sequential crew

CrewAI:

```python notest
from crewai import Agent, Crew, Process, Task

researcher = Agent(
    role="Researcher",
    goal="Find three facts about tides",
    backstory="You check your sources.",
)
writer = Agent(
    role="Writer",
    goal="Write a short post",
    backstory="You write plainly.",
)
research = Task(
    description="Research tides.", expected_output="Three facts", agent=researcher
)
post = Task(
    description="Write a post about tides.",
    expected_output="One paragraph",
    agent=writer,
    context=[research],
)
crew = Crew(agents=[researcher, writer], tasks=[research, post], process=Process.sequential)
result = crew.kickoff()
print(result)
print(crew.usage_metrics)
```

Nexura. Each role becomes an agent whose instructions hold the role, goal, backstory and
expected output. `SequentialAgent` passes the researcher's answer to the writer:

```python
from nexura_agents import Agent, FakeModel, SequentialAgent, Usage

usage = Usage(prompt_tokens=30, completion_tokens=10, total_tokens=40)
researcher = Agent(
    name="Researcher",
    instructions=(
        "You are a researcher. You check your sources. "
        "Find three facts about the topic. Reply with three short facts."
    ),
    model=FakeModel(  # use a real model such as "openai:gpt-4.1-mini" in production
        responses=["1. The moon pulls the sea. 2. Two highs a day. 3. Spring tides."],
        default_usage=usage,
    ),
)
writer = Agent(
    name="Writer",
    instructions="You are a writer. You write plainly. Turn the facts into one paragraph.",
    model=FakeModel(
        responses=["Tides follow the moon, with two highs a day."], default_usage=usage
    ),
)

crew = SequentialAgent("Tides post", [researcher, writer])
result = crew.run("Research tides and write a post about them.")

assert result.status == "completed"
assert result.output == "Tides follow the moon, with two highs a day."
assert [step.member for step in result.steps] == ["Researcher", "Writer"]
assert writer.model.calls[0]["messages"][-1].content.startswith("1. The moon")
assert result.usage.total_tokens == 80  # crew.usage_metrics
```

What changed:

- `role`, `goal`, `backstory` and `expected_output` are text in `instructions`. Nexura does
  not build a prompt from them for you.
- Tasks are gone. The team's prompt goes to the first member, and each later member gets
  the previous answer.
- If a member has a tool with `requires_approval=True`, the whole team pauses and resumes
  without re-running members that finished ([Approvals pass through](teams.md#approvals-pass-through)).

## Example: a flow with a router

CrewAI:

```python notest
from crewai.flow.flow import Flow, listen, router, start
from pydantic import BaseModel


class RefundState(BaseModel):
    amount: int = 0
    route: str = ""


class RefundFlow(Flow[RefundState]):
    @start()
    def load(self):
        self.state.amount = 5000

    @router(load)
    def triage(self):
        return "manual" if self.state.amount > 1000 else "auto"

    @listen("auto")
    def auto(self):
        self.state.route = "auto"

    @listen("manual")
    def manual(self):
        self.state.route = "manual"


flow = RefundFlow()
flow.kickoff()
print(flow.state.route)  # manual
flow.plot("refund_flow")
```

Nexura. The router becomes a step with `transitions`, and listeners become the steps those
transitions name:

```python
import asyncio

from nexura_runtime.workflow import ModuleRegistry, WorkflowEngine, WorkflowNode, WorkflowSpec

registry = ModuleRegistry()


@registry.register_handler("load")
def load(state: dict) -> dict:
    return {"amount": 5000}


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
    name="Refund flow",
    entry_node="load",
    nodes={
        "load": WorkflowNode(id="load", handler="load", default_transition="triage"),
        "triage": WorkflowNode(
            id="triage", handler="triage", transitions={"auto": "auto", "manual": "manual"}
        ),
        "auto": WorkflowNode(id="auto", handler="auto"),
        "manual": WorkflowNode(id="manual", handler="manual"),
    },
    terminal_nodes=["auto", "manual"],
)


async def main() -> None:
    result = await WorkflowEngine(spec, registry).run({})
    assert result.state["route"] == "manual"
    print(spec.to_mermaid())  # instead of flow.plot()


asyncio.run(main())
```

What changed:

- Methods on a class become handlers registered by name. A handler gets the state dict and
  returns the keys it changes; it does not set attributes on `self.state`.
- `@listen` becomes `default_transition` on the step before, and the router's labels
  become the step's `transitions`.
- Building the engine checks the spec, so a label with no step fails before the run.

## Example: human feedback with persistence

CrewAI pauses a flow for feedback through a provider that raises `HumanFeedbackPending`.
The flow state is saved, and you resume it later with `from_pending` and `resume`:

```python notest
from crewai.flow import (
    Flow,
    HumanFeedbackPending,
    HumanFeedbackProvider,
    PendingFeedbackContext,
    human_feedback,
    listen,
    start,
)


class ReviewQueue(HumanFeedbackProvider):
    def request_feedback(self, context: PendingFeedbackContext, flow: Flow) -> str:
        # Notify a reviewer here (chat, email, a ticket).
        raise HumanFeedbackPending(context=context, callback_info={"queue": "refunds"})


class ReplyFlow(Flow):
    @start()
    @human_feedback(
        message="Send this reply?",
        emit=["approved", "rejected"],
        llm="gpt-4o-mini",
        default_outcome="rejected",
        provider=ReviewQueue(),
    )
    def draft(self):
        return "Your refund for ORD-1 is on its way."

    @listen("approved")
    def send(self, result):
        print("sending", result.output)


result = ReplyFlow().kickoff()
if isinstance(result, HumanFeedbackPending):
    flow_id = result.context.flow_id

# Later, when the reviewer answers:
flow = ReplyFlow.from_pending(flow_id)
flow.resume("Yes, send it.")
```

In Nexura, the step that sends gets `requires_approval=True`. The paused run is in the
store, and any engine on the same store can resume it with `approve` or `reject`:

```python
import asyncio
import tempfile
from pathlib import Path

from nexura_runtime.workflow import (
    ModuleRegistry,
    SQLiteWorkflowStore,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
)

registry = ModuleRegistry()
outbox: list[str] = []


@registry.register_handler("draft")
def draft(state: dict) -> dict:
    return {"reply": f"Your refund for {state['order_id']} is on its way."}


@registry.register_handler("send")
def send(state: dict) -> dict:
    outbox.append(state["reply"])
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
    db = Path(tempfile.mkdtemp()) / "flows.sqlite3"

    async with SQLiteWorkflowStore(db) as store:
        engine = WorkflowEngine(spec, registry, store=store)
        first = await engine.run({"order_id": "ORD-1"})
        second = await engine.run({"order_id": "ORD-2"})
    assert first.status == second.status == "paused" and outbox == []
    print(first.pending_approval.input_state["reply"])  # what the reviewer sees

    # Later, in another process: a new engine on the same file.
    async with SQLiteWorkflowStore(db) as store:
        engine = WorkflowEngine(spec, registry, store=store)
        sent = await engine.resume(
            first.workflow_run_id, first.pending_approval.approval_id, decision="approve"
        )
        dropped = await engine.resume(
            second.workflow_run_id, second.pending_approval.approval_id, decision="reject"
        )

    assert sent.status == "completed" and dropped.status == "rejected"
    assert outbox == ["Your refund for ORD-1 is on its way."]


asyncio.run(main())
```

What changed:

- No provider class. The run returns `status == "paused"` with a `pending_approval`, and
  you notify the reviewer from your own code.
- The flow id becomes the run id, and `from_pending` plus `resume(feedback)` becomes
  `engine.resume(run_id, approval_id, decision=...)`.
- There is no LLM step that reads the feedback. For free-form feedback, call
  `interrupt({...})` in a handler, resume with `engine.resume_interrupt(...)`, and choose
  the next step from the answer in your own code
  ([Pausing for human input](workflows.md#pausing-for-human-input-with-interrupt)).
- An approval expires after `approval_ttl` seconds and can be used once.

## What Nexura does not have

- **Role-based agent prompts.** No `role`, `goal`, `backstory` or `expected_output` fields,
  and no prompt built from them. You write the instructions.
- **Tasks and automatic planning.** No task objects and no hierarchical manager that
  assigns tasks. A lead agent with sub-agents as tools is the closest pattern.
- **Typed flow state.** Workflow state is a JSON dict, not a Pydantic model.
- **`or_` listeners and several start methods.** A step has one entry and one next step per
  branch.
- **LLM-interpreted feedback.** Nothing like `@human_feedback(emit=..., llm=...)` that maps
  free-form text to an outcome, and no equivalent of its `learn` option.
- **Crew-level memory, knowledge and the CrewAI tool catalogue.** Nexura has its own
  [memory](memory.md), [knowledge](knowledge.md) and [connectors](connectors.md), with a
  different design. CrewAI tools do not plug in directly; MCP servers do
  ([MCP and OpenAPI tools](mcp-and-openapi.md)).
- **A hosted platform.** CrewAI's hosted platform has review assignment and email
  notification for paused flows. Nexura has a local service and Studio for one user on one
  machine ([Service and Studio](studio-and-service.md)).
- **Maturity.** Nexura is alpha and unpublished, with no outside users yet. CrewAI is
  widely used.

## Checklist

1. For each crew agent, write `instructions` from its role, goal and backstory, and add
   each task's expected output. Give it a `model`.
2. Replace a sequential crew with `SequentialAgent`, and a hierarchical crew with a lead
   agent that has the members as tools or handoffs.
3. For each flow, write a `WorkflowSpec`: one step per method, `default_transition` for
   `@listen`, `transitions` for `@router`, `parallel=` for `and_`.
4. Replace `@persist` with `SQLiteWorkflowStore`, and `@human_feedback` with approval gates
   or `interrupt()`. Store run ids where you stored flow ids.
5. Test with `FakeModel` and the in-memory store ([Testing your agents](testing.md)), then
   compare results on your own data with a real model before you switch.

## Sources

Read on 2026-10-20. Each fact used above was checked on these pages.

- [Flows](https://docs.crewai.com/en/concepts/flows): `Flow[State]` with a Pydantic state,
  `@start`, `@listen`, `@router` with labels, `and_`, `or_`, `kickoff`, `kickoff_async`,
  `state.id`, `@persist` with `SQLiteFlowPersistence` as the default, resume with
  `kickoff(inputs={"id": ...})`, fork with `kickoff(restore_from_state_id=...)`, `plot()`,
  and `@human_feedback` (CrewAI 1.8.0 or later).
- [Human feedback in flows](https://docs.crewai.com/en/learn/human-feedback-in-flows):
  `message`, `emit`, `llm`, `default_outcome`, `provider`, `learn`;
  `HumanFeedbackProvider.request_feedback`, `HumanFeedbackPending`, state saved
  automatically, `from_pending(flow_id)`, `resume()` and `resume_async()`.
- [Crews](https://docs.crewai.com/en/concepts/crews): `Crew(agents, tasks, process)`,
  `Process.sequential` (the default) and hierarchical with `manager_llm` or
  `manager_agent`, `kickoff(inputs=...)`, `kickoff_for_each`, `akickoff`, `memory`,
  `max_rpm`, `usage_metrics`.
- [Tasks](https://docs.crewai.com/en/concepts/tasks): `description`, `expected_output`,
  `agent`, `context`, `output_pydantic`, `async_execution`, `guardrail`, `human_input`
  ("have a human review the final answer of the agent").
- [Flow HITL management](https://docs-platform.crewai.com/platform/en/features/flow-hitl-management):
  the hosted platform pauses at a review point, emails the assignee and waits.
