# Human approval

Some tools should not run without a person saying yes: sending money, deleting data, emailing customers. Mark such a tool with `requires_approval` and the run stops before the tool executes. You get back a `RunInterruption` that you can show to a reviewer, save as JSON, and resume later, even in another process. This page covers pausing, persisting, resuming, and binding an approval to the exact arguments the reviewer saw.

## Pause and resume

```python
from nexura_agents import Agent, FakeModel, Runner, ToolCall, tool

sent = []


@tool(requires_approval=True)
def send_refund(order_id: str, amount: int) -> str:
    """Refund an order."""
    sent.append((order_id, amount))
    return f"refunded {amount} for {order_id}"


model = FakeModel(
    responses=[
        [ToolCall(id="c1", name="send_refund", arguments={"order_id": "A1", "amount": 40})],
        "Refund sent.",
    ]
)
agent = Agent(name="Support", instructions="Help.", model=model, tools=[send_refund])
runner = Runner()

result = runner.run(agent, "Refund order A1")
assert result.status == "paused"
assert sent == []  # the tool has not run
interruption = result.interruption
assert [c.name for c in interruption.pending] == ["send_refund"]

final = runner.resume(agent, interruption, approve=True)
assert final.status == "completed"
assert final.output == "Refund sent."
assert sent == [("A1", 40)]
```

When the model asks for a tool with `requires_approval`, the runner stops before running anything from that model response. `result.status` is `"paused"` and `result.interruption` holds the paused state. No tool in that batch has run yet.

`requires_approval` can also be a function `(ctx, arguments) -> bool`, so only some calls pause, for example `lambda ctx, args: args["amount"] > 100`. See [Tools](tools.md).

If one model response contains several tool calls and any of them needs approval, the whole batch waits. On resume, the calls that did not need approval run together with the approved ones.

## What a RunInterruption holds

`RunInterruption` is a dataclass. The fields you will use:

- `pending`: the `ToolCall`s waiting for a decision (`id`, `name`, `arguments`).
- `run_id`, `agent_name`: which run and agent it belongs to.
- `messages`: the transcript so far, ending with the assistant message that asked for the tools.
- `usage`, `turn`, `tool_calls_count`: counters at the time of the pause.
- `requests`: how many model requests the run has made so far.
- `session_id`, `expected_revision`: session information if you use a session store.
- `tags`, `metadata`, `state`, `usage_by_agent`, `usage_by_model`: run context that the
  resumed run continues with, so `ctx.tags` and `result.tags` match the original run.
  `to_json()` saves metadata values that JSON cannot hold (dates, objects) as text, while
  `state` must be JSON already.

`resume` raises `ApprovalDecisionError` if you pass an interruption from a different agent (matched by agent name).

## Save it and resume later

`to_json()` returns a string and `RunInterruption.from_json()` rebuilds the object. Store the string in a database or a file and resume from another process.

```python
from nexura_agents import Agent, FakeModel, Runner, ToolCall, tool
from nexura_agents.types import RunInterruption


@tool(requires_approval=True)
def delete_user(user_id: str) -> str:
    """Delete a user."""
    return f"deleted {user_id}"


def build_agent() -> Agent:
    model = FakeModel(
        responses=[
            [ToolCall(id="c1", name="delete_user", arguments={"user_id": "u9"})],
            "Done.",
        ]
    )
    return Agent(name="Admin", instructions="Admin.", model=model, tools=[delete_user])


paused = Runner().run(build_agent(), "Delete user u9")
saved = paused.interruption.to_json()  # a str; write it anywhere

# ... later, possibly in another process ...
restored = RunInterruption.from_json(saved)
assert restored.pending[0].arguments == {"user_id": "u9"}

# The agent must be rebuilt with the same name and tools. This fake model starts
# from the answer that follows the tool call.
agent = Agent(
    name="Admin",
    instructions="Admin.",
    model=FakeModel(responses=["Done."]),
    tools=[delete_user],
)
final = Runner().resume(agent, restored, approve=True)
assert final.output == "Done."
assert "deleted u9" in [m.content for m in final.messages if m.role == "tool"]
```

The JSON stores the transcript, the pending calls, the counters and `requests`. Only the token counts (`prompt_tokens`, `completion_tokens`, `total_tokens`) and `is_complete` of `usage` are saved; the optional cache and `reasoning_tokens` fields are not.

`from_json` raises `ValueError` if the document has an unsupported `schema_version`.

