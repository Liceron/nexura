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

A hook can also require approval without changing the tool: `AgentHooks.tool_needs_approval(ctx, tool, arguments)` returning `True` gates the call exactly as `requires_approval` would (same pause, digests, expiry, single use, resume and recovery rules). Hooks add gates only; they can never lift a tool's own `requires_approval`. A hook's gate is recorded in the run's journal (with the pause, and again with the approval claim of a resume), so a runner without that hook (another process, or one built without the policy) that resumes or recovers the run after a crash still waits for a person, and an expired claim is asked for again. Runs paused before this release carry no record: keep the hook on the runners that continue them. An operator policy installed with `set_default_hooks` applies to every runner and to nested runs. See [Adding approval gates](guardrails-hooks-limits.md#adding-approval-gates) and [Policies](guardrails-hooks-limits.md#policies-default-hooks-for-every-run).

## What a RunInterruption holds

`RunInterruption` is a dataclass. The fields you will use:

- `pending`: the `ToolCall`s waiting for a decision (`id`, `name`, `arguments`).
- `run_id`, `agent_name`: which run and agent it belongs to.
- `interruption_id`, `expires_at`: unique approval identity and Unix expiry time.
  `Runner(approval_ttl=3600)` sets the TTL in seconds (one hour by default).
- `action_digests`, `signature`: original action hashes and optional HMAC-SHA256.
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
reviewed = paused.interruption.pending_digests()  # retain what the reviewer actually saw

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
final = Runner().resume(agent, restored, approve=reviewed)
assert final.output == "Done."
assert "deleted u9" in [m.content for m in final.messages if m.role == "tool"]
```

The JSON stores the transcript, pending calls, counters, usage (including cache and
reasoning counts), expiry and signature. Keep unsigned JSON in trusted server-side
storage. Keep reviewed digests separately from any client-submitted interruption;
recomputing them from that submission does not prove what was reviewed.

`from_json` raises `ValueError` if the document has an unsupported `schema_version`.

## Decisions: approve, reject, or both

`approve` accepts three forms:

- `True`: approve every pending call that is not in `reject`, after verifying a
  signed or stored copy. SDK-issued in-memory interruptions carry a trusted snapshot
  through agents, teams and streams, even when another Runner continues them.
- A list of call IDs, with the same signed/stored-copy requirement.
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

reviewed = interruption.pending_digests()  # captured when showing the actions
final = Runner().resume(
    agent, interruption, approve={"p1": reviewed["p1"]}, reject={"p2": "duplicate invoice"}
)
assert ran == ["INV-1"]
tool_messages = [m.content for m in final.messages if m.role == "tool"]
assert any("duplicate invoice" in text for text in tool_messages)
```

The SDK does not record that an interruption was already resumed. If a stored interruption must be used only once, enforce that in your own storage.

## Bind approval to the exact arguments

A reviewer approves a specific action and approval occurrence. `pending_digests()` returns
`{call_id: sha256}` where the digest covers the tool name, canonical JSON arguments,
any trusted tool scope, run id and SDK-generated interruption id. Show the arguments
to the reviewer, keep the digest, and pass it back as `approve={call_id: digest}`.
A later pause needs its own decision even for identical arguments and a reused model
call id. Recovery retains the interruption id of the same pause and accepts that
occurrence's original reviewed decision. This applies to ordinary approval tools as
well as scoped harness tools. The journal's stable effect hash is separate from the
reviewed decision hash; `RunInterruption.action_digest()` is not an approval grant.

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

On resume the runner also checks that pending calls match the transcript and stored
action digests. With a trusted stored copy, the entire submitted envelope must
match it. With a signing secret, any change to the envelope invalidates its HMAC.
Unsigned JSON must remain in trusted server-side storage even when the caller
provides reviewed action digests: those bind tool arguments, not arbitrary history
or run state.

Serialization drops the private in-memory snapshot. The originating Runner can
still verify a JSON roundtrip against its own retained copy. A fresh Runner given
unsigned JSON without a configured store cannot use `approve=True`: return
`approve={call_id: reviewed_digest}` instead, or configure a store/signing secret.
This digest-only option applies to individual run interruptions. Teams require an
authenticated **whole team envelope** for every resume, including rejection-only
decisions: tool digests alone cannot authenticate completed outputs, usage or the
next member's prompt. A fresh runner loading team JSON must share the signing key
or approval store. The originating runner can also verify its retained copy.

## Expiry, authenticity and single use

