# Teams

A team runs several agents as one unit. `SequentialAgent` runs them in order, `ParallelAgent` runs them at once, and `LoopAgent` runs one agent repeatedly until a condition is met. Teams nest, share one `Runner` (so hooks, usage limits and budgets cover everything), add up usage across all members, and pass human approvals through: if any member needs approval the whole team pauses and can be resumed later, without re-running the members that already finished.

Every team has `run` / `run_async` and returns a `TeamResult` with `status` (`"completed"`, `"paused"` or `"cancelled"`), `output`, `steps` (one `TeamStep` per member, with its `path`, `member` name and `result`), `usage` and `interruption`.

## SequentialAgent

Each member receives the previous member's output as its prompt. The first member receives the team's prompt, and the team's output is the last member's output. Pydantic outputs are passed on as JSON and other non-text values as `json.dumps`.

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

pipeline = SequentialAgent("Pipeline", [researcher, writer])
result = pipeline.run("Tell me about tides")

assert result.status == "completed"
assert result.output == "Post: tides follow the moon."
assert [(s.path, s.member) for s in result.steps] == [("0", "Researcher"), ("1", "Writer")]
# The writer was given the researcher's answer.
assert writer.model.calls[0]["messages"][-1].content == "Tides follow the moon."
```

Pass `handoff=fn` to build the next prompt yourself. It receives the team's original prompt and the previous member's result (a `RunResult` or `TeamResult`).

```python
from nexura_agents import Agent, FakeModel, SequentialAgent

first = Agent(name="First", instructions="", model=FakeModel(responses=["draft"]))
second = Agent(name="Second", instructions="", model=FakeModel(responses=["final"]))

team = SequentialAgent(
    "Pipe",
    [first, second],
    handoff=lambda original, previous: f"Task: {original}\nDraft: {previous.final_output}",
)
team.run("Write a haiku")
assert second.model.calls[0]["messages"][-1].content == "Task: Write a haiku\nDraft: draft"
```

## ParallelAgent

Every member receives the same prompt and runs concurrently (at most `max_concurrency` at a time, default 10). `steps` stay in declaration order whatever the finish order. The output is a dict `{member name: member output}`, or whatever your `combine` function returns. It receives `{member name: result}`.

If one member raises, the others are cancelled and that error is raised.

```python
from nexura_agents import Agent, FakeModel, ParallelAgent


def build() -> list[Agent]:
    return [
        Agent(name="Pro", instructions="", model=FakeModel(responses=["It is fast."])),
        Agent(name="Con", instructions="", model=FakeModel(responses=["It is costly."])),
    ]


result = ParallelAgent("Debate", build()).run("Evaluate the plan")
assert result.output == {"Pro": "It is fast.", "Con": "It is costly."}

merged = ParallelAgent(
    "Debate",
    build(),
    combine=lambda by_name: " / ".join(r.final_output for r in by_name.values()),
)
assert merged.run("Evaluate the plan").output == "It is fast. / It is costly."
```

## LoopAgent

Runs one member repeatedly. Iteration 1 receives the team's prompt; later iterations receive the last output as text, or `feedback(original_prompt, last_result)` if given. The loop stops when `until(last_result)` returns something truthy, or after `max_iterations` (1 to 100). Steps are named `iter-1`, `iter-2`, and so on.

```python
from nexura_agents import Agent, FakeModel, LoopAgent

editor = Agent(
    name="Editor",
    instructions="Improve the text.",
    model=FakeModel(responses=["v1", "v2 (good)", "v3"]),
)

loop = LoopAgent("Polish", editor, max_iterations=5, until=lambda r: "good" in r.final_output)
result = loop.run("rough text")

assert result.output == "v2 (good)"
assert [s.path for s in result.steps] == ["iter-1", "iter-2"]
```

## GroupChat

Members take turns in one shared conversation. Each turn one member speaks; its prompt is the task plus the conversation so far, as plain text, one block per turn in the form `<member name>: <output text>`:

```text
Write a haiku

