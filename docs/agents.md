# Agents

An `Agent` is configuration: a name, instructions, a model, tools and limits. It holds no
conversation state, so one agent object can serve many runs at once. This page covers
instructions, dependencies, conversation memory, multi-agent patterns and streaming.

```python
from nexura_agents import Agent, FakeModel

agent = Agent(
    name="Support",
    instructions="You are a support agent. Be brief.",
    model=FakeModel(responses=["Try restarting it."]),
    max_turns=5,
)
result = agent.run("My router is down.")
assert result.output == "Try restarting it."
```

Useful constructor arguments besides the first three: `tools`, `output_type`,
`output_retries` (default 1), `max_turns` (default 10), `max_tool_calls` (default 20),
`timeout`, `input_guardrails`, `output_guardrails`, `parallel_tool_calls` and `description`.
Bad values raise `SDKValidationError` when you create the agent, not mid-run.

## Instructions

Instructions can be a string or a function of the `RunContext`. The function is called at the
start of each run, so the prompt can depend on `deps`.

```python
from dataclasses import dataclass

from nexura_agents import Agent, FakeModel, RunContext


@dataclass
class User:
    name: str
    plan: str


def instructions(ctx: RunContext[User]) -> str:
    return f"You help {ctx.deps.name}, who is on the {ctx.deps.plan} plan."


model = FakeModel(responses=["Hi Asha."])
agent = Agent(name="Helper", instructions=instructions, model=model)
agent.run("hello", deps=User("Asha", "pro"))

system = model.calls[0]["messages"][0]
assert system.role == "system"
assert system.content == "You help Asha, who is on the pro plan."
```

The function can also be `async`, for example to load the user's profile from a
database; the runner awaits it. It must return a string. Calling
`agent.render_instructions(ctx)` yourself works only for sync functions; for async ones use
`await agent.render_instructions_async(ctx)`.

## Dependencies with RunContext

`deps` is any object you pass to `run(..., deps=...)`: a database handle, an HTTP client, the
current user. A tool receives it by declaring a first parameter annotated `RunContext[T]`.
That parameter is not shown to the model.

```python
from dataclasses import dataclass, field

from nexura_agents import Agent, FakeModel, RunContext, ToolCall, tool


@dataclass
class Shop:
    stock: dict[str, int]
    log: list[str] = field(default_factory=list)


@tool
def check_stock(ctx: RunContext[Shop], sku: str) -> int:
    """Return units in stock for a SKU."""
    ctx.deps.log.append(sku)
    return ctx.deps.stock[sku]


model = FakeModel(
    responses=[[ToolCall(id="1", name="check_stock", arguments={"sku": "A1"})], "7 left."]
)
agent = Agent(name="Shop", instructions="", model=model, tools=[check_stock])
shop = Shop(stock={"A1": 7})

result = agent.run("How many A1?", deps=shop)
assert result.output == "7 left." and shop.log == ["A1"]
```

`RunContext` also carries `run_id`, `agent_name`, `turn`, `usage` and `messages`.

## Shared run state

`ctx.state` is a dict that tools, hooks, output validators and instruction functions can
read and write during a run. It holds facts the run accumulates, such as a cart, a
customer ID or a step reached, without putting them in the conversation:

```python
from typing import Any

from nexura_agents import Agent, FakeModel, MemorySessionStore, RunContext, Runner, ToolCall, tool


@tool
def add_to_cart(ctx: RunContext[Any], item: str) -> str:
    """Add an item to the cart."""
    cart = ctx.state.setdefault("cart", [])
    cart.append(item)
    return f"cart: {', '.join(cart)}"


def add(item: str, call_id: str) -> list[ToolCall]:
    return [ToolCall(id=call_id, name="add_to_cart", arguments={"item": item})]


model = FakeModel(responses=[add("pen", "c1"), "Added.", add("ink", "c2"), "Added."])
agent = Agent(name="Shop", instructions="Help with the cart.", model=model, tools=[add_to_cart])
runner = Runner(session_store=MemorySessionStore())

first = runner.run(agent, "add a pen", session_id="u1")
assert first.state == {"cart": ["pen"]}
second = runner.run(agent, "and ink", session_id="u1")  # the state came back with the session
assert second.state == {"cart": ["pen", "ink"]}
```

- `Runner.run(..., state={...})` seeds it; the values are merged over any state saved with
  the session. `result.state` has the final value.
- Instruction functions see it too, but in a session the system message is rendered on
  the first run and then kept. Read state in tools when it changes between runs.
- It is saved with the session (`MemorySessionStore`, `SQLiteSessionStore`), with
  approval pauses (`RunInterruption.state`, including `to_json`) and with durable
  checkpoints, and is restored by `resume` and `recover`.
- It is shared across handoffs: every agent in the run sees the same dict. An agent used as
  a tool (`as_tool()`) or a team member runs separately and starts with its own empty
  state.
- Keep it JSON-serialisable. Saving a value that is not raises `StorageSerializationError`.
- A custom session store whose `save` does not accept `state=` still works, but the state
  is not saved and the runner emits a `RuntimeWarning`.
- `deps` are things you pass in (clients, the current user) and are never stored.
  `state` is data the run produces and keeps.

## Sessions and conversation memory

Without a session, each run starts empty. To keep a conversation, give a `Runner` a session
store and a `session_id`. `MemorySessionStore` keeps history in process; `SQLiteSessionStore(path)`
keeps it in a file. The store loads history before the run and saves it after.

```python
from nexura_agents import Agent, FakeModel, MemorySessionStore, Runner

model = FakeModel(responses=["Nice to meet you, Asha.", "Your name is Asha."])
agent = Agent(name="Chat", instructions="Remember the user.", model=model)
runner = Runner(session_store=MemorySessionStore())

runner.run(agent, "I'm Asha.", session_id="user-1")
second = runner.run(agent, "What's my name?", session_id="user-1")

assert second.output == "Your name is Asha."
# The second model call saw the first exchange.
seen = [m.content for m in model.calls[1]["messages"]]
assert "I'm Asha." in seen and "Nice to meet you, Asha." in seen
```