```python
import secrets
from nexura_agents import Runner
from nexura_agents.durable import SQLiteCheckpointStore

# Keep the same secret securely across processes/restarts; do not send it to clients.
secret = secrets.token_bytes(32)
store = SQLiteCheckpointStore("approvals.sqlite")
runner = Runner(
    checkpoint_store=store,
    approval_ttl=900,
    approval_signing_secret=secret,
)
```

`expires_at` is checked before resume and again before consumption. Expired
interruptions raise `ApprovalDecisionError`; JSON from before expiry was recorded
loads with expiry zero and cannot resume. Obtain a fresh approval rather than
extending a client-submitted deadline.

HMAC-SHA256 authenticates canonical interruption JSON containing run id, starting
and active agent, pending call ids, tool names, argument digests and expiry. It also
covers the full transcript, state and counters so a client cannot change the next
model's inputs. Every resume with a configured secret requires a valid signature,
including when a store is also present. This authenticates the issued envelope; it
does not authenticate the human reviewer, encrypt the contents or prevent replay.
The application owns reviewer authorization and must display the same envelope it
submits for approval. Anyone holding the secret can issue envelopes.

With built-in `MemoryCheckpointStore` or `SQLiteCheckpointStore`, resume atomically
marks the matching, unexpired interruption consumed **before** dispatch. Racing
resumes have one winner. SQLite tombstones survive process restart and checkpoint
cleanup; the memory store lasts only for the life of that shared store object.
Invalid decisions/edits do not consume the approval. A failure after consumption
does not refund it: investigate the durable checkpoint/uncertain outcome instead
of replaying the grant.

With these stores, resume first checks the run's execution journal without writing: an
approval that was already used, or claimed by an earlier resume, is refused there. A
resume that cannot take the run, because another resume holds it, consumes nothing. It
then writes a claim to the run's execution journal: the approval id, its expiry and your
decisions, including edits and rejections. Next it consumes the approval, and each
approved call's step intent records the approval it was sent under.

If the process dies after the claim and before anything was sent, use
`runner.recover_async(agent, run_id)` instead of resuming again. Recovery dispatches
under the same approval after checking it again, or pauses with a fresh interruption
once that approval has expired. A second resume with the claimed approval is refused
and points to `recover()`.

With these stores your decisions are recorded with the run, so they must be
representable as JSON: `ApproveWith` arguments, rejection reasons, answers and
`ResolvedOutcome` results may hold only strings, finite numbers, booleans, `None`, lists
and mappings with string keys. Tuples are turned into lists. Anything else (NaN, a
non-string key, an arbitrary object) makes resume raise `ApprovalDecisionError` before it
takes the run or consumes anything; resume again with JSON values. An approval whose
calls were already sent under another approval is refused.