Conversation so far:
Writer: first draft
Critic: too long
```

Lines after the first of one output are indented four spaces, so an output can never pass itself off as another member's turn. Outputs that are not text (structured output) are shown as JSON, the same before and after a pause. If the task is a list of `Message` objects, later turns get those messages unchanged (roles and attachments kept) plus the conversation as a final user message. The first turn gets the task alone. The history is bounded: only the last `history_turns` turns (default 20) are shown, and the oldest are dropped until the text fits `history_chars` (default 20000; one longer turn is cut from its start). Whenever something was dropped the history starts with `[earlier turns omitted]`.

`select` chooses who speaks next:

- `"round_robin"` (default) cycles the members in order.
- A callable `(transcript, member_names) -> name`, where `transcript` is a list of `GroupChatTurn(turn, speaker, text, output)`.
- A manager `Agent`, asked "who speaks next" with the member names and their `description`s. Its answer (stripped, case-insensitive) must be a member name; anything else raises `SDKValidationError`, nobody is picked silently. The manager's usage counts toward the team's usage and `UsageLimits`, and it cannot need approval. Members' outputs reach the manager as quoted, indented data, but a member can still argue for who goes next; the answer is always limited to the member names.

`terminate` ends the chat early: `MaxTurns(n)`, `TextMention(text)` (case-insensitive, in the last speaker's output), or a plain (not async) `callable(transcript) -> bool`, which gets a snapshot of the transcript. Combine them with `|` (either) and `&` (both). `max_turns` (1 to 100) is always a hard cap. The result's `output` is the last speaker's output and `result.stop_reason` says why it ended: `"max_turns"` for the cap, otherwise the condition, such as `"text_mention('done')"` (`None` for other teams).

Steps are named `turn-3:Writer` (the speaker is in the path); a manager's choice is its own step, `select-3`. On resume, finished and paused turns keep their speaker, so the selector is never asked again for them. Member names must be unique ignoring case and must not contain `/`.

```python
from nexura_agents import Agent, FakeModel
from nexura_agents.teams import GroupChat, TextMention

writer = Agent(
    name="Writer",
    instructions="Write.",
    description="writes drafts",
    model=FakeModel(responses=["draft one", "draft two"]),
)
critic = Agent(
    name="Critic",
    instructions="Critique.",
    description="reviews drafts",
    model=FakeModel(responses=["shorter please", "DONE: approved"]),
)
chat = GroupChat("Review", [writer, critic], max_turns=10, terminate=TextMention("done"))
result = chat.run("Write a haiku")

assert [s.path for s in result.steps] == [
    "turn-1:Writer",
    "turn-2:Critic",
    "turn-3:Writer",
    "turn-4:Critic",
]
assert result.output == "DONE: approved"
assert result.stop_reason == "text_mention('done')"
assert critic.model.calls[0]["messages"][-1].content == (
    "Write a haiku\n\nConversation so far:\nWriter: draft one"
)
```

Using a manager agent:

```python
from nexura_agents import Agent, FakeModel
from nexura_agents.teams import GroupChat

coder = Agent(
    name="Coder",
    instructions="",
    description="writes code",
    model=FakeModel(responses=["print(1)"]),
)
tester = Agent(
    name="Tester", instructions="", description="tests code", model=FakeModel(responses=["passes"])
)
manager = Agent(
    name="Manager",
    instructions="Pick the next speaker.",
    model=FakeModel(responses=["coder", " TESTER "]),
)
chat = GroupChat("Dev", [coder, tester], max_turns=2, select=manager)
result = chat.run("Add a feature")
assert [s.path for s in result.steps] == ["select-1", "turn-1:Coder", "select-2", "turn-2:Tester"]
```

Import `GroupChat`, `GroupChatTurn`, `MaxTurns`, `TextMention` and `TerminationCondition` from `nexura_agents` (or `nexura_agents.teams`).

## Nested teams

A team member can itself be a team. Paths of nested steps include the parent position, such as `"1/0"`.

```python
from nexura_agents import Agent, FakeModel, ParallelAgent, SequentialAgent


def agent(name: str, reply: str) -> Agent:
    return Agent(name=name, instructions="", model=FakeModel(responses=[reply]))


team = SequentialAgent(
    "Report",
    [
        agent("Planner", "plan"),
        ParallelAgent("Sections", [agent("Intro", "intro"), agent("Body", "body")]),
        agent("Editor", "edited"),
    ],
)
result = team.run("Write the report")

assert result.output == "edited"
sections = result.steps[1].result
assert [s.path for s in sections.steps] == ["1/0", "1/1"]
```

A nested team that was not given its own `runner` uses its parent's, so hooks and one `Runner(usage_limits=...)` passed to the outer team cover the whole tree. Token limits are checked against the running total of every sub-run in the team (after each member finishes), while `request_limit` still applies to each member's own run. A `TeamResult` has `.output` and `.final_output` (the output as text), so callbacks that receive `RunResult | TeamResult` can use either type the same way. `result.usage` adds up every sub-run, nested teams included.

## Approvals pass through

If a member pauses for approval (a tool with `requires_approval`), the team stops scheduling new work, lets members that are already running finish, and returns `status == "paused"` with a `TeamInterruption`. Finished steps are kept. Decisions are keyed by `"<path>:<call id>"`; list them with `interruption.pending_calls()` and the digests to show a reviewer with `interruption.pending_digests()`.

`to_json()` saves the interruption (the paused runs, prompts, plus the output and usage of finished members) and `TeamInterruption.from_json()` rebuilds it. A new process must share the signing key or approval store that authenticates this whole envelope; action digests alone do not authenticate team state. Finished members are never run again, and usage from before and after the pause is counted once. Outputs of finished members come back as plain JSON values after a round trip (a Pydantic model becomes a dict).

```python
from nexura_agents import Agent, FakeModel, Runner, SequentialAgent, TeamInterruption, ToolCall, tool