You can also manage history yourself: pass `history=[...]` (a list of `Message`) to `run`. A
`session_id` needs a store, and you cannot combine a store with `history`; both mistakes raise
`SDKValidationError`. Saves are compare-and-swap, so two runs writing the same session
concurrently raise `SessionConflictError` instead of overwriting each other.

### Branching a conversation

`fork_session(store, source_id, new_id, at=None)` copies a session into a new one, so you can
try a different reply, let a user edit an earlier message, or run "what if" variants without
touching the original. `at` is how many stored messages to keep; the stored messages start
with the system message, so look at `(await store.get(id)).messages` to pick the point.

```python
import asyncio

from nexura_agents import Agent, FakeModel, MemorySessionStore, Runner, fork_session

store = MemorySessionStore()
model = FakeModel(responses=["Hi Asha.", "Tea, noted.", "Coffee, noted."])
agent = Agent(name="Chat", instructions="Remember the user.", model=model)
runner = Runner(session_store=store)


async def main() -> None:
    await runner.run_async(agent, "I'm Asha.", session_id="main")
    await runner.run_async(agent, "I like tea.", session_id="main")
    # Keep the system message and the first exchange, then take the conversation elsewhere.
    await fork_session(store, "main", "what-if", at=3)
    await runner.run_async(agent, "I like coffee.", session_id="what-if")

    main_chat = [m.content for m in (await store.get("main")).messages if m.role != "system"]
    branch = [m.content for m in (await store.get("what-if")).messages if m.role != "system"]
    assert main_chat == ["I'm Asha.", "Hi Asha.", "I like tea.", "Tea, noted."]
    assert branch == ["I'm Asha.", "Hi Asha.", "I like coffee.", "Coffee, noted."]


asyncio.run(main())
```

The source is never changed and a fork never overwrites: an existing `new_id` raises
`SessionConflictError`. A point that would separate a tool call from its results raises
`SDKValidationError` naming the nearest allowed points; a call that never got a result
(an interrupted turn) blocks nothing. Saved run state
(`RunContext.state`) is copied with the messages.

### Managing sessions

Four helpers, importable from `nexura_agents`, tidy up stored conversations:

- `delete_session(store, id)` removes a session and returns `True` if it existed. The id can
  be used again (save with `expected_revision=0`); its revision keeps counting from the
  deleted session, so a snapshot taken before the delete can never overwrite the new one.
- `list_sessions(store)` returns all session ids, sorted.
- `pop_last_turn(store, id)` removes the last user message and everything after it (tool
  calls, results, reply). Earlier turns, the system message and saved run state stay. It
  returns the saved snapshot, or `None` when there is no user turn. The output-validator
  retry messages the runner adds on a retry (role `user`) are not user turns: the real user
  message is the one removed, never the retry feedback.
- `retry_last_turn(runner, agent, id, **run_kwargs)` pops the last turn and runs the same
  user input again, so you get a fresh reply. Extra arguments (`deps`, `timeout`,
  `cancel`, ...) go to `run_async`. If the retry fails or is cancelled, the original turn is
  put back (unless the session was saved by someone else meanwhile). It is a helper
  function, not a `Runner` method.

`MemorySessionStore` and `SQLiteSessionStore` have `delete` and `list_sessions` methods. They
are optional for custom stores: without them `delete_session` and `list_sessions` raise
`SDKValidationError`. Popping only needs `get` and `save`, and is compare-and-swap, so a
concurrent save raises `SessionConflictError` instead of losing data.

```python
import asyncio

from nexura_agents import Agent, FakeModel, MemorySessionStore, Runner
from nexura_agents import delete_session, list_sessions, pop_last_turn, retry_last_turn

store = MemorySessionStore()
model = FakeModel(responses=["Tea.", "Coffee.", "Chai."])
agent = Agent(name="Chat", instructions="Answer briefly.", model=model)
runner = Runner(session_store=store)


async def main() -> None:
    await runner.run_async(agent, "Favourite drink?", session_id="chat")
    await runner.run_async(agent, "And another?", session_id="chat")
    retried = await retry_last_turn(runner, agent, "chat")
    assert retried.output == "Chai."

    snapshot = await pop_last_turn(store, "chat")
    assert [m.content for m in snapshot.messages if m.role != "system"] == [
        "Favourite drink?",
        "Tea.",
    ]

    assert await list_sessions(store) == ["chat"]
    assert await delete_session(store, "chat") is True
    assert await delete_session(store, "chat") is False


asyncio.run(main())
```

Sessions keep one conversation. To keep facts across conversations (preferences, names),
give the agent `memory=Memory(store)`; see [Long-term memory](memory.md).

### Trimming long conversations (history processors)

A session grows with every run, and eventually the provider rejects the request as too
long. `history_processors` change what the model sees on each call. The stored session,
checkpoints and `result.messages` keep the full conversation.

```python
from nexura_agents import (
    Agent,
    FakeModel,
    MemorySessionStore,
    Runner,
    fit_token_budget,
    keep_last_turns,
)

model = FakeModel(responses=["one", "two", "three"])
agent = Agent(
    name="Chat",
    instructions="Be brief.",
    model=model,
    history_processors=[keep_last_turns(2), fit_token_budget(8000)],
)
runner = Runner(session_store=MemorySessionStore())
for text in ["first", "second", "third"]:
    result = runner.run(agent, text, session_id="user-1")

sent = [m.content for m in model.calls[-1]["messages"] if m.role == "user"]
assert sent == ["second", "third"]  # the model saw the last two turns
assert len([m for m in result.messages if m.role == "user"]) == 3  # nothing was lost
```

- `keep_last_turns(n)` keeps the system message and the last `n` user turns, each with
  its tool calls and results.