## Decisions: approve, reject, or both

`approve` accepts three forms:

- `True`: approve every pending call that is not in `reject`.
- A list of call IDs.
- A mapping of call ID to digest (next section).

`reject` is a mapping of call ID to a reason. Rejected calls do not run; the model receives a tool message saying the call was rejected and why, and can answer accordingly.

Every pending call must be decided exactly once. If you leave one out, or name a call in both `approve` and `reject`, `resume` raises `ApprovalDecisionError`.

```python
from nexura_agents import Agent, ApprovalDecisionError, FakeModel, Runner, ToolCall, tool

ran = []


@tool(requires_approval=True)
def pay(invoice: str) -> str:
    """Pay an invoice."""
    ran.append(invoice)
    return f"paid {invoice}"


def make_agent() -> Agent:
    model = FakeModel(
        responses=[
            [
                ToolCall(id="p1", name="pay", arguments={"invoice": "INV-1"}),
                ToolCall(id="p2", name="pay", arguments={"invoice": "INV-2"}),
            ],
            "Handled.",
        ]
    )
    return Agent(name="Billing", instructions="Pay.", model=model, tools=[pay])


agent = make_agent()
interruption = Runner().run(agent, "Pay both").interruption

# Missing a decision for p2: refused.
try:
    Runner().resume(agent, interruption, approve=["p1"])
except ApprovalDecisionError:
    pass
else:
    raise AssertionError("expected ApprovalDecisionError")
assert ran == []

final = Runner().resume(agent, interruption, approve=["p1"], reject={"p2": "duplicate invoice"})
assert ran == ["INV-1"]
tool_messages = [m.content for m in final.messages if m.role == "tool"]
assert any("duplicate invoice" in text for text in tool_messages)
```

The SDK does not record that an interruption was already resumed. If a stored interruption must be used only once, enforce that in your own storage.

## Bind approval to the exact arguments

A reviewer approves a specific action, not just a call ID. `pending_digests()` returns `{call_id: sha256}` where the digest covers the tool name and the canonical JSON of its arguments. Show the arguments to the reviewer, keep the digest, and pass it back as `approve={call_id: digest}`.

```python
from nexura_agents import Agent, ApprovalDecisionError, FakeModel, Runner, ToolCall, tool


@tool(requires_approval=True)
def transfer(to: str, amount: int) -> str:
    """Transfer money."""
    return f"sent {amount} to {to}"


model = FakeModel(
    responses=[
        [ToolCall(id="t1", name="transfer", arguments={"to": "bob", "amount": 10})],
        "ok",
    ]
)
agent = Agent(name="Bank", instructions="Bank.", model=model, tools=[transfer])
interruption = Runner().run(agent, "Send Bob 10").interruption

digests = interruption.pending_digests()
assert list(digests) == ["t1"]

# A digest the reviewer saw for different arguments is refused.
wrong = {"t1": "0" * 64}
try:
    Runner().resume(agent, interruption, approve=wrong)
except ApprovalDecisionError as exc:
    assert "does not match" in str(exc)
else:
    raise AssertionError("expected ApprovalDecisionError")

final = Runner().resume(agent, interruption, approve=digests)
assert final.output == "ok"
```

On resume the runner also checks that the pending calls match the assistant message at the end of the transcript. If someone edited the saved JSON so the arguments in `pending` no longer match the transcript, resume raises `ApprovalDecisionError` and nothing runs. The digests are not signed, so protect the stored JSON the same way you protect any other record that grants authority.

`approve=True` is the shortcut for "approve all of what is pending, as stored". Use the digest form when a person reviews the arguments.

## Approve with changes

A reviewer often wants to say "yes, but smaller" rather than reject the call outright.
`ApproveWith(arguments=..., digest=...)` approves a pending call and runs it with the
reviewer's arguments:

```python
from nexura_agents import Agent, ApproveWith, FakeModel, Runner, ToolCall, tool


@tool(requires_approval=True)
def refund(order_id: str, amount: int) -> str:
    """Refund an order."""
    return f"refunded {amount} on {order_id}"


ask = [ToolCall(id="c1", name="refund", arguments={"order_id": "A7", "amount": 500})]
agent = Agent(
    name="Support",
    instructions="Handle refunds.",
    model=FakeModel(responses=[ask, "I refunded 100."]),
    tools=[refund],
)
runner = Runner()
paused = runner.run(agent, "Refund order A7")
seen = paused.interruption.pending_digests()["c1"]  # what the reviewer was shown

done = runner.resume(
    agent,
    paused.interruption,
    approve={"c1": ApproveWith(arguments={"order_id": "A7", "amount": 100}, digest=seen)},
)
assert [m.content for m in done.messages if m.role == "tool"] == ["refunded 100 on A7"]
```