Your rejections and answers hold across a crash. A call you rejected is never sent by
recovery, and a call to a tool that requires approval with no decision on record is
asked for again rather than sent. See
[Durable execution](durable-execution.md#budgets-and-approvals-across-a-takeover-s11).

For single use without run checkpointing, pass `approval_store=store`. Custom
checkpoint stores must implement `nexura_agents.approvals.ApprovalStore` or provide
a separate approval store; missing atomic methods fail closed. Consumption records
must be retained for the lifetime of their grants, independently of run cleanup.
Without a configured approval/checkpoint store, **single use is the caller's
responsibility**, including for signed interruptions. A Runner's convenience
snapshot is neither persistent storage nor a cross-process replay ledger.
The convenience cache removes expired entries and retains at most 256 envelopes
in least-recently-used order. SDK-issued objects also carry their private trusted
snapshot; serialization discards it.

`TeamInterruption` has its own identity, expiry and signature. Its canonical payload
covers the team name, original prompt, nested prompts, every completed output and
text, completed usage, and the full member interruptions. The team's expiry is
the earliest pending member expiry. The same approval store atomically consumes
the team grant and each member grant; a member signature alone cannot authorize
altered team state.

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
  digest approval. Leave it out only when Runner verifies a signed/stored copy.
- The call in the transcript is rewritten with the edited arguments, so the history (and
  the saved session) shows what actually ran. The model sees that call and its result.
- Edits and plain digest approvals (`{"c2": digest}`) can be mixed in one `approve`
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
  - A coordinator tool call also needs approval when its tool has `requires_approval` or
    a hook's `tool_needs_approval` says so, not only when the agent has
    `approval_required`. A tool called inside an `AgentDefinition.as_tool()` subagent that
    needs approval raises `AgentApprovalRequiredError` before anything runs (a subagent
    cannot wait for a person inside the delegation).
  - The coordinator continues after an approval by running the run again (same
    `ExecutionContext`). With an `approval_gate`, a ledger records what each run visit
    did. A visit is the workspace, run, node and workflow step (`step_key`, else
    `attempt_id`), so a node visited again in a cycle starts fresh. The ledger records
    tool calls by position (agent and its definition digest, handoff count, turn, index)
    with their tool, argument digest and result, and the model's responses. It lives in
    the gate's `ApprovalPolicyManager` database (table `coordinator_call_ledger`), or in
    memory for one `AgentCoordinator` object when the gate has no database. On the re-run:
    - a call that already ran is replayed from its result, never executed again, so a
      run with several gated calls completes, each approval claimed once;
    - a call that was dispatched but has no result (the process stopped, the tool raised)
      is sent again only when its tool is `pure` or `idempotent`;
    - if the model asks for a different call at a position where a call already ran, or
      asks again for an ungated, non-idempotent call an earlier attempt already sent, the
      run stops with `CoordinatorCallOutcomeUnknownError` and sends nothing (check the
      external system, then start a new run);
    - `AgentCoordinator(replay_model_responses=True)` replays the recorded model responses
      instead of calling the model again. The re-run then follows exactly the recorded
      path, is not billed again for those turns, and continues live after it. It is off
      by default because a re-asked model may legitimately propose a different call,
      which then asks for its own approval.

    Replayed steps emit `coordinator.tool_replayed` / `coordinator.model_replayed`
    progress events on the context's event sink (`executed: False`, no arguments or
    results) and a `nexura.coordinator.replayed` trace event; hooks are not called for a
    replay. When a run visit completes, its entries are deleted and only a completion
    marker stays. Running it again raises `ApprovalClaimConflictError` when it used
    approvals, else `CoordinatorRunCompletedError`. `purge_coordinator_ledger(manager,
    older_than_seconds)` removes old rows, markers included; run it periodically with a
    TTL longer than your approval expiry.

    Without a database, a re-run through another coordinator object or in another
    process cannot see what ran, and ungated calls before the gate run again: configure
    the gate with an `ApprovalPolicyManager` database for coordinator flows that pause.
    For long multi-step gated flows, the SDK `Runner` with a checkpoint store is the
    better fit (it resumes instead of re-running).
  - A `tool_to_module` module whose tool has `requires_approval` runs a gated call only
    with `approval_granted`: as a workflow node without its own approval it pauses for
    approval (`request_approval` with the tool name and the arguments) and runs after a
    person approves those arguments (compared by canonical digest, so `1`, `1.0` and
    `True` differ); called anywhere else it raises `ContractError` (`APPROVAL_REQUIRED`).
    Through `module_tool` the rule gets the agent run's context, with its `deps`; as a
    workflow node there are no deps, so a rule that reads `ctx.deps` sees `None` there.
- **Trusted code.** Python that builds `ExecutionContext(approval_granted=True)` itself, or
  calls a module directly with it, is part of your trusted code and is not gated. Keep such
  calls out of reach of model output and flow input.

Action digests are not signatures. Optional SDK HMAC envelopes protect client-carried
state; runtime gate/workflow approvals rely on trusted storage. Protect approval
databases and signing secrets like any other record that grants authority.

Saved-agent workflow nodes accept the workflow's validated, single-use grant for
the stored tool calls even when the SDK's default one-hour snapshot window has
elapsed. After the workflow checks its own TTL and claims the grant, the adapter
opens a new SDK dispatch window bounded by the node timeout and remaining execution
time. This does not extend the workflow grant's lifetime. Direct SDK interruptions
still require their original unexpired approval.

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
reviewed = result.interruption.pending_digests()  # retained from the human review

# ...later, maybe in another process, after a human reviews paused.pending:
paused = RunInterruption.from_json(saved)
result = runner.resume(agent, paused, approve=reviewed, deps=shop)  # or reject={id: "reason"}
print(result.output)  # order_id='A1' refunded=250.0
print(shop.refunds)  # ['A1:250.0']
```

Rejected calls never run; the model is told why and can respond. For a review UI, show
`paused.pending_digests()` alongside each call and pass back `approve={call_id: digest}`:
if anyone edits the saved run in between, `resume` refuses, because each approval is
bound to a SHA-256 digest of the exact tool name and arguments. Digests use canonical
JSON (`nexura_agents.contracts.canonical`), which refuses non-string mapping keys rather
than converting them, so `{1: x}` and `{"1": y}` can never share a digest.