refunds = []
# Offline example key. In an application, provision and securely retain a random secret.
signing_key = b"offline-team-example-key"


@tool(requires_approval=True)
def refund(order_id: str) -> str:
    """Refund an order."""
    refunds.append(order_id)
    return f"refunded {order_id}"


def build_team(resumed: bool = False) -> SequentialAgent:
    # A rebuilt agent's fake model starts from the answer that follows the tool call.
    replies = ["Refunded."]
    if not resumed:
        replies.insert(0, [ToolCall(id="r1", name="refund", arguments={"order_id": "A1"})])
    clerk = Agent(name="Clerk", instructions="", model=FakeModel(responses=replies), tools=[refund])
    notify = Agent(
        name="Notify", instructions="", model=FakeModel(responses=["Customer notified."])
    )
    return SequentialAgent(
        "Refunds", [clerk, notify], runner=Runner(approval_signing_secret=signing_key)
    )


paused = build_team().run("Refund order A1")
assert paused.status == "paused" and refunds == []
interruption = paused.interruption
assert [key for key, call in interruption.pending_calls()] == ["0:r1"]
saved = interruption.to_json()  # store it anywhere

# ... later, in another process, with newly built objects ...
restored = TeamInterruption.from_json(saved)
final = build_team(resumed=True).resume(restored, approve=True)
assert final.status == "completed" and final.output == "Customer notified."
assert refunds == ["A1"]
```

`resume` / `resume_async` take the same decisions as `Runner.resume`: `approve=True` (everything not in `reject`), a list of keys, or a mapping of key to digest; `reject` maps keys to reasons. Unless `approve=True`, every pending call needs a decision, otherwise `SDKValidationError` names the missing keys. A resumed team can pause again.

In a `ParallelAgent` several members can pause at once; every paused member is listed in `pending`. In a `LoopAgent` the resumed iteration is counted, so the loop continues with the next one.

## A team as a tool

`team.as_tool()` lets another agent call the team (like `Agent.as_tool`, it takes an optional `runner=`, used for members unless the team has its own). The team receives the caller's `deps` and its usage is added to the calling run. A team cannot wait for approval inside another agent's tool call: if a member needs it, the tool raises an error and nothing after the pause ran.

```python
from nexura_agents import Agent, FakeModel, SequentialAgent, ToolCall

research = SequentialAgent(
    "Research",
    [
        Agent(name="Search", instructions="", model=FakeModel(responses=["found"])),
        Agent(name="Summarise", instructions="", model=FakeModel(responses=["summary"])),
    ],
    description="Researches a topic.",
)
lead = Agent(
    name="Lead",
    instructions="Delegate research.",
    tools=[research.as_tool()],
    model=FakeModel(
        responses=[
            [ToolCall(id="t1", name="research", arguments={"request": "tides"})],
            "All done.",
        ]
    ),
)
result = lead.run("Look into tides")
assert result.output == "All done."
assert "summary" in [m.content for m in result.messages if m.role == "tool"]
```

## Cancelling a team

Pass a `CancelToken` as `cancel=` to `run`, `run_async`, `resume_async`, `stream_async` or `resume_stream_async`. It goes to every member run, so cancelling it stops the whole team: running members stop, no further member starts, and nested teams stop too. Nothing is raised. The result has `status == "cancelled"`, `output is None`, `steps` for the members that finished or were stopped, and `usage` for what they used. A cancelled team has no `interruption` and cannot be resumed. When streaming, the last event is `TEAM_COMPLETED` whose `data["result"].status` is `"cancelled"`. `runner=` on `run` and `run_async` replaces the team's runner for that call, as it does on `stream_async`.

```python
from nexura_agents import Agent, CancelToken, FakeModel, SequentialAgent