- The edited arguments are checked against the tool's schema before anything runs. If
  they do not fit, or the call is not pending, the resume raises `ApprovalDecisionError`.
- `digest` binds the approval to the original action the reviewer saw, like a plain
  digest approval. Leave it out to approve by call id only.
- The call in the transcript is rewritten with the edited arguments, so the history (and
  the saved session) shows what actually ran. The model sees that call and its result.
- Edits and plain approvals (`{"c2": digest}` or call ids) can be mixed in one `approve`
  mapping. Questions from `ask_user` are answered with `answers=`, not edited.

## Asking the user a question

Approval answers "may the agent do this?". Sometimes the agent needs information only the
user has instead, such as a booking size or which account to use. `Agent(ask_user=True)`
gives the model an `ask_user(question)` tool. Calling it pauses the run with the question,
and you resume with the answer:

```python
from nexura_agents import Agent, FakeModel, Runner, ToolCall

question = [ToolCall(id="q1", name="ask_user", arguments={"question": "For how many people?"})]
agent = Agent(
    name="Concierge",
    instructions="Book restaurant tables.",
    model=FakeModel(responses=[question, "Booked a table for 4 at 8 pm."]),
    ask_user=True,
)
runner = Runner()
paused = runner.run(agent, "Book dinner tonight")
assert paused.status == "paused"
assert paused.interruption.questions == [{"id": "q1", "question": "For how many people?"}]

done = runner.resume(agent, paused.interruption, answers={"q1": "4 people"})
assert done.output == "Booked a table for 4 at 8 pm."
```

- `interruption.questions` lists the pending questions as `{"id", "question"}`. The answer
  text becomes the tool result the model sees.
- It uses the same pause as approvals, so it works with `to_json()`/`from_json()` across
  processes, with sessions and with durable checkpoints. A batch can hold questions and
  approval-gated calls together: answer the questions and approve or reject the rest in
  one `resume`.
- Only calls to the built-in tool count as questions. A tool of your own that happens to be
  named `ask_user` is approved or rejected like any other, and `answers=` cannot supply
  its result.
- Every pending call needs exactly one decision. Questions can be answered or rejected
  (`reject={"q1": "prefers not to say"}`), but not approved; `approve=True` skips them.
  Answers must be text, and an answer for a call that is not a pending question raises
  `ApprovalDecisionError`.
- In a handoff, the agent that asks must have `ask_user=True`; resume with the starting
  agent as usual. Over MCP, or inside `as_tool()`, a question cannot be answered, so it
  fails like an approval would.

## Usage limits across a pause

`requests` is saved in the interruption and restored on resume, so `UsageLimits.request_limit` counts model requests across the pause instead of starting again at zero. See [Guardrails, hooks and limits](guardrails-hooks-limits.md).

## Async and streaming

`Runner.resume_async` is the async form. `Runner.resume_stream_async` yields the same events as `stream_async` after the resume point. A paused streaming run ends with a `RUN_PAUSED` event whose data includes the `interruption`.

## What approvals cover, and what they do not

Approvals protect the actions you mark or that declare writes, not every line of Python:

- **Agent tools** pause only when they set `requires_approval` (MCP, OpenAPI and module
  tool sets also accept `requires_approval="writes"`, which pauses every tool not marked
  read-only; see [MCP and OpenAPI](mcp-and-openapi.md)). A plain `@tool`
  function that writes to an external system is not paused unless you set
  `requires_approval=True`; the SDK cannot tell from arbitrary Python that it writes.