- `fit_token_budget(max_tokens, estimator=None)` drops the oldest turns until the
  estimate fits. The estimate is offline (`HeuristicTokenEstimator`), not the provider's
  count, so leave headroom. Attachments are not counted.
- Both cut only where a user message starts, so a tool call is never separated from its
  result. The runner's output-retry feedback ("Your response did not match the required
  JSON schema…") counts as part of the turn it retries. Both always keep the system message and the current turn, even when those alone
  are over the budget.
- Your own processor is a function `(messages) -> messages` or `(ctx, messages) ->
  messages`, sync or async, and receives a copy. It gets the `RunContext` when it has two
  required positional parameters. Use it to summarise, redact or filter.
  Processors run in order, before recalled memories are added to the system message. A
  result that is not a list of `Message`, that keeps no conversation message, or that
  keeps a tool result without its call (or a call without the result it had) raises `SDKValidationError`. So does a processor
  that raises; the original error is in `original_error`.
- With handoffs, the active agent's processors apply.

### Rolling summary of older turns

Plain trimming forgets old turns. `summarize_older_turns(model, ...)` instead replaces
the turns older than the last `keep_last_turns` user turns with one summary written by
`model`:

```python
from nexura_agents import Agent, FakeModel, MemorySessionStore, Runner
from nexura_agents.history import summarize_older_turns

summarizer = FakeModel(
    responses=["The user is planning a trip to Pune.", "The user is planning a Pune trip by train."]
)
model = FakeModel(responses=["a", "b", "c", "d"])
agent = Agent(
    name="Chat",
    instructions="Be brief.",
    model=model,
    history_processors=[summarize_older_turns(summarizer, keep_last_turns=2)],
)
runner = Runner(session_store=MemorySessionStore())
for text in ["plan a trip", "to Pune", "by train", "in May"]:
    runner.run(agent, text, session_id="trip")

sent = model.calls[-1]["messages"]
assert [m.content for m in sent if m.role == "user"] == ["by train", "in May"]
summary = [m for m in sent if m.role == "system" and "Summary" in (m.content or "")]
assert "Pune trip by train" in summary[0].content
assert len(summarizer.calls) == 2  # one summary per new cut, not one per model call
```

- The summary is a **system** message right after the leading system messages, labelled
  "written automatically by a model, not by the user" and wrapped in
  `<conversation_summary>` tags, so it is never presented as the user's words.
- It cuts only at user-message boundaries (like `keep_last_turns`), so a tool call is
  never separated from its result. The runner's retry feedback is not sent to the
  summarizer. The session, checkpoints and `result.messages` keep the full history.
- Cache: each processor object caches summaries, keyed by a SHA-256 chain over the older
  turns plus `max_summary_chars` and `instructions`. The same prefix is summarized once
  however many model calls follow; when one more turn becomes old, the cached summary of
  the earlier prefix is extended with only that turn. At most `max_cache_entries`
  (default 64) summaries are kept, least recently used dropped. Runs that share the
  processor share the cache.
- `max_summary_chars` (default 2000) bounds the summary; `instructions` adds guidance for
  the summarizer; `timeout` bounds each summarizer call.
- If the summarizer fails or returns no text, the default `on_error="trim"` falls back to
  plain trimming for that call (older turns are dropped, nothing is cached, the next call
  retries). Use `on_error="raise"` to fail the run with `SDKValidationError` instead.
- The summarizer's token usage is counted with `ctx.add_usage` and added to the run's usage
  (and checked against `UsageLimits`) right after the model call it was made for.
- The excerpt sent to the summarizer has its angle brackets escaped, so text in the
  conversation cannot close the excerpt and pose as instructions to the summarizer.
- Import it from `nexura_agents` (or `nexura_agents.history`).

## Agents as tools

`agent.as_tool()` turns an agent into a tool another agent can call. The tool takes one
argument, `request`. The sub-agent receives the caller's `deps`, and its token usage is added to
the calling run, so usage limits and accounting cover the whole tree. A sub-agent cannot wait for
approval inside a tool call: if one of its tools needs approval, the call raises `ToolError`
and nothing after that point runs. Run such agents directly, or in a
[team](teams.md), where approvals pause and resume.

```python
from nexura_agents import Agent, FakeModel, ToolCall, Usage

specialist = Agent(
    name="Policy",
    instructions="Answer policy questions.",
    description="Answers refund policy questions.",
    model=FakeModel(responses=["Refunds within 30 days."], default_usage=Usage(10, 5, 15, True)),
)
lead = Agent(
    name="Lead",
    instructions="Ask the policy agent when needed.",
    model=FakeModel(
        responses=[
            [ToolCall(id="1", name="policy", arguments={"request": "refund window?"})],
            "30 days.",
        ],
        default_usage=Usage(1, 1, 2, True),
    ),
    tools=[specialist.as_tool()],
)

result = lead.run("What is the refund window?")
assert result.output == "30 days."
assert result.usage.total_tokens == 2 + 2 + 15  # two lead turns plus the sub-agent
```

The tool name defaults to the agent name in lower case with spaces replaced by underscores
(`"Policy"` becomes `policy`). Pass `name=`, `description=` or `runner=` to change them. If the
sub-agent has an `output_type`, the tool returns the object as a JSON-compatible dict.

## Handoffs

A handoff moves control to another agent: the first agent stops and the target finishes the run
(compare `as_tool()`, where the caller keeps control and only gets an answer). List targets in
`Agent(handoffs=[...])`. Each one becomes a `transfer_to_<snake_case_name>` tool with an optional
`reason: str`. When the model calls it, the target becomes the active agent for the rest of the
run: its model, instructions (rendered with the same `deps`), tools, handoffs, `output_type` and
output guardrails take over, and the conversation carries on in the same transcript. The call gets
the tool result `Transferred to <name>.`, so the transcript stays valid for every provider.