token = CancelToken()
token.cancel("user left")
team = SequentialAgent("t", [Agent(name="a", model=FakeModel(["hi"]), instructions="x")])
result = team.run("go", cancel=token)
print(result.status)
```

## Streaming progress

`team.stream_async(prompt, deps=None, timeout=None, runner=None, cancel=None)` runs the team and yields events as it goes, so a UI can show who is working. It takes the same arguments as `run_async` (`run`, `run_async`, `resume_async`, `stream_async` and `resume_stream_async` all accept `deps`, `timeout`, `runner` and `cancel`). `team.resume_stream_async(interruption, ...)` does the same for a resume.

The events are `RunEvent` objects:

- `TEAM_STARTED` and, last, `TEAM_COMPLETED` or `TEAM_PAUSED`. The last event's `data["result"]` is the same `TeamResult` that `run_async` returns. Nested teams report their own start and end too.
- `MEMBER_STARTED` and then `MEMBER_COMPLETED` (`data["status"]`, `data["result"]`) or `MEMBER_FAILED` (`data["error"]`, `data["error_type"]`) for each member. A member finished before a pause is reported once, with `data["restored"] = True`.
- Every event of a member agent (`RUN_STARTED`, `TOOL_STARTED`, `FINAL_OUTPUT` and so on) keeps its type and `run_id`, and gains `data["team"]`, `data["member"]` and `data["member_path"]`, the list of path segments (`["1", "0"]` for the first member of the team at position 1).

Members that run in parallel interleave in the order events arrive. If a member raises, `TEAM_FAILED` is yielded (`data["error"]`, `data["error_type"]`) and the error is then raised from the stream, like `run_async`. Closing the stream early (`await stream.aclose()` or leaving the `async for` block) cancels the members that are still running. `event.is_terminal` is true only for the root team's last event (`TEAM_COMPLETED`, `TEAM_PAUSED` or `TEAM_FAILED` without a `member_path`); a member's own `RUN_COMPLETED` and a nested team's end events are not terminal. Member runs go through `Runner.stream_async` (or `resume_stream_async`), so a `Runner` subclass that customises `run_async` only affects `run_async`.

```python
import asyncio

from nexura_agents import Agent, FakeModel, SequentialAgent
from nexura_agents.events import RunEventType

pipeline = SequentialAgent(
    "Pipeline",
    [
        Agent(name="Researcher", instructions="", model=FakeModel(responses=["facts"])),
        Agent(name="Writer", instructions="", model=FakeModel(responses=["a post"])),
    ],
)


async def main() -> None:
    members = []
    last = None
    async for event in pipeline.stream_async("Tell me about tides"):
        if event.type == RunEventType.MEMBER_STARTED:
            members.append(event.data["member"])
        last = event
    assert members == ["Researcher", "Writer"]
    assert last is not None and last.type == RunEventType.TEAM_COMPLETED
    assert last.data["result"].output == "a post"


asyncio.run(main())
```

## Serving a team over MCP

`AgentMCPServer(agents=[...])` accepts teams as well as agents. A team becomes one tool named after the team. A team that pauses for approval is refused with a "needs human approval" error, because approval cannot be given over MCP. Unlike a single agent, a team may already have run earlier members (or finished siblings) before the pause, so the message says those members ran and their effects stand. See [Serving agents over MCP](serving-over-mcp.md).

## Teams or handoffs?

Use a team when you decide the order up front (pipelines, fan-out, loops). Use
[handoffs](agents.md#handoffs) when the model decides who takes over: a triage agent routes to a
specialist, which then owns the rest of the conversation. The two combine: a handoff inside a team
member works, and the member's result reports the agent that finished.

Use a [`GroupChat`](#groupchat) when members should talk to each other over several turns and
something (round robin, your function or a manager agent) decides who speaks, instead of a fixed
order or a model handing over control.

## See also

- [Agents](agents.md): agents as tools and handoffs.
- [Human approval](approvals.md): `RunInterruption`, digests and decisions.
- [Guardrails, hooks and limits](guardrails-hooks-limits.md): limits that apply to a shared `Runner`.
- [Budgets and usage](budgets-and-usage.md): usage accounting.
- [Serving agents over MCP](serving-over-mcp.md): teams as MCP tools.

## Teams and handoffs

A team runs several agents as one unit with one `Runner`, so hooks, usage limits and
budgets cover all of it and usage is summed once. If any member needs approval the whole
team pauses (`TeamInterruption`, JSON round trip) and `team.resume_async(...)` continues
without re-running finished members. `team.as_tool()` lets another agent call a team.

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

A handoff is different: the first agent stops and the specialist finishes the run, with its
own model, instructions, tools and `output_type`.

```python
from nexura_agents import Agent, FakeModel, Runner, ToolCall

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
    handoffs=[billing],
)

result = Runner().run(triage, "I want a refund")
print(result.agent_path, result.final_output)  # ['Triage', 'Billing'] Refund started.
```

`max_handoffs` (default 10) bounds the switches, and an approval pause resumes with the
agent that was active. See `docs/teams.md` and `docs/agents.md`.