- **Workflow nodes** pause when `requires_approval=True`, when the engine's
  `approval_required_handlers` names them, or when a module declares write operations
  (supported connectors, `nexura.http.request` other than GET or HEAD, channels,
  `nexura.sql.execute`, the sandbox module, OpenAPI writes). The approval is bound to the
  node's resolved inputs: a `resume(state_update=...)` that changes them needs a fresh
  approval, for module, function, mapped, parallel and sub-workflow nodes alike. It also
  expires (`approval_ttl`; the shipped stores refuse to claim an expired approval and the
  engine checks again right before the node runs) and can be claimed once. Answering an
  `interrupt()` does not approve a node that was never approved. The pending approval
  carries `canonical_payload_json`, the exact text whose SHA-256 is its `payload_digest`,
  so a reviewer (and Studio's approval screen) sees the bytes being approved; rows stored
  before this field existed return an empty string. When an edit re-gates a node, the
  fresh request records `supersedes_approval_id`, `regate_reason` (`"inputs_edited"`) and
  `previous_input_state`, so a reviewer who never saw the first request still sees what
  changed. See
  [Workflows](workflows.md#approval-gates).
- **The low-level `ApprovalGate`** (`nexura_runtime.runtime.approvals`) binds each
  request to its dispatch identity: workspace, run, node, spec digest, action kind, action
  name and payload digest. Presenting an approved id for any other value raises
  `ApprovalIdentityMismatchError`; a request past `expires_at` raises `ApprovalExpiredError`
  even if it was approved in time (the claim checks the manager's own clock atomically; a
  `now` argument can only move it later, never back before `expires_at`); a request stored
  before identity binding (schema migration 5) raises `ApprovalIdentityMissingError` and is
  never dispatched, so create a fresh request for it. More rules:
  - `gate.wrap_tool(tool)` approves every argument of the call: positional arguments are
    bound to the tool's signature by name, so `tool.run(1)` and `tool.run(amount=1)` need
    the same approval and `tool.run(1_000_000, approval_id=...)` is refused for an
    approval of `tool.run(1)`. Extra positional arguments (`*extra`) appear in the payload
    under `"*extra"`, a key no keyword argument can take, so `tool.run(1, "memo")` and
    `tool.run(1, extra=["memo"])` need different approvals. A tool whose signature cannot
    be inspected accepts only keyword arguments through the gate. A call whose arguments
    cannot be bound to the signature, or cannot be canonicalised (a non-string mapping key,
    NaN, an unsupported type), is refused with `ApprovalError` before any request is
    created.
  - A wrapped tool called without `context=` gets a run id unique to that wrapped object,
    so another tool with the same name cannot use its approvals.
  - `ApprovalPolicyManager.create_request` only stores a `pending` request with no
    decision and no claim; decisions go through `submit_decision`.
  - `ApprovalModule` (`nexura.runtime.approvals`) works only in `context.workspace_id` (a
    `workspace_id` input naming another workspace is refused) and binds `create` to
    `context.run_id`. `decide` records `context.principal_id` as the decider and takes the
    role from `context.attributes["principal_role"]`; it is refused when the context has
    no role or when `decided_by_principal_id` / `decided_by_role` inputs disagree.
  - `AgentCoordinator` tool approvals use a stable id derived from the workspace, run,
    node, agent, handoff count, turn, call index, tool name and argument digest (not the
    provider's tool-call id, which changes on every response), so re-running the same run
    after approval claims it when the model repeats the same call; a different call asks
    for its own approval; a further re-run raises `ApprovalClaimConflictError` (the call is not sent
    twice, and the coordinator does not checkpoint). Coordinator and
    `AgentDefinition.as_tool` approvals are bound to `AgentDefinition.definition_digest()`,
    so editing the agent after the request invalidates them, and a nested subagent asks
    for its own approval instead of inheriting its parent's `approval_id`.
- **Trusted code.** Python that builds `ExecutionContext(approval_granted=True)` itself, or
  calls a module directly with it, is part of your trusted code and is not gated. Keep such
  calls out of reach of model output and flow input.

The digests are not signatures: whoever can edit your stored runs or approval database can
change what is pending. Protect that storage like any other record that grants authority.

The REST run detail (`GET /api/v1/runs/{run_id}`) returns the pending approval's
complete `input_state` and `canonical_payload_json`. Approval views have no key-count,
list-length, text-length or depth truncation. Unlike bounded diagnostic events, they
preserve ordinary action values even under names such as `api_key` or `password`.
Only known stored credential values in the run's workspace and values matching the
secret-value detector are masked as `[REDACTED]`. Redaction is heuristic, not a general
personal-data detector. When masking occurs, the displayed canonical JSON differs from
the stored bytes: `payload_digest` continues to identify the original approved payload.
Review the entire pending action before deciding. REST rejection claims the same engine
approval as acceptance, persists a terminal rejection checkpoint, and executes no gated
node; concurrent decisions cannot both claim it.

## See also

- [Tools](tools.md): `requires_approval` on `@tool`, `SchemaTool` and module tools.
- [Guardrails, hooks and limits](guardrails-hooks-limits.md)
- [Workflows](workflows.md): durable approvals and `interrupt()` in graphs.
- [Testing your agents](testing.md)

## Security model and limits

- Workflow JSON never contains code; only registered handlers run.
- Approvals cover supported connector writes and approval-gated workflow nodes. Each is
  bound to a digest of what the approver saw (a workflow node's resolved inputs; for the
  low-level `ApprovalGate`, the run, node, spec digest, action kind, action name and
  payload, where a wrapped tool's payload is every argument, positional ones included)
  and to its expiry, which is enforced when the approval is claimed and again before the
  node runs; an edited input needs a fresh approval and duplicate decisions return 409.
  Canonical digests refuse non-string mapping keys. A plain `@tool` function is gated only
  with `requires_approval=True`, and Python code that sets `approval_granted` on an
  `ExecutionContext` itself is trusted. Details and limits: [SECURITY_MODEL.md](../SECURITY_MODEL.md).
- Side-effecting handlers and tools are never retried automatically. A crash during
  a side effect leaves its outcome uncertain; it is recorded, not replayed.
- Unknown token usage and unknown pricing stay unknown instead of becoming zero.
- The open-source edition is single-user: one principal, the local owner, with no logins,
  tokens or API keys. Authentication (the `authenticated-shared-service` profile) is in the
  Nexura enterprise edition and has not been through an external security review.

Full threat model, controls and open gaps: [SECURITY_MODEL.md](../SECURITY_MODEL.md).
Report vulnerabilities as described in [SECURITY.md](../SECURITY.md).

## A real agent in 40 lines

A refund agent with typed dependencies, a typed result, self-correcting tools, approval
for large refunds, and hard usage limits.

```python
from dataclasses import dataclass, field

from pydantic import BaseModel

from nexura_agents import (
    Agent,
    FakeModel,
    ModelRetry,
    RunContext,
    Runner,
    RunInterruption,
    ToolCall,
    UsageLimits,
    tool,
)


@dataclass
class Shop:  # your database, clients, current user...
    orders: dict[str, float]
    refunds: list[str] = field(default_factory=list)


class Decision(BaseModel):  # the agent must return exactly this
    order_id: str
    refunded: float


@tool
def get_order(ctx: RunContext[Shop], order_id: str) -> float:
    """Return the order total."""
    if order_id not in ctx.deps.orders:
        raise ModelRetry(f"No order {order_id}. Order IDs look like A1.")  # model retries
    return ctx.deps.orders[order_id]


@tool(requires_approval=lambda ctx, args: args["amount"] > 100)  # big refunds wait
def refund(ctx: RunContext[Shop], order_id: str, amount: float) -> str:
    """Refund an order."""
    ctx.deps.refunds.append(f"{order_id}:{amount}")
    return "refunded"


model = FakeModel(
    responses=[  # stands in for "vertex:gemini-2.5-flash"
        [ToolCall(id="1", name="get_order", arguments={"order_id": "A1"})],
        [ToolCall(id="2", name="refund", arguments={"order_id": "A1", "amount": 250.0})],
        '{"order_id": "A1", "refunded": 250.0}',
    ]
)
agent = Agent(
    name="Refunds",
    instructions="Resolve refund requests.",
    model=model,
    tools=[get_order, refund],
    output_type=Decision,
)
runner = Runner(usage_limits=UsageLimits(request_limit=8))
shop = Shop(orders={"A1": 250.0})

result = runner.run(agent, "Refund order A1 in full", deps=shop)
assert result.status == "paused" and shop.refunds == []  # nothing happened yet
saved = result.interruption.to_json()  # store it anywhere

# ...later, maybe in another process, after a human reviews paused.pending:
paused = RunInterruption.from_json(saved)
result = runner.resume(agent, paused, approve=True, deps=shop)  # or reject={id: "reason"}
print(result.output)  # order_id='A1' refunded=250.0
print(shop.refunds)  # ['A1:250.0']
```

Rejected calls never run; the model is told why and can respond. For a review UI, show
`paused.pending_digests()` alongside each call and pass back `approve={call_id: digest}`:
if anyone edits the saved run in between, `resume` refuses, because each approval is
bound to a SHA-256 digest of the exact tool name and arguments. Digests use canonical
JSON (`nexura_agents.contracts.canonical`), which refuses non-string mapping keys rather
than converting them, so `{1: x}` and `{"1": y}` can never share a digest.