```python
from nexura_agents import Agent, FakeModel, Handoff, RunEventType, Runner, ToolCall

billing = Agent(
    name="Billing",
    instructions="You handle refunds.",
    model=FakeModel(responses=["Refund started."]),
)
triage = Agent(
    name="Triage",
    instructions="Route the request.",
    model=FakeModel(
        responses=[[ToolCall(id="1", name="transfer_to_billing", arguments={"reason": "refund"})]]
    ),
    handoffs=[billing],  # or Handoff(billing, tool_name=..., input_filter=..., on_handoff=...)
)

result = Runner().run(triage, "I want a refund")
assert result.final_output == "Refund started."
assert result.agent_path == ["Triage", "Billing"] and result.last_agent is billing
assert [e.data["to"] for e in result.events if e.type == RunEventType.HANDOFF] == ["Billing"]
```

`Handoff(agent, tool_name=None, description=None, input_filter=None, on_handoff=None)` customises
a target (`handoff(agent, **kw)` is shorthand). `input_filter` receives a copy of the conversation
without the system message and returns what the target sees; every tool call must keep its result
or the run fails with `SDKValidationError`. `on_handoff(ctx, reason)` runs when the handoff
happens and may be async. Agents that hand back to each other need `agent.add_handoffs(other)`
after both exist. A handoff tool name that duplicates another handoff or collides with the agent's
own tools (memory tools included) raises `SDKValidationError` at construction.

Rules:

- **One handoff per turn.** If a turn holds several handoff calls the first wins; the others get
  the result `Ignored: only one handoff per turn...`. Other tool calls in the same turn still run,
  with the agent that made them, and the switch happens after the whole batch. Results stay in
  call order.
- **Limits cover the whole run.** `max_turns`, `max_tool_calls` and `timeout` come from the agent
  that started the run, and usage is summed once across agents (`UsageLimits` and budgets apply as
  usual). `Agent(max_handoffs=10)` (or `Runner(max_handoffs=...)`, which wins) bounds the
  switches; one more raises `MaxHandoffsExceededError`.
- **Guardrails and output.** Input guardrails run only for the starting agent. The agent that
  produces the final answer supplies `output_type`, `output_retries` and output guardrails.
- **Memory** is recalled once, for the starting agent, and not repeated after a handoff.
- **Approvals.** If the active agent pauses, `RunInterruption.active_agent` and `agent_path`
  record it (they survive `to_json()` / `from_json()`; older payloads still load). Resume with the
  *starting* agent: `Runner.resume_async(triage, interruption)` finds the active agent by walking
  the handoff graph, and an unknown name raises `ApprovalDecisionError`.
- **Sessions.** The saved transcript contains the handoff call and its result, and it starts with
  the starting agent's system message again, so the next run of the session is valid.
- **Events and tracing.** A `HANDOFF` event (`from`, `to`, `reason`, `path`) is emitted between
  the two agents' model turns. With tracing on, each handoff is a `handoff A -> B` span and the
  `invoke_agent` span carries `nexura.agent.path` and `nexura.agent.last`.
- **Serving.** `AgentMCPServer` and `A2AServer` serve the starting agent; handoffs inside work
  unchanged.

### Structured handoff input and conditional handoffs

`Handoff(target, input_type=Model)` makes the model fill in structured fields when it hands
off, instead of a free-text reason. `Handoff(target, enabled=...)` offers a handoff only
when it applies:

```python
from typing import Any

from nexura_agents import Agent, FakeModel, Handoff, RunContext, Runner, ToolCall
from pydantic import BaseModel


class Escalation(BaseModel):
    priority: int
    summary: str


def billing_rules(ctx: RunContext[Any]) -> str:
    ticket = ctx.metadata.get("handoff_input") or {}
    return f"Resolve billing issues. Priority: {ticket.get('priority', 'n/a')}."


billing_model = FakeModel(responses=["Refund issued."])
billing = Agent(name="Billing", instructions=billing_rules, model=billing_model)
triage = Agent(
    name="Triage",
    instructions="Route customers.",
    model=FakeModel(
        responses=[
            [
                ToolCall(
                    id="h1",
                    name="transfer_to_billing",
                    arguments={"priority": 1, "summary": "charged twice"},
                )
            ]
        ]
    ),
    handoffs=[Handoff(billing, input_type=Escalation, enabled=lambda ctx: ctx.deps != "guest")],
)
result = Runner().run(triage, "I was charged twice", deps="customer")
assert result.output == "Refund issued."
assert billing_model.calls[0]["messages"][0].content == "Resolve billing issues. Priority: 1."
```

- With `input_type`, the handoff tool's parameters are that model's fields. Invalid input
  fails the run with `ToolValidationError`, as for a malformed `reason`.
  - `on_handoff(ctx, data)` receives the validated object.
  - The target agent finds the input as a dict in `ctx.metadata["handoff_input"]`, in its
    instructions, tools and hooks.
  - The next handoff clears it, so later agents never see an earlier agent's input.
    `handoff_input` is a reserved key: do not put your own value under it in
    `run(..., metadata=...)`, because handoffs overwrite or remove it.
  - The handoff and `ctx.metadata` (with `handoff_input`) survive a pause, `resume_async`
    and `recover_async`, so tools that read it after an approval still see the input.
- `enabled` takes `True`/`False` or a function of the `RunContext`, sync or async, checked
  before every model call, as for [`@tool(enabled=...)`](tools.md). A disabled handoff is
  not offered, and a call to it fails the batch with `UnknownToolError`.

The platform package has its own coordinator with the same `transfer_to_<name>` convention
(`AgentCoordinator`, `HandoffTarget`, `max_handoff_depth`, `total_fuel`); see `nexura_runtime`.

## Streaming

`Runner.stream_async` yields `RunEvent` objects as the run progresses: `RUN_STARTED`,
`AGENT_STARTED` (also after each handoff, with `HANDOFF` before it), `MODEL_STARTED`,
`MODEL_COMPLETED`, `TOOL_STARTED`, `TOOL_COMPLETED`, `RETRY`, `PARTIAL_OUTPUT` (typed output),
`FINAL_OUTPUT`, and a terminal `RUN_COMPLETED`, `RUN_PAUSED`, `RUN_CANCELLED` or `RUN_FAILED`. Pass `stream_text=True` to also get
`TEXT_DELTA` events, whose `data["text"]` is a piece of the model's reply.

Text deltas only appear when the model supports streaming. `FakeModel(..., stream=True)` streams
its scripted replies in `chunk_size` pieces (see [Testing](testing.md)); this
example uses `StreamingFakeProvider` with scripted deltas.

```python
import asyncio

from nexura_agents import Agent, RunEventType, Runner, Usage
from nexura_agents.providers.fake import StreamingFakeProvider
from nexura_agents.providers.types import StreamDelta

deltas = [
    StreamDelta(kind="text_delta", text="Hel"),
    StreamDelta(kind="text_delta", text="lo"),
    StreamDelta(kind="usage_delta", usage=Usage(4, 2, 6, True)),
    StreamDelta(kind="finish", finish_reason="stop"),
]
agent = Agent(
    name="Streamer",
    instructions="",
    model=StreamingFakeProvider("fake-stream", queued_deltas=[deltas]),
)


async def main() -> None:
    events = [e async for e in Runner().stream_async(agent, "hi", stream_text=True)]
    chunks = [e.data["text"] for e in events if e.type == RunEventType.TEXT_DELTA]
    assert chunks == ["Hel", "lo"]
    done = next(e for e in events if e.type == RunEventType.RUN_COMPLETED)
    assert done.data["output"] == "Hello"


asyncio.run(main())
```

With `stream_text=True` a streamed tool call also arrives piece by piece as `TOOL_CALL_DELTA`
events, so a UI can show "calling lookup..." while the model is still writing the arguments.
Each event's `data` has `turn`, `index` (which call in the reply, for parallel calls), `id` and
`name` (`None` until the model has sent them) and `arguments_delta` (a piece of the JSON
arguments; join the pieces of one `index` to get the whole string). Match pieces by
`index`: a provider that never sends an id keeps `id` at `None`, and the call then runs
with the generated id `call_<index>`. Chunks that carry nothing new emit no event. These are the model's raw
output, sent before argument validation, tool guardrails and approval, so use them for
display only; the call that runs is reported by `TOOL_STARTED`.

```python
import asyncio

from nexura_agents import Agent, RunEventType, Runner, tool
from nexura_agents.providers.fake import StreamingFakeProvider
from nexura_agents.providers.types import StreamDelta


@tool
def lookup(city: str) -> str:
    """Weather for a city."""
    return f"sunny in {city}"


def call(args: str, **first: str) -> StreamDelta:
    return StreamDelta(
        kind="tool_call_delta",
        tool_call_index=0,
        tool_call_id=first.get("id"),
        tool_name=first.get("name"),
        tool_arguments_delta=args,
    )


turns = [
    [call('{"city": ', id="c1", name="lookup"), call('"Pune"}'), StreamDelta(kind="finish")],
    [StreamDelta(kind="text_delta", text="Sunny."), StreamDelta(kind="finish")],
]
agent = Agent(
    name="Weather",
    instructions="",
    model=StreamingFakeProvider("fake-stream", queued_deltas=turns),
    tools=[lookup],
)


async def main() -> None:
    pieces = []
    async for event in Runner().stream_async(agent, "weather in Pune?", stream_text=True):
        if event.type == RunEventType.TOOL_CALL_DELTA:
            pieces.append(event.data["arguments_delta"])  # e.g. update a "calling..." chip
            assert event.data["name"] == "lookup"
    assert "".join(pieces) == '{"city": "Pune"}'


asyncio.run(main())
```

If a run ends in `RUN_FAILED`, the stream yields that event and then raises the underlying
error. The event's `error` text has credentials masked (`password=...`, bearer tokens, API
keys), since events are often stored or shown; the raised error keeps the full message. Without `stream_text`, `stream_async` still gives you the lifecycle events, with the
reply arriving whole in `FINAL_OUTPUT`.

### Custom progress events

A tool can report progress while it works: `ctx.emit(name, **data)` sends a `CUSTOM`
event to whoever streams the run. Its `data` has `name`, `data` (your keyword arguments),
`agent`, `tool_call_id` and `tool_name`. Events from a tool stream while the tool is still
running, also from sync tools (which run in a worker thread) and from tools running in
parallel. Called from hooks or instructions, outside a tool call, they arrive at the run's
next step with `tool_call_id` and `tool_name` set to `None`. In a team stream they carry the
member tags like every member event. `run_async` keeps them in `result.events`. The name
must be a non-empty string. The data is copied when you call `emit`, so changing your own
objects afterwards does not change the event; an event a background thread sends after its
tool has finished arrives at the run's next step instead of being lost.

```python
import asyncio

from nexura_agents import Agent, FakeModel, RunEventType, Runner, ToolCall, tool
from nexura_agents.run_context import RunContext


@tool
async def import_rows(ctx: RunContext[None], count: int) -> str:
    """Import rows."""
    for done in range(1, count + 1):
        ctx.emit("progress", done=done, total=count)  # e.g. drive a progress bar
    return f"imported {count} rows"


model = FakeModel(
    responses=[[ToolCall(id="c1", name="import_rows", arguments={"count": 3})], "Done."]
)
agent = Agent(name="Importer", instructions="", model=model, tools=[import_rows])


async def main() -> None:
    progress = [
        event.data["data"]["done"]
        async for event in Runner().stream_async(agent, "import")
        if event.type == RunEventType.CUSTOM
    ]
    assert progress == [1, 2, 3]


asyncio.run(main())
```

## Cancelling a run

Pass a `CancelToken` and call `token.cancel()` from anywhere: another task, a web
request handler, or another thread. The run stops and returns a result with
`status == "cancelled"` instead of raising:

```python
import threading

from nexura_agents import Agent, CancelToken, FakeModel, Runner

token = CancelToken()
token.cancel("user closed the chat")  # here before the run starts; usually from elsewhere

result = Runner().run(Agent(name="A", instructions="i", model=FakeModel()), "hi", cancel=token)
assert result.status == "cancelled"
assert result.events[-1].data["reason"] == "user closed the chat"
```

- The run checks the token before each model call, and interrupts what it is waiting
  on: a model call, an `async` tool, a tool hook, a tool guardrail, an output validator
  or an `on_handoff` callback. A streamed model call stops at the next chunk. A sync
  tool runs in a worker thread that Python cannot stop: the run moves on, but the
  function keeps running until it returns.
- The run ends with a `RUN_CANCELLED` event. The result has the messages, usage and turn
  count so far. The interrupted step is not recorded, and the session is not saved.
- With a `checkpoint_store`, the checkpoint is kept, so `recover_async` can continue the
  run later. A tool interrupted mid-call is treated as uncertain, like after a crash.
- With tracing on, the run's span ends with `nexura.run.status = "cancelled"`.
- One token can stop several runs. `cancel()` is safe from any thread and only the first
  reason is kept. Cancelling the asyncio task itself still raises `CancelledError`, as
  before.
- `run`, `run_async`, `stream_async`, `resume`/`resume_async`/`resume_stream_async` and
  `recover`/`recover_async`/`recover_stream_async` all accept `cancel=`.

## Copies with clone

`agent.clone(**changes)` returns a new agent with the same configuration and the given
constructor arguments changed. It is validated like a new agent, and the original is
left as it was. Type checkers keep the output type: `Agent[Out].clone(...)` and
`with agent.override(...) as a` are `Agent[Out]`, and `retry_last_turn` returns
`RunResult[Out]` for an `Agent[Out]`:

```python
from nexura_agents import Agent, FakeModel, ModelSettings

support = Agent(name="Support", instructions="Help.", model=FakeModel(), max_turns=6)
careful = support.clone(name="Support (careful)", model_settings=ModelSettings(temperature=0))
assert careful.max_turns == 6 and support.model_settings is None
```

- Any `Agent(...)` argument can be changed; an unknown name raises `SDKValidationError`.
- Handoffs added later with `add_handoffs` are kept unless you pass `handoffs=`. The
  model object is shared, and memory tools are not duplicated.
- For tests, `agent.override(model=...)` changes an agent in place for a `with` block;
  see [Testing](testing.md#swapping-the-model-of-an-existing-agent).

## Diagrams

`agent.to_mermaid()` (also on teams) returns a Mermaid flowchart. GitHub, GitLab and most
Markdown viewers render it, so you can paste it into a README or a design doc.

```python
from nexura_agents import Agent, FakeModel, tool


@tool(requires_approval=True)
def refund(amount: int) -> str:
    """Refund an order."""
    return "ok"


billing = Agent(name="Billing", instructions="Handle money.", model=FakeModel(), tools=[refund])
triage = Agent(name="Triage", instructions="Route.", model=FakeModel(), handoffs=[billing])

diagram = triage.to_mermaid()
assert diagram.startswith("flowchart LR")
assert '(["refund (approval)"])' in diagram and "-.->|handoff|" in diagram
```

- Agents are rectangles and tools are rounded. Tools are marked `(approval)`,
  `(disabled)` (`enabled=False`) or `(conditional)` (an `enabled` rule).
- Handoffs are dotted arrows. An agent or team used with `as_tool()` is drawn with an
  `as tool: <name>` arrow to it, and its own tools are shown too.
- Teams are boxes. Sequential members are joined in order, parallel members stand side
  by side, and a loop shows `repeat up to Nx`.
- Agents that hand off to each other are drawn once each, so cycles are safe.
  `to_mermaid(include_tools=False)` shows only agents, teams and handoffs.
- The diagram describes what the code allows, not one particular run.

## See also

- [Getting started](getting-started.md): install and first run.
- [Structured output](structured-output.md): typed results from an agent.
- [Tools](tools.md): function tools and dependencies in depth.
- [Human approval](approvals.md): pausing before risky tools.
- [Testing your agents](testing.md): scripting models for tests.

## What the agent SDK covers

Everything here lives in `nexura-agents` and is described in the [documentation index](README.md):

- **Agent teams and handoffs.** `SequentialAgent`, `ParallelAgent` and `LoopAgent` compose
  agents under one `Runner`; an approval inside any member pauses the whole team and
  resumes later. `Agent(handoffs=[...])` lets a triage agent pass the conversation to a
  specialist.
- **Structured output.** `output_type` takes a Pydantic model or a JSON-schema dict.
  OpenAI, Gemini/Vertex, LiteLLM and Anthropic get the schema natively; other models get
  it in the prompt. The runner validates either way.
- **Tools from anywhere.** MCP servers (stdio and Streamable HTTP), OpenAPI specs, images,
  PDFs and audio as `Attachment` inputs, and your agents served over MCP or A2A (Agent2Agent
  1.0, JSON-RPC binding; no push notifications, OAuth or gRPC).
- **Long-term memory and prompt registry.** `Agent(memory=Memory(store, namespace=...))`
  recalls facts per user; `PromptRegistry` versions prompts with movable labels.
- **Models.** Vertex, Gemini, OpenAI, Anthropic, Sarvam and Ollama directly, plus
  `LiteLLMModel` for the providers LiteLLM supports (streaming; checked offline only).
- **Tracing.** Opt-in OpenTelemetry spans using GenAI semantic conventions; nothing is
  imported or recorded until you call `instrument()`.
- **Durable runs.** `Runner(checkpoint_store=...)` checkpoints every step, and
  `recover_async` continues after a crash. Finished tools never re-run; uncertain ones
  pause for a decision unless marked `idempotent=True`.
- **[Reproducing failures](reproducing-failures.md).** Export recorded runs as
  JSON with best-effort redaction of known secret shapes and replay offline with
  scripted models and tool fixtures; review a bundle before sharing it.
- **Control over tools.** Per-tool `timeout`, automatic `retry` with backoff and per-run
  `cache` for idempotent tools, input and output guardrails per tool, `enabled` rules
  and `prepare_tools` to hide tools, and `tool_choice` to force, forbid or pick a tool.
- **Control over runs.** History processors trim long conversations, output validators
  send the model back with feedback, `PARTIAL_OUTPUT` streams typed answers as they
  arrive, and a `CancelToken` stops a run from anywhere.
- **Model settings.** Temperature, `top_p`, `seed`, penalties, reasoning effort and
  extended thinking (including Anthropic thinking with tools), set per runner or per
  agent.
- **Diagrams.** `agent.to_mermaid()` and `WorkflowSpec.to_mermaid()` draw agents,
  handoffs, tools and workflow steps.

A team of two agents, offline:

```python
from nexura_agents import Agent, FakeModel, SequentialAgent

researcher = Agent(
    name="Researcher",
    instructions="Find facts.",
    model=FakeModel(responses=["Tides follow the moon."]),
)
writer = Agent(
    name="Writer",
    instructions="Write a post.",
    model=FakeModel(responses=["Post: tides follow the moon."]),
)

result = SequentialAgent("Pipeline", [researcher, writer]).run("Tell me about tides")
print(result.status, result.output)  # completed Post: tides follow the moon.
```

## Package overview

**Build AI agents with estimated-cost admission control, approval before risky actions, and resumable runs.**

```bash
pip install nexura-agents            # core: httpx + pydantic only
pip install nexura-vertex            # + Google Vertex AI with your gcloud login (separate package)
pip install "nexura-agents[litellm]" # + LiteLLM models (Bedrock, Azure, and others)
pip install "nexura-agents[otel]"    # + OpenTelemetry tracing
```

- **One import, plain Python.** Functions become tools, Pydantic models become typed outputs.
- **Human approval that survives restarts.** A tool marked `requires_approval` pauses the
  run; save it as JSON, approve the *exact* arguments (tool name and argument digest)
  later — even from another process. Tools without `requires_approval` are not paused.
- **Spend control that happens before the call.** Budgets reserve the estimated cost
  before each model request and refuse calls that would exceed the allowance. This is
  admission control, not a provider-invoice ceiling: actual billed usage can exceed the
  estimate, after which later calls are refused. Token limits fail
  closed: if a provider doesn't report usage, the run stops rather than guessing zero.
- **Self-correcting agents.** Invalid output or a tool raising `ModelRetry` goes back to
  the model with the reason, instead of crashing the run.
- **Any model, one line.** `"vertex:gemini-2.5-flash"`, `"openai:gpt-4.1-mini"`,
  `"anthropic:..."`, `"gemini:..."`, `"sarvam:..."`, `"ollama:llama3.1"` — with retry and
  fallback across providers.
- **Teams and handoffs.** `SequentialAgent`, `ParallelAgent` and `LoopAgent` compose agents
  under one runner, and `Agent(handoffs=[...])` passes a conversation to a specialist.
  An approval anywhere pauses the whole run and resumes later.
- **Tests without keys.** `FakeModel` scripts the model so your agent logic runs offline in CI.

> Status: **alpha** (0.4). Python 3.11, 3.12 and 3.13 are declared; the CI matrix is defined
> but no remote CI has run yet. Locally, 295 SDK tests passed on 3.11 and 3.13 from a built
> wheel on 2026-10-09, excluding 4 platform-importing test files. The platform
> package `nexura-runtime` requires Python 3.12. The test suite runs offline against
> mocked provider APIs. Vertex AI (Gemini 2.5 Flash) has also passed a live check run by
> the owner on 2026-10-08 (plain reply, tool call, streaming; details in the repository's
> [Verification record](testing.md#verification-record)); every other provider is mock-verified
> only apart from Gemini API, whose reply/tool/streaming/structured-output check passed on 2026-10-20. `examples/live/provider_check.py` lets you run checks against
> OpenAI, Anthropic, Gemini or Sarvam with your own key; the OpenAI, Anthropic and Sarvam checks have not run.
> LiteLLM is checked offline only, against the real library's mock mode.
>
> Full guides live in the repository's `docs/` folder: 40 guides (getting started,
> agents, teams, tools, MCP and OpenAPI, A2A, memory, prompts, structured output,
> approvals, guardrails, multimodal, providers, tracing, testing and more) plus a
> generated API reference.

---

The MIT open-source edition is **single user, this machine only; for a team or a
public endpoint use the hosted or enterprise edition**. Agent teams below compose
agents in code; they do not provide human user accounts or service authentication.

## What you get

| You want | Use |
|---|---|
| Run it | `agent.run(prompt)` / `await agent.run_async(prompt)`; `Runner(...)` adds limits, hooks, budgets |
| Tools from functions | `@tool`; sync or async; arguments validated with Pydantic |
| Your app's objects inside tools | `deps=` + a `RunContext[YourDeps]` parameter (hidden from the model) |
| Instructions that depend on the user | `Agent(instructions=lambda ctx: ...)` |
| A typed, validated answer | `Agent(output_type=YourModel, output_retries=1)` → `result.output`; a JSON-schema dict also works, and OpenAI, Gemini/Vertex, LiteLLM and Anthropic get the schema natively |
| Let a tool say "try again" | `raise ModelRetry("why")`; `@tool(max_retries=2)` also retries bad arguments |
| Run independent tools at once | `Agent(parallel_tool_calls=True)` |
| A human in the loop | `@tool(requires_approval=True or predicate)` → `result.interruption` → `runner.resume(...)` |
| Block unsafe input or output | `Agent(input_guardrails=[...], output_guardrails=[...])` returning `GuardrailResult` |
| Stop runaway loops and cost | `Runner(usage_limits=UsageLimits(request_limit=..., total_tokens_limit=...))` |
| Estimated-cost admission control | `Runner(budget=BudgetManager(...).create_run_budget(...))` (provider models); actual billed usage can exceed the estimate, after which later calls are refused; not a provider-invoice ceiling |
| Observe or veto every step | `Runner(hooks=AgentHooks subclass)` and `runner.stream_async(...)` events |
| Live text as it's generated | `runner.stream_async(agent, prompt, stream_text=True)` → `TEXT_DELTA` events |
| Show tool calls as they're written | same `stream_text=True` → `TOOL_CALL_DELTA` events |
| Survive provider outages | `FallbackModel(RetryModel(primary), backup)` |
| Agents calling agents | `specialist.as_tool()`; usage rolls up into the caller's totals |
| Hand a conversation to a specialist | `Agent(handoffs=[billing, tech])` → `transfer_to_<name>` tools; `result.last_agent` |
| Pipelines, fan-out, refine loops | `SequentialAgent`, `ParallelAgent`, `LoopAgent`; approvals pause the whole team |
| Remember facts about a user | `Agent(memory=Memory(store, namespace=lambda ctx: ctx.deps.user_id))` |
| Prompts outside your code | `PromptRegistry` with versions and labels; `RunResult.prompt` records which was used |
| Traces in your OpenTelemetry backend | `nexura_agents.tracing.instrument()` (`[otel]` extra); off by default |
| Many more providers | `LiteLLMModel("bedrock/...")` or `"litellm:..."` (`[litellm]` extra; streams text and typed output) |
| Talk to other frameworks' agents | `A2AServer(agent).http_app(token=...)`, `A2AClient`, `remote_agent_tool(url)` |
| Tools from an MCP server | `MCPServer.stdio(...)` / `MCPServer.http(url)` → `await server.tools()` |
| Tools from a REST API | `openapi_tools(spec, headers={"Authorization": ...})` |
| Images, PDFs, audio | `agent.run(["What's wrong here?", Attachment.from_path("bill.pdf")])` |
| Your agents inside Claude Desktop, Cursor, VS Code | `AgentMCPServer("name", agents=[...]).run_stdio()`; tested in-process against this SDK's own MCP client only; not yet checked with those apps |
| Conversation memory | `Runner(session_store=SQLiteSessionStore(path))` + `session_id=` |
| Keep long conversations within limits | `Agent(history_processors=[keep_last_turns(10), fit_token_budget(8000)])`; the stored session keeps everything |
| Check the answer's meaning, not just its shape | `Agent(output_validators=[fn])`; `raise ModelRetry("why")` sends the model back |
| Fill a form while the answer streams | `output_type=...` + `stream_text=True` → `PARTIAL_OUTPUT` events |
| Show a tool only to some users or steps | `@tool(enabled=lambda ctx: ...)`, `Agent(prepare_tools=fn)` |
| Check a tool's arguments and results | `@tool(input_guardrails=[...], output_guardrails=[...])`; `GuardrailResult.rejected("why")` tells the model |
| Slow or flaky tools | `@tool(timeout=10)`; `@tool(idempotent=True, retry=ToolRetry(), cache=True)` |
| Let a tool's result be the answer | `Agent(tool_use_behavior=StopAtTools(["lookup"]))` |
| Force, forbid or pick a tool | `ModelSettings(tool_choice="required" \| "none" \| "<tool>")` |
| Per-agent temperature, seed, reasoning effort, thinking | `Agent(model_settings=ModelSettings(...))`, merged over `Runner(model_settings=...)` |
| Survive a crash mid-run | `Runner(checkpoint_store=SQLiteCheckpointStore(path))` → `runner.recover_async(agent, run_id)` |
| Stop a run from elsewhere | `token = CancelToken()`; `run(..., cancel=token)`; `token.cancel()` |
| Structured or conditional handoffs | `Handoff(billing, input_type=Ticket, enabled=lambda ctx: ...)` |
| A variant of an agent, or a fake model in tests | `agent.clone(**changes)`, `with agent.override(model=FakeModel(...)):` |
| A picture of your agents or workflows | `agent.to_mermaid()`, `team.to_mermaid()`, `spec.to_mermaid()` |

## Memory, prompts and tracing

- **Long-term memory.** `Agent(memory=Memory(store, namespace=...))` recalls relevant
  memories into a delimited block in the system prompt and gives the model `remember` and
  `recall` tools. Namespaces keep users apart. `HashingEmbedder` is for offline demos;
  `ProviderEmbedder` uses a real embedding model. See `docs/memory.md`.
- **Prompt registry.** `PromptRegistry` stores immutable versions with movable labels
  (`set_label("support", "production", 3)`), in memory or in files. Pass
  `registry.instructions("support", label="production")` as the agent's instructions; the
  version used appears in `RunResult.prompt`. See `docs/prompts.md`.
- **OpenTelemetry.** With the `[otel]` extra, `instrument()` emits GenAI-convention spans
  for agents, tools, teams, handoffs and workflows. Message content is recorded only with
  `capture_content=True`. Nothing is imported or recorded until you call it, and a failing
  tracer never fails a run. This sends spans to your backend; Nexura Agents has no tracing
  UI of its own. See `docs/observability.md`.

## Beyond the SDK

`nexura-agents` is the agent toolkit on its own. The **Nexura platform**
(`nexura-runtime`) builds on it with durable workflows, a REST service, a TypeScript
client, the Studio web UI (with a pending-input banner, a checkpoint history tab with
fork, and a playground model picker), a trigger worker that runs scheduled and webhook
workflows with job leases, an operator-controlled allowlist of real chat models, RAG,
MCP/OpenAPI connectors and evaluations.

## Project

MIT licensed, © 2026 Liceron. Security reports: info@liceron.in.
