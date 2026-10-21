# Workflows

A workflow is a graph of named steps that run in order and share one JSON state. The
engine saves a checkpoint after every step, so a run can pause for a human, survive a
process restart, retry a flaky step, or be forked from an earlier point. Everything on
this page lives in `nexura_runtime.workflow` and runs without a model or a network.

## A first workflow

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


@registry.register_handler("lookup")
def lookup(state: dict) -> dict:
    return {"total": 4500 if state["order_id"] == "ORD-1" else 0}


@registry.register_handler("greet")
async def greet(state: dict) -> dict:
    return {"message": f"Order {state['order_id']} total: {state['total']}"}


spec = WorkflowSpec(
    name="Demo",
    entry_node="lookup",
    nodes={
        "lookup": WorkflowNode(id="lookup", handler="lookup", default_transition="greet"),
        "greet": WorkflowNode(id="greet", handler="greet"),
    },
    terminal_nodes=["greet"],
)


async def main() -> None:
    engine = WorkflowEngine(spec, registry, store=MemoryWorkflowStore())
    result = await engine.run({"order_id": "ORD-1"})
    assert result.status == "completed"
    assert result.state["message"] == "Order ORD-1 total: 4500"
    assert result.step_count == 2


asyncio.run(main())
```

The parts:

- `WorkflowSpec(name, entry_node, nodes, terminal_nodes=[...])` describes the graph. It
  is plain data and can be saved as JSON (see [JSON specs](#json-specs)).
- `WorkflowNode(id, handler, ...)` is one step. `handler` is a name looked up in the
  registry. The step's next node is `default_transition`, or a `transitions` entry
  chosen by a branch (below).
- `ModuleRegistry` maps names to handlers. A handler is a sync or async function that
  takes the current state dict and returns a dict of updates (or `None`). Sync handlers
  run in a worker thread. The engine merges the update into the state, so a handler
  never has to return the whole state.
- `WorkflowEngine(spec, registry, store=...)` runs the spec. Creating it validates the
  spec against the registry, so an unknown handler name fails at construction.
- `engine.run(initial_state, workflow_run_id=None)` returns a `WorkflowRunResult` with
  `status` (`completed` or `paused` from `run`; `rejected` after a rejected resume; failures raise), `state`, `step_count`,
  `current_node_id`, `pending_approval` and `interrupt`.

State and updates must be JSON-serializable. Returning something else fails the step
with a `WorkflowExecutionError`. A run ends when a node has no next node, or the next
node is a name listed in `terminal_nodes` that is not a real node. A terminal node
that has a handler still runs first.

A `workflow_run_id` you pass must be new in the store: reusing one raises
`WorkflowError` (or `WorkflowTerminalError` if that run already finished).

## Branches

A handler picks the next node by returning a branch name. Either put `_branch` in the
returned dict or return a `HandlerOutput`. The name must be a key in the node's
`transitions`, otherwise the step fails.

```python
import asyncio

from nexura_runtime.workflow import (
    HandlerOutput,
    ModuleRegistry,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
)

registry = ModuleRegistry()


@registry.register_handler("triage")
def triage(state: dict) -> dict:
    return {"_branch": "big" if state["amount"] > 1000 else "small"}


@registry.register_handler("auto")
def auto(state: dict) -> HandlerOutput:
    return HandlerOutput(state_update={"route": "auto"})


@registry.register_handler("manual")
def manual(state: dict) -> dict:
    return {"route": "manual"}


spec = WorkflowSpec(
    name="Triage",
    entry_node="triage",
    nodes={
        "triage": WorkflowNode(
            id="triage", handler="triage", transitions={"small": "auto", "big": "manual"}
        ),
        "auto": WorkflowNode(id="auto", handler="auto"),
        "manual": WorkflowNode(id="manual", handler="manual"),
    },
    terminal_nodes=["auto", "manual"],
)


async def main() -> None:
    engine = WorkflowEngine(spec, registry)
    assert (await engine.run({"amount": 50})).state["route"] == "auto"
    assert (await engine.run({"amount": 5000})).state["route"] == "manual"


asyncio.run(main())
```

`HandlerOutput(state_update={...}, branch="name")` is the explicit form of the same
thing. A handler may also return a `(dict, branch)` tuple. `_branch` is removed from the
update before it is merged, so it never appears in the state. With no branch, the
engine follows `default_transition`, then `transitions["default"]`, then ends the run.

Cycles are allowed (a branch can go back to an earlier node). `max_steps` bounds them;
see [Limits and timeouts](#limits-and-timeouts).

## Stores and durability

Without a store, a run cannot pause or resume. With one, every step writes a
checkpoint.

- `MemoryWorkflowStore()` keeps checkpoints in the process. Use it for tests.
- `SQLiteWorkflowStore(path)` writes to a SQLite file (WAL mode). Close it with
  `await store.close()`, or use it as a context manager (`with` or `async with`).
- `nexura_postgres.PostgresWorkflowStore(dsn)` (package `nexura-postgres`) keeps the same
  checkpoints in Postgres, for several hosts sharing one database; see
  [Workflows on Postgres](durable-execution.md#workflows-on-postgres-s8).

A new engine built on the same database picks up where the old one stopped. The spec
must be the same: resuming checks the spec's hash and raises
`WorkflowSpecMismatchError` if it changed.

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
refunds: list[str] = []


@registry.register_handler("lookup_order")
def lookup_order(state: dict) -> dict:
    return {"amount": 4500}


@registry.register_handler("process_refund")
def process_refund(state: dict) -> dict:
    refunds.append(state["order_id"])
    return {"refund_id": "REF-9901"}


spec = WorkflowSpec(
    name="Refund",
    entry_node="lookup",
    nodes={
        "lookup": WorkflowNode(id="lookup", handler="lookup_order", default_transition="gate"),
        "gate": WorkflowNode(id="gate", handler="process_refund", requires_approval=True),
    },
    terminal_nodes=["gate"],
)


async def main() -> None:
    db = Path(tempfile.mkdtemp()) / "workflows.sqlite3"

    store = SQLiteWorkflowStore(db)
    paused = await WorkflowEngine(spec, registry, store=store).run({"order_id": "ORD-1"})
    assert paused.status == "paused" and refunds == []
    await store.close()  # the process "exits" here

    # A new store and engine on the same file, as after a restart.
    store = SQLiteWorkflowStore(db)
    done = await WorkflowEngine(spec, registry, store=store).resume(
        paused.workflow_run_id, paused.pending_approval.approval_id, decision="approve"
    )
    assert done.status == "completed" and done.state["refund_id"] == "REF-9901"
    assert refunds == ["ORD-1"]
    await store.close()


asyncio.run(main())
```

### Recovering after a crash

Both stores keep an **execution journal** next to the checkpoints (the
[durable execution contract](durable-execution.md); `SQLiteWorkflowStore` keeps it in the
same file). A run holds a lease from its first write, renewed every `lease_ttl / 3`
(`WorkflowEngine(lease_ttl=30.0)`). Before a step that is not `pure` runs, the engine
records that it is about to dispatch it (an intent); after it returns, its outcome and the
next node are recorded together, then the checkpoint moves. Checkpoint saves are fenced by
a revision, so a stale worker cannot overwrite a newer checkpoint.

`await engine.recover(run_id)` continues a run whose process stopped. It takes the run's
lease (at once when the owner was a process on this host that has exited, otherwise once
the lease has expired; while another worker holds it, it raises `WorkflowError`) and then
looks at the step at the cursor:

- the step never started: it runs;
- its outcome was recorded but the checkpoint was not saved: the outcome is used and the
  step is **not** run again;
- it started and its outcome is unknown: what happens depends on its effect class.
  `pure` steps run again; `idempotent` steps run again with the same idempotency key;
  `reconcilable` steps are asked what happened; `non_idempotent` steps are never run again
  silently: the run pauses with `pause_reason="effect_unknown"` and a `pending_effect`
  (`recover(run_id, on_uncertain="fail")` fails the run instead).

Declare a handler's class when you register it:
`registry.register_handler("lookup", effect="pure")`, `effect="idempotent"`,
`effect="non_idempotent"`, or `effect="reconcilable", reconcile=fn` where
`fn(state, intent)` returns `Applied(output)`, `NotApplied()` or `Unknown()` (from
`nexura_agents.contracts.execution`). **A function handler that declares nothing is
`non_idempotent`.** A module uses its manifest's `effect_policy` (`pure` / `read`,
`idempotent_write`, `non_idempotent_write`). Handlers get a stable key to pass on to the
system they call: `ctx.idempotency_key()` in modules, `step_idempotency_key()` (from
`nexura_runtime.workflow`) in functions; it is the same when the step runs again after a
crash and different for a new visit of the node. `ctx.fencing_token` /
`step_fencing_token()` is the lease's fencing token.

A person resolves a paused `effect_unknown` run with
`await engine.resolve_effect(run_id, resolution, actor=..., result=...)`, which records the
decision once and continues the run:

- `"confirmed"`: it happened; `result` (a dict of state updates) is used as the step's
  output and the step does not run again;
- `"not_applied"`: it did not happen; the step runs again as a new attempt with a new key.
  If the step needs approval, a fresh approval is asked for first;
- `"failed"`: the run fails.

The run service's `POST /api/v1/runs/{run_id}/effects/{effect_id}/resolve` records the same
decision; `recover(run_id)` then continues the run.

Approvals survive a crash too. A paused run holds no lease, so any process can resume it.
If a process stops after it claimed an approval but before the approved step dispatched,
`recover` checks the approval again (same step, same inputs, not expired) and runs the
step under it; an expired or changed approval is asked for again. Once the step has
dispatched, the approval is used up: a `not_applied` resolution needs a fresh one.

```python
import asyncio

from nexura_runtime.workflow import (
    MemoryWorkflowStore,
    ModuleRegistry,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
)

payments: list[str] = []


class ProcessStopped(BaseException):
    """Stands in for the process dying in the middle of the payment."""


registry = ModuleRegistry()


@registry.register_handler("lookup", effect="pure")
def lookup(state: dict) -> dict:
    return {"amount": 4500}


@registry.register_handler("pay")  # undeclared: non_idempotent
def pay(state: dict) -> dict:
    payments.append(state["order_id"])  # the remote system applied it ...
    if len(payments) == 1:
        raise ProcessStopped  # ... and the process died before the result was saved
    return {"paid": True}


spec = WorkflowSpec(
    name="Pay",
    entry_node="lookup",
    nodes={
        "lookup": WorkflowNode(id="lookup", handler="lookup", default_transition="pay"),
        "pay": WorkflowNode(id="pay", handler="pay"),
    },
    terminal_nodes=["pay"],
)


async def main() -> None:
    store = MemoryWorkflowStore()
    try:
        await WorkflowEngine(spec, registry, store=store).run(
            {"order_id": "ORD-7"}, workflow_run_id="pay-1"
        )
    except ProcessStopped:
        pass

    engine = WorkflowEngine(spec, registry, store=store)  # after the restart
    paused = await engine.recover("pay-1")
    assert paused.status == "paused" and paused.pause_reason == "effect_unknown"
    assert payments == ["ORD-7"]  # not sent a second time

    # Someone checks the payment provider: the payment went through.
    done = await engine.resolve_effect(
        "pay-1", "confirmed", actor="ops@example.test", result={"paid": True}
    )
    assert done.status == "completed" and payments == ["ORD-7"]


asyncio.run(main())
```

#### Parallel, map and sub-workflow steps

The parts of a composite step are recorded one by one. Each parallel branch
(`<node>/<branch>#<step>`) and each mapped item (`<node>#<step>[<item>]`) has its own
intent, outcome and idempotency key (`ctx.idempotency_key()` / `step_idempotency_key()`
inside it return the part's key). A sub-workflow child keeps its own journal under its run
id (`<parent>/<node>#<step>[[<item>]]`, with `parent_run_id` set). When `recover` runs the
step again, it decides per part:

- a part whose outcome was recorded is **not** run again; its recorded update is used;
- a part that never started runs;
- a part that started without an outcome follows its effect class, with its own key:
  `pure` and `idempotent` parts run again with the same key, `reconcilable` parts are
  probed one by one, and a `non_idempotent` part is never repeated: once the other parts
  have finished, the run pauses `effect_unknown` with a `pending_effect` naming the part
  (`"part": "item"` and `"item": 2`, `"part": "branch"` and `"branch": "charge"`, or
  `"part": "subworkflow"` and the child's `"run_id"`);
- a sub-workflow child is recovered under its own journal from the parent's `recover`
  (call `recover` on the top-level run, not on the child).

`resolve_effect` applies to that part only: `"confirmed"` uses `result` as the part's
update, `"not_applied"` sends that part again as a new attempt (`<node>#<step>.2[<item>]`,
a new key; a step that needs approval first asks for a fresh one), `"failed"` fails the run.
If several parts are in doubt, the run pauses on them one at a time. A retry of the step
in the same process (`retry=`) keeps the parts that already finished and runs the failed
ones again with their keys.

```python
import asyncio

from nexura_runtime.workflow import (
    MemoryWorkflowStore,
    ModuleRegistry,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
)

sent: list[int] = []


class ProcessStopped(BaseException):
    """Stands in for the process dying while invoice 2 is being sent."""


registry = ModuleRegistry()


@registry.register_handler("send_invoice")  # undeclared: non_idempotent
def send_invoice(state: dict) -> dict:
    sent.append(state["invoice"])
    if state["invoice"] == 2 and sent.count(2) == 1:
        raise ProcessStopped
    return {"sent": state["invoice"]}


spec = WorkflowSpec(
    name="Invoices",
    entry_node="send",
    nodes={
        "send": WorkflowNode(
            id="send",
            handler="send_invoice",
            map_over="invoices",
            map_as="invoice",
            reducers={"sent": "append"},
            max_concurrency=1,
        )
    },
    terminal_nodes=["send"],
)


async def main() -> None:
    store = MemoryWorkflowStore()
    try:
        await WorkflowEngine(spec, registry, store=store).run(
            {"invoices": [1, 2, 3]}, workflow_run_id="inv-1"
        )
    except ProcessStopped:
        pass

    engine = WorkflowEngine(spec, registry, store=store)  # after the restart
    paused = await engine.recover("inv-1")
    assert paused.pause_reason == "effect_unknown"
    assert paused.pending_effect["item"] == 1  # invoice 2 is in doubt
    assert sent == [1, 2, 3]  # invoice 1 was not sent again; invoice 3 was sent once

    done = await engine.resolve_effect("inv-1", "confirmed", actor="ops", result={"sent": 2})
    assert done.status == "completed" and done.state["sent"] == [1, 2, 3]
    assert sent == [1, 2, 3]


asyncio.run(main())
```

#### Built-in modules use the step's key

`nexura.http.request` sends the step's key as `Idempotency-Key` on writes (POST, PUT,
PATCH, DELETE, OPTIONS) when you give no `idempotency_key` input or header, so its
`retries` input (at most 5; transport errors and 429/5xx answers) can retry a POST or PATCH
inside a workflow step. Outside a workflow step, a POST/PATCH retry without a key is still
refused (`NonIdempotentRetryForbiddenError`). The email and WhatsApp channel modules use
the step's key as the outbox de-duplication key, and connector writes send it as
`Idempotency-Key` (the S3 connector, which signs its headers, does not). An explicit key
always wins.

Limits: `SQLiteWorkflowStore` is for one host; several hosts need
`nexura_postgres.PostgresWorkflowStore`, which behaves the same.
`fork` is unchanged: it starts a new run that runs the forked step again. A child whose
`recover` was asked for with `on_uncertain="fail"` fails, and the parent's step fails like
any failed sub-workflow (`WorkflowExecutionError`).

## Approval gates

Set `requires_approval=True` on a node and the engine pauses before running it. The
result has `status="paused"` and a `pending_approval` holding an `approval_id`, the
`node_id`, and a copy of the state the node would see. Nothing in the gated handler has
run yet.

`engine.resume(run_id, approval_id, decision="approve" | "reject", ...)` continues the
run:

- `approve` runs the gated node and carries on. `state_update={...}` merges extra
  values into the state first (an approver can correct a field). A correction that
  changes what the gated node reads does not run on the old approval: the run pauses
  again with a new `pending_approval` showing the new inputs. What a node reads depends on
  its kind: a module node reads its resolved `input_mapping` inputs (and each mapped
  item's), a function handler reads the whole state (so any changed key re-gates it, also
  for mapped and parallel function steps), and a sub-workflow node reads the inputs it
  passes to the child run. An update that leaves those values unchanged runs on the
  original approval.
- Modules that write to an external system pause for approval without `requires_approval`:
  connectors with write operations, `nexura.http.request` (not GET or HEAD),
  `nexura.channel.email`, `nexura.channel.whatsapp`, `nexura.sql.execute` and OpenAPI
  operations that are not GET or HEAD. The module sees `approval_granted` only after a human
  approves; an `approved` input is ignored. A parallel step binds the approval to the whole
  state (its branches read all of it), so a `state_update` that changes any key needs a new
  approval. One approval covers every branch: `pending_approval.details` lists the branches,
  which of them can write (`write_branch_count`) and an `inputs_digest`.
- `reject` ends the run with `status="rejected"`. The handler never runs. A rejected
  run cannot be resumed.

An approval can be used once. It is claimed atomically, so of two concurrent `resume`
calls with the same id one raises `ApprovalAlreadyClaimedError`. Resuming a run that has
already finished raises `WorkflowTerminalError`.

```python
import asyncio

from nexura_runtime.errors import WorkflowTerminalError
from nexura_runtime.runtime.approvals.errors import WrongWorkspaceError
from nexura_runtime.workflow import (
    MemoryWorkflowStore,
    ModuleRegistry,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
)

registry = ModuleRegistry()
sent: list[dict] = []


@registry.register_handler("send")
def send(state: dict) -> dict:
    sent.append(state)
    return {"sent": True}


spec = WorkflowSpec(
    name="Send",
    entry_node="send",
    nodes={"send": WorkflowNode(id="send", handler="send", requires_approval=True)},
    terminal_nodes=["send"],
)


async def main() -> None:
    store = MemoryWorkflowStore()
    engine = WorkflowEngine(spec, registry, store=store, workspace_id="acme")

    # Reject: the handler never runs.
    first = await engine.run({"to": "a"})
    rejected = await engine.resume(
        first.workflow_run_id, first.pending_approval.approval_id, decision="reject"
    )
    assert rejected.status == "rejected" and sent == []

    # A correction changes what the node reads, so it needs a fresh approval.
    second = await engine.run({"to": "b"})
    corrected = await engine.resume(
        second.workflow_run_id,
        second.pending_approval.approval_id,
        state_update={"to": "b@example.com"},
    )
    assert corrected.status == "paused" and sent == []
    assert corrected.pending_approval.input_state["to"] == "b@example.com"
    approval_id = corrected.pending_approval.approval_id
    done = await engine.resume(second.workflow_run_id, approval_id)
    assert done.status == "completed" and sent[0]["to"] == "b@example.com"
    try:
        await engine.resume(second.workflow_run_id, approval_id)
    except WorkflowTerminalError:
        pass
    else:
        raise AssertionError("a finished run cannot be resumed again")

    # Approvals are bound to the workspace that created them.
    third = await engine.run({"to": "c"})
    try:
        await engine.resume(
            third.workflow_run_id,
            third.pending_approval.approval_id,
            workspace_id="other-team",
        )
    except WrongWorkspaceError:
        pass
    else:
        raise AssertionError("a different workspace must not approve")


asyncio.run(main())
```

`resume` also accepts these checks:

- `workspace_id`: must match the workspace stored on the approval, or
  `WrongWorkspaceError` is raised. When omitted, the engine's own `workspace_id` is used.
- `actor_role` (with `actor`): if the node's `required_role` is `"admin"` (the default)
  and `actor_role` is given and is not `"admin"`, `WrongActorError` is raised. Set a
  different `required_role` on the node to change what is demanded.
- `expected_payload_digest`: if given, must equal the `payload_digest` recorded on the
  pending approval, or `MutatedArgumentsError` is raised.

The pending approval expires `approval_ttl` seconds after it was created (an engine
argument, default 3600). Resuming after that marks the run failed and raises
`ApprovalExpiredError`. The expiry is checked before the claim, inside the claim
(`MemoryWorkflowStore`, `SQLiteWorkflowStore` and `PostgresWorkflowStore` never claim an
approval past its stored `expires_at`) and again right before the approved node runs, so a slow claim cannot run a
node after its approval expired. A pending approval stored without `expires_at` (older
checkpoints) expires `approval_ttl` after its `created_at`; one with no `created_at`
either is treated as expired. Without a store, `resume` raises `WorkflowError`.

`state_update` is applied as JSON, as it would be over HTTP: non-string keys become
strings (so `{1: "x"}` replaces the value at `"1"` and re-gates the node like any other
edit) and keys that collide that way (`{1: "x", "1": "y"}`) raise
`WorkflowValidationError`. Everything that can refuse a resume (this validation, the
approval binding, the sub-workflow lookup) runs before the approval is claimed, so a
refused resume leaves the approval usable.

Passing `approval_required_handlers={"name", ...}` to the engine makes those handlers
(or module ids) gated by policy. A spec that sets `requires_approval=False` on one of
them fails validation, so a spec author cannot switch the gate off.

## Retries

`WorkflowNode(retry={...})` retries a failing node with exponential backoff. The keys:

| Key | Meaning | Default |
|---|---|---|
| `max_attempts` | total tries, 1 to 20 (1 means no retry) | 1 |
| `initial_delay` | seconds to wait after the first failure | 0.5 |
| `max_delay` | cap on the wait | 30.0 |
| `backoff` | multiplier per attempt, at least 1 | 2.0 |
| `retry_on` | exception class names to retry; empty retries any error | `()` |

Unknown keys raise `WorkflowValidationError`. `retry_on` matches class names along the
exception's base classes, so `"ConnectionError"` also catches its subclasses. Timeouts,
interrupts and spec mistakes (`WorkflowValidationError`) are never retried. If the whole
workflow has a `timeout`, a retry whose backoff would pass the deadline is not
attempted.

```python
import asyncio

from nexura_runtime.errors import WorkflowExecutionError
from nexura_runtime.workflow import (
    ModuleRegistry,
    NodeRetryPolicy,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
)

registry = ModuleRegistry()
calls: list[int] = []


@registry.register_handler("flaky")
def flaky(state: dict) -> dict:
    calls.append(1)
    if len(calls) < 3:
        raise ConnectionError("upstream reset")
    return {"ok": True}


@registry.register_handler("always_bad")
def always_bad(state: dict) -> dict:
    raise KeyError("missing")


def one_node(handler: str, retry: dict) -> WorkflowSpec:
    return WorkflowSpec(
        name="r",
        entry_node="n",
        nodes={"n": WorkflowNode(id="n", handler=handler, retry=retry)},
        terminal_nodes=["n"],
    )


async def main() -> None:
    retry = {"max_attempts": 3, "initial_delay": 0.01, "retry_on": ["ConnectionError"]}
    result = await WorkflowEngine(one_node("flaky", retry), registry).run({})
    assert result.status == "completed" and len(calls) == 3

    # KeyError is not in retry_on, so the first failure is final.
    try:
        await WorkflowEngine(one_node("always_bad", retry), registry).run({})
    except WorkflowExecutionError as exc:
        assert exc.node_id == "n"

    policy = NodeRetryPolicy.from_dict({"max_attempts": 4, "initial_delay": 1, "backoff": 2})
    assert [policy.delay(a) for a in (1, 2, 3)] == [1, 2, 4]


asyncio.run(main())
```

A failure that is not retried (or the last one) saves a checkpoint with
`status="failed"` and raises `WorkflowExecutionError`; the original exception is chained
as its cause.

### Refusing retries on non-idempotent steps

A retry repeats the node's side effects. A module whose manifest says
`effect_policy="non_idempotent_write"` (for example, one that moves money) cannot be
retried: building the engine raises `WorkflowValidationError`. The check also covers a
sub-workflow node: retrying the sub-workflow would re-run every step inside it, so it
is refused if a non-idempotent step sits at any depth. Move the retry inside, onto steps
that are safe to repeat.

```python
import asyncio

from nexura_agents.contracts import BaseModule, ExecutionContext, ModuleManifest, ModuleResult
from nexura_runtime.errors import WorkflowValidationError
from nexura_runtime.workflow import ModuleRegistry, WorkflowEngine, WorkflowNode, WorkflowSpec


class SendPayment(BaseModule):
    manifest = ModuleManifest(
        module_id="pay.send",
        version="1.0.0",
        kind="tool",
        title="Send payment",
        description="Moves money.",
        effect_policy="non_idempotent_write",
    )

    async def invoke(self, ctx: ExecutionContext, inputs, cfg) -> ModuleResult:
        return ModuleResult(status="success", outputs={"paid": True})


registry = ModuleRegistry()
registry.register("pay", SendPayment())

inner = WorkflowSpec(
    name="inner",
    entry_node="pay",
    nodes={"pay": WorkflowNode(id="pay", handler="pay")},
    terminal_nodes=["pay"],
)


def outer(retry: dict) -> WorkflowSpec:
    return WorkflowSpec(
        name="outer",
        entry_node="step",
        nodes={"step": WorkflowNode(id="step", handler="", subgraph=inner, retry=retry)},
        terminal_nodes=["step"],
    )


async def main() -> None:
    try:
        WorkflowEngine(outer({"max_attempts": 2}), registry)
    except WorkflowValidationError as exc:
        assert "non-idempotent" in str(exc)
    else:
        raise AssertionError("retrying a payment must be refused")

    result = await WorkflowEngine(outer({}), registry).run({})  # no retry: fine
    assert result.state["paid"] is True


asyncio.run(main())
```

## Pausing for human input with interrupt()

Approval gates answer a yes/no question before a node. `interrupt(value)` is for a
question inside a node: ask for a draft edit, a missing field, a choice. The first call
stops the run with `status="paused"`. `result.interrupt` is
`{"interrupt_id", "node_id", "value"}`. Answer it with
`engine.resume_interrupt(run_id, interrupt_id, answer)`.

The node does not continue from the `interrupt()` line. It runs again from the top and
`interrupt()` now returns the answer. A node may call `interrupt()` several times;
earlier answers are replayed in order. So code before the call runs once per resume.
Put side effects after it, and keep what comes before it cheap and repeatable.

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
log: list[str] = []


@registry.register_handler("review")
def review(state: dict) -> dict:
    log.append("start")  # runs on every attempt, so keep it harmless
    reply = interrupt({"question": "Approve this draft?", "draft": state["draft"]})
    log.append("sent")  # only reached once there is an answer
    return {"reply": reply}


spec = WorkflowSpec(
    name="Review",
    entry_node="review",
    nodes={"review": WorkflowNode(id="review", handler="review")},
    terminal_nodes=["review"],
)


async def main() -> None:
    engine = WorkflowEngine(spec, registry, store=MemoryWorkflowStore())
    paused = await engine.run({"draft": "Refund in full"}, workflow_run_id="t-1")
    assert paused.status == "paused"
    assert paused.interrupt["value"]["question"] == "Approve this draft?"
    assert paused.step_count == 0  # the node has not finished
    assert log == ["start"]

    done = await engine.resume_interrupt(
        "t-1", paused.interrupt["interrupt_id"], "Refund approved, sorry for the delay"
    )
    assert done.status == "completed"
    assert done.state["reply"] == "Refund approved, sorry for the delay"
    assert log == ["start", "start", "sent"]  # the node ran twice in total


asyncio.run(main())
```

Rules:

- `interrupt()` outside a running node raises `WorkflowError`.
- The value passed to `interrupt()` and the answer must be JSON-serializable.
- It needs a store, like any pause. `resume_interrupt` raises `WorkflowError` without
  one.
- The answer is one-shot: answering the same `interrupt_id` twice raises
  `ApprovalAlreadyClaimedError`. A different workspace gets `WrongWorkspaceError`.
- Answering does not approve anything. The node runs again with
  `ExecutionContext.approval_granted` only if it was already running with an approval
  for the same inputs when it called `interrupt()`; a node that was never approval-gated
  stays unapproved, so `context.require_write_approval()` still refuses.
- `interrupt()` raises a `BaseException` subclass so a handler's `except Exception`
  does not swallow it. Do not catch `BaseException` around it.
- A retry policy does not retry an interrupt. Earlier answers are replayed on each
  attempt.
- An interrupt is not allowed inside parallel branches or `map_over` items
  (see [Limits](#limits)).

Through the HTTP service a run paused by `interrupt()` has status `waiting_for_input`
and is answered with `POST /api/v1/runs/{run_id}/input`; see
[Studio and the service](studio-and-service.md#interrupts-history-forks-and-sub-runs).

## History and fork

Each saved checkpoint is kept. `await engine.history(run_id)` returns them, oldest
first, as `WorkflowCheckpoint` objects with `state`, `current_node_id`, `step_count`
and `status`. Both built-in stores support it.

`await engine.fork(run_id, checkpoint_index, state_update=None, new_run_id=None)`
starts a new run from one of those checkpoints. The source run is not changed.
Nodes before the checkpoint do not run again. The new run id defaults to
`<run_id>~fork-<8 hex chars>` and the new checkpoint records `forked_from`.

Things to know:

- A negative index counts from the end. An out-of-range index raises `WorkflowError`.
  A checkpoint that is the end of the run (no current node) cannot be forked.
- `state_update` is merged into the checkpoint's state before the fork runs: a "what
  if" with a different value.
- Approval gates ahead of the fork point ask again. An approval granted in the source
  run does not carry over, so a fork cannot bypass a gate.
- The fork is bound to the spec hash. If the spec was edited since the source run,
  `fork` raises `WorkflowSpecMismatchError`.
- Runs belong to a workspace. `history` and `fork` on a run owned by another workspace
  raise `CrossWorkspaceAccessError` (from `nexura_runtime.service.auth.errors`).

```python
import asyncio

from nexura_runtime.service.auth.errors import CrossWorkspaceAccessError
from nexura_runtime.workflow import (
    MemoryWorkflowStore,
    ModuleRegistry,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
)

registry = ModuleRegistry()


@registry.register_handler("price")
def price(state: dict) -> dict:
    return {"price": state["qty"] * 10}


@registry.register_handler("tax")
def tax(state: dict) -> dict:
    return {"total": state["price"] + state["price"] // 10}


spec = WorkflowSpec(
    name="Quote",
    entry_node="price",
    nodes={
        "price": WorkflowNode(id="price", handler="price", default_transition="tax"),
        "tax": WorkflowNode(id="tax", handler="tax"),
    },
    terminal_nodes=["tax"],
)


async def main() -> None:
    store = MemoryWorkflowStore()
    engine = WorkflowEngine(spec, registry, store=store, workspace_id="acme")
    await engine.run({"qty": 2}, workflow_run_id="q-1")

    history = await engine.history("q-1")
    assert [cp.current_node_id for cp in history] == ["tax", None]
    assert history[0].state["price"] == 20

    # Re-run only the tax step, with a different price.
    fork = await engine.fork("q-1", 0, state_update={"price": 100}, new_run_id="q-1-what-if")
    assert fork.state["total"] == 110
    assert (await engine.history("q-1"))[-1].state["total"] == 22  # source untouched

    other = WorkflowEngine(spec, registry, store=store, workspace_id="rival")
    for call in (other.history("q-1"), other.fork("q-1", 0)):
        try:
            await call
        except CrossWorkspaceAccessError:
            pass
        else:
            raise AssertionError("another workspace must not read or fork this run")


asyncio.run(main())
```

The service exposes the same operations as `GET /api/v1/runs/{run_id}/history` and
`POST /api/v1/runs/{run_id}/fork`, and `GET /api/v1/runs/{run_id}/subruns` lists child
runs; see [Studio and the service](studio-and-service.md#interrupts-history-forks-and-sub-runs).

## Sub-workflows

A node with `subgraph=<WorkflowSpec>` (and an empty `handler`) runs a whole workflow as
one step. Use it to reuse a block of steps, or to keep a long flow readable.

- `input_mapping={"child_key": "parent_key"}` copies parent state into the child's
  initial state. A value that is not a string is passed as a literal. Without a mapping
  the child gets a copy of the whole parent state.
  A string value is a state key; to pass a fixed string instead write `{"$literal": "post_message"}`,
  which yields the value itself (any JSON value). The approval gate sees a literal connector operation,
  so a literal write operation pauses for approval and a literal read operation does not.
- `output_mapping={"child_key": "parent_key"}` copies chosen child state keys back to
  the parent under new names. Without a mapping, the parent receives the keys the child
  wrote or changed.
- The child run is stored under its own id, `<parent run>/<node>#<step>`, for example
  `run-1/kyc#1`. A retry of the sub-workflow node adds `.<attempt>` after the step; an
  item of a `map_over` step adds `[<item index>]`.
- Approval gates and `interrupt()` inside the child pause the parent. The parent's
  `pending_approval.node_id` or `interrupt["node_id"]` is a path such as `kyc/ask`. Use
  the parent's engine and run id to resume: the engine routes the decision to the child
  and continues.
- Nested sub-workflows work to any depth. Editing a sub-workflow changes the parent
  spec's hash, so an old run cannot resume against a different child.

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


@registry.register_handler("fetch_doc")
def fetch_doc(state: dict) -> dict:
    return {"doc": f"doc-for-{state['pan']}"}


@registry.register_handler("ask")
def ask(state: dict) -> dict:
    return {"verdict": interrupt({"question": f"Is {state['doc']} valid?"})}


@registry.register_handler("record")
def record(state: dict) -> dict:
    return {"recorded": state["kyc_result"]}


kyc = WorkflowSpec(
    name="kyc",
    entry_node="fetch",
    nodes={
        "fetch": WorkflowNode(id="fetch", handler="fetch_doc", default_transition="ask"),
        "ask": WorkflowNode(id="ask", handler="ask"),
    },
    terminal_nodes=["ask"],
)

parent = WorkflowSpec(
    name="onboard",
    entry_node="kyc",
    nodes={
        "kyc": WorkflowNode(
            id="kyc",
            handler="",
            subgraph=kyc,
            input_mapping={"pan": "customer_pan"},
            output_mapping={"verdict": "kyc_result"},
            default_transition="record",
        ),
        "record": WorkflowNode(id="record", handler="record"),
    },
    terminal_nodes=["record"],
)


async def main() -> None:
    store = MemoryWorkflowStore()
    engine = WorkflowEngine(parent, registry, store=store)
    paused = await engine.run({"customer_pan": "ABCDE1234F"}, workflow_run_id="run-1")

    assert paused.status == "paused"
    assert paused.interrupt["node_id"] == "kyc/ask"  # a path into the child
    assert await store.get("run-1/kyc#1") is not None  # the child's own run

    done = await engine.resume_interrupt("run-1", paused.interrupt["interrupt_id"], "valid")
    assert done.state["recorded"] == "valid"
    assert "doc" not in done.state  # only the mapped output came back


asyncio.run(main())
```

Because the child shares the parent's store, it needs a store whenever it can pause.
Child engines inherit the parent's workspace, budget, event sink, step limit and
approval policy.

## Parallel steps

`WorkflowNode(parallel=["a", "b"])` runs several handlers at the same time on a copy of
the same state, then merges their updates. List handler names (registered in the
registry); do not also set `handler`. Continue with `default_transition`: a parallel
branch cannot choose a transition.

Branches receive the node's `config` (for example `credential_ref`) and read the whole
state; `input_mapping` does not apply to them.

If two branches write the same key, the step fails rather than keep whichever finished
last. Either have each branch write its own key, or name a reducer for the shared key
with `reducers={"key": "how"}`. Reducers combine values in the order the branches are
declared, not the order they finish:

| Reducer | Effect on the key |
|---|---|
| `append` | adds each value as one item of a list |
| `extend` | adds each list value's items to a list |
| `sum` | adds numbers |
| `merge` | merges dicts |
| `max`, `min` | keeps the larger or smaller value |

A reducer starts from the key's current value in state (or an empty list, dict or 0
where that makes sense). `max_concurrency` (default 10, range 1 to 100) limits how many
run at once. The first branch to fail cancels the rest, and its error is raised.

```python
import asyncio

from nexura_runtime.errors import WorkflowExecutionError
from nexura_runtime.workflow import ModuleRegistry, WorkflowEngine, WorkflowNode, WorkflowSpec

registry = ModuleRegistry()


@registry.register_handler("search_orders")
async def search_orders(state: dict) -> dict:
    await asyncio.sleep(0.02)  # finishes last, still merged first
    return {"evidence": f"order for {state['customer']} delayed", "cost": 1}


@registry.register_handler("search_chats")
async def search_chats(state: dict) -> dict:
    return {"evidence": "asked twice about refund", "cost": 2}


def spec(reducers: dict) -> WorkflowSpec:
    return WorkflowSpec(
        name="gather",
        entry_node="gather",
        nodes={
            "gather": WorkflowNode(
                id="gather",
                handler="",
                parallel=["search_orders", "search_chats"],
                reducers=reducers,
            )
        },
        terminal_nodes=["gather"],
    )


async def main() -> None:
    reducers = {"evidence": "append", "cost": "sum"}
    result = await WorkflowEngine(spec(reducers), registry).run({"customer": "Asha"})
    assert result.state["evidence"] == [
        "order for Asha delayed",
        "asked twice about refund",
    ]  # declaration order
    assert result.state["cost"] == 3

    # Without reducers, both branches wrote "evidence": the step fails.
    try:
        await WorkflowEngine(spec({}), registry).run({"customer": "Asha"})
    except WorkflowExecutionError as exc:
        assert "evidence" in str(exc)
    else:
        raise AssertionError("conflicting writes must fail")


asyncio.run(main())
```

## map_over

`map_over="key"` runs the node's handler (or its sub-workflow) once per item of the list
at `state["key"]`, up to `max_concurrency` at a time. Each run sees a copy of the state
plus `item` and `item_index`; `map_as="row"` renames them to `row` and `row_index`. The
state value must be a list or the step fails. Results are merged with `reducers` the
same way as parallel branches, in item order.

```python
import asyncio

from nexura_runtime.workflow import ModuleRegistry, WorkflowEngine, WorkflowNode, WorkflowSpec

registry = ModuleRegistry()


@registry.register_handler("label")
async def label(state: dict) -> dict:
    return {"labels": f"{state['row_index']}:{state['row'].upper()}", "count": 1}


spec = WorkflowSpec(
    name="label-all",
    entry_node="each",
    nodes={
        "each": WorkflowNode(
            id="each",
            handler="label",
            map_over="rows",
            map_as="row",
            reducers={"labels": "append", "count": "sum"},
            max_concurrency=2,
        )
    },
    terminal_nodes=["each"],
)


async def main() -> None:
    result = await WorkflowEngine(spec, registry).run({"rows": ["a", "b", "c"]})
    assert result.state["labels"] == ["0:A", "1:B", "2:C"]
    assert result.state["count"] == 3


asyncio.run(main())
```

Mapped steps, like parallel steps, continue through `default_transition` and cannot
choose a branch.

### Limits

Parallel branches and mapped items cannot be resumed one at a time, so they cannot
pause:

- `interrupt()` inside a parallel branch or a `map_over` item raises
  `WorkflowValidationError`. Ask before or after the step.
- A sub-workflow run per item (`map_over` with `subgraph`) may not contain an approval
  gate; the spec fails validation. The same pause rule applies to sub-workflows nested
  inside it. Approve before or after the mapped step.
- `parallel` and `map_over` cannot be combined, and `parallel` cannot be combined with
  `subgraph`.
- `reducers` only apply to parallel or `map_over` steps. Allowed names are
  `append`, `extend`, `sum`, `merge`, `max`, `min`.

```python
import asyncio

from nexura_runtime.errors import WorkflowExecutionError
from nexura_runtime.workflow import (
    ModuleRegistry,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
    interrupt,
)

registry = ModuleRegistry()


@registry.register_handler("ask")
def ask(state: dict) -> dict:
    return {"answer": interrupt("question?")}


spec = WorkflowSpec(
    name="bad",
    entry_node="each",
    nodes={"each": WorkflowNode(id="each", handler="ask", map_over="rows")},
    terminal_nodes=["each"],
)


async def main() -> None:
    try:
        await WorkflowEngine(spec, registry).run({"rows": [1, 2]})
    except WorkflowExecutionError as exc:
        assert "interrupt()" in str(exc)
    else:
        raise AssertionError("interrupt inside map_over must be refused")


asyncio.run(main())
```

## Limits and timeouts

Two engine arguments bound a run:

- `max_steps` (default 50): the number of node executions. When a run reaches it, the
  checkpoint is saved as `failed` and `WorkflowMaxStepsError` is raised. This is what
  stops an accidental cycle. The count carries across resumes.
- `timeout` (seconds, default none): a deadline for the whole run, counted from when the
  engine starts executing (and again from each resume). Handlers still running when it
  passes are cancelled, the checkpoint is saved as `failed`, and
  `WorkflowTimeoutError` is raised.

Both are validated when the engine is built: `max_steps` must be an integer of at least
1 and `timeout` a positive number, or `WorkflowValidationError` is raised.

```python
import asyncio

from nexura_runtime.errors import WorkflowMaxStepsError, WorkflowTimeoutError
from nexura_runtime.workflow import (
    MemoryWorkflowStore,
    ModuleRegistry,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
)

registry = ModuleRegistry()


@registry.register_handler("tick")
def tick(state: dict) -> dict:
    return {"n": state.get("n", 0) + 1}


@registry.register_handler("slow")
async def slow(state: dict) -> dict:
    await asyncio.sleep(5)
    return {}


loop = WorkflowSpec(
    name="loop",
    entry_node="tick",
    nodes={"tick": WorkflowNode(id="tick", handler="tick", default_transition="tick")},
)
sleepy = WorkflowSpec(
    name="sleepy",
    entry_node="slow",
    nodes={"slow": WorkflowNode(id="slow", handler="slow")},
    terminal_nodes=["slow"],
)


async def main() -> None:
    store = MemoryWorkflowStore()
    try:
        await WorkflowEngine(loop, registry, store=store, max_steps=5).run(
            {}, workflow_run_id="cycle"
        )
    except WorkflowMaxStepsError:
        pass
    else:
        raise AssertionError("the cycle must be stopped")
    assert (await store.get("cycle")).status == "failed"
    assert (await store.get("cycle")).state["n"] == 5

    try:
        await WorkflowEngine(sleepy, registry, timeout=0.05).run({})
    except WorkflowTimeoutError:
        pass
    else:
        raise AssertionError("the deadline must be enforced")


asyncio.run(main())
```

## JSON specs

A spec is plain data. `spec.to_json(indent=2)` and `WorkflowSpec.from_json(text)` round
trip it, including sub-workflows, parallel and map settings, retry policies and
reducers. `to_dict()` and `from_dict()` do the same with Python dicts. `from_dict` also
accepts `nodes` as a list of node dicts, and a node's `module_id` can stand in for
`handler`. Bad input raises `WorkflowValidationError`; a `spec_version` the library does
not support raises `UnsupportedWorkflowVersionError`.

`spec.canonical_hash()` is a SHA-256 of the executable parts of the spec. Layout hints
(`presentation`, and editor keys such as `position` inside `metadata`) are left out, so
moving a node on a canvas does not change the hash. Everything else does: a changed
handler, transition, retry policy or sub-workflow produces a new hash. Checkpoints and
approvals record the hash, which is why `resume` and `fork` refuse a changed spec.

```python
from nexura_runtime.workflow import WorkflowNode, WorkflowSpec

child = WorkflowSpec(
    name="child",
    entry_node="a",
    nodes={"a": WorkflowNode(id="a", handler="noop")},
    terminal_nodes=["a"],
)
spec = WorkflowSpec(
    name="parent",
    entry_node="each",
    nodes={
        "each": WorkflowNode(
            id="each",
            handler="",
            subgraph=child,
            map_over="rows",
            map_as="row",
            reducers={"out": "append"},
            max_concurrency=4,
            retry={"max_attempts": 2},
            default_transition="end",
        ),
        "end": WorkflowNode(id="end", handler="noop", presentation={"x": 10, "y": 20}),
    },
    terminal_nodes=["end"],
)

text = spec.to_json()
again = WorkflowSpec.from_json(text)
assert again.canonical_hash() == spec.canonical_hash()
assert again.nodes["each"].map_as == "row"
assert again.nodes["each"].subgraph.name == "child"
assert again.nodes["each"].reducers == {"out": "append"}

# Moving a node on a canvas does not change the executable identity...
again.nodes["end"].presentation = {"x": 99, "y": 99}
assert again.canonical_hash() == spec.canonical_hash()

# ...but editing a handler does.
again.nodes["end"].handler = "other"
assert again.canonical_hash() != spec.canonical_hash()
```

A JSON spec names handlers, it does not contain code. The registry you build the engine
with decides what each name does, which is why a spec from an untrusted source is
limited to the handlers you registered.

## Export to Python and back

A workflow is never locked inside Studio. `workflow_to_python` writes plain Python that
rebuilds the same `WorkflowSpec` with the public `WorkflowSpec` / `WorkflowNode` constructors.
Handlers stay names (strings) that your registry resolves. Approval gates, retries and
composite steps (sub-workflows, `parallel`, `map_over`) get a comment above the node, and every
field is kept: the module's `spec.to_dict()` equals the original's.

The generated file ends with an `if __name__ == "__main__":` block that runs the workflow with
your registry. Pass `registry_import="package.module:attribute"` to name it; without it the
built-in registry is used.

```python
from nexura_runtime.codegen import python_to_workflow, workflow_to_python
from nexura_runtime.workflow import WorkflowNode, WorkflowSpec

spec = WorkflowSpec(
    name="refund",
    entry_node="lookup",
    nodes={
        "lookup": WorkflowNode(
            id="lookup", handler="synthetic_order_lookup", default_transition="pay"
        ),
        "pay": WorkflowNode(
            id="pay", handler="synthetic_router", requires_approval=True, retry={"max_attempts": 2}
        ),
    },
    terminal_nodes=["pay"],
)

source = workflow_to_python(spec, registry_import="my_app.handlers:registry")
assert "Approval gate" in source and 'handler="synthetic_order_lookup"' in source
assert python_to_workflow(source).to_dict() == spec.to_dict()
```

`python_to_workflow(source)` reads such a module back **without executing it**. It parses the
text with `ast` and accepts only the shape the generator writes: literals, the
`WorkflowSpec(...)` / `WorkflowNode(...)` calls with keyword arguments, handler names as
strings, the `asyncio` / `json` / `nexura_runtime.workflow` imports, and the exact
`__main__` block. Anything else (other calls, attribute access, other imports, comprehensions,
lambdas, f-strings, decorators, function or class definitions) raises `CodeImportError` with the
line number, so it is safe for code from untrusted users. Hand-edits that stay inside that
shape (change a transition, add a node, tune a retry) import fine; logic belongs in handlers.

```python
from nexura_runtime.codegen import CodeImportError, python_to_workflow

try:
    python_to_workflow("import os\nos.system('echo never runs')\n")
except CodeImportError as exc:
    assert exc.line == 1
    assert str(exc).startswith("line 1:")
```

Where it is available:

- Service: `GET /api/v1/workflows/{id}/export/python` returns `{"source", "filename",
  "spec_hash", "workflow_id"}` (secrets scrubbed like the bundle export; editors and admins only).
  `POST /api/v1/workflows/import/python` takes `{"source": "...", "name"?, "workflow_id"?}` (or
  the raw source as `text/plain`), never executes it, caps it at 256 KiB, and then applies the
  same validation, approval policy and workspace rules as the JSON import. A rejected file
  answers 400 `INVALID_PYTHON_SOURCE` with `errors: [{line, column, message}]`.
- Studio: Import / Export JSON has "Export as Python" and "Import from Python" tabs.
- CLI: `nexura-runtime workflow export spec.json --format python -o workflow.py [--registry pkg.mod:attr]`
  and `nexura-runtime workflow import workflow.py [-o spec.json]` (prints the JSON spec; never runs
  the file).

Agents need no export step: `nexura_agents.Agent(...)` is already Python, and the platform has no
separate serializable agent spec.

## Streaming progress

`engine.stream(initial_state, workflow_run_id=None)` runs a workflow and yields `RunEvent` objects as it goes; `engine.resume_stream(workflow_run_id, approval_id, ...)` does the same for `resume`. `run` is unchanged and reports nothing.

- `WORKFLOW_STARTED`, then for each node `NODE_STARTED` (`node_id`, `step`) and `NODE_COMPLETED` (adds `attempts`, `branch` and `updated_keys`).
- `NODE_PAUSED` when a node waits for an approval or an `interrupt()` (`reason` is `"approval"` or `"interrupt"`), followed by `WORKFLOW_PAUSED`.
- `NODE_FAILED` followed by `WORKFLOW_FAILED` when a node fails for good; the error is then raised from the stream, as `run` raises it.
- The last event is `WORKFLOW_COMPLETED` or `WORKFLOW_PAUSED`; its `data["result"]` is the `WorkflowRunResult` that `run` returns. These end events and `WORKFLOW_FAILED` are the only ones with `event.is_terminal` true.

Every event has `data["workflow"]` (the name of the spec that ran the node, so nodes of a sub-workflow carry the sub-workflow's name) and `data["workflow_run_id"]` on workflow-level events. A parallel or `map_over` step is one node; its branches do not report separately. Closing the stream early cancels the run. `stream` picks the run id before starting (so `WORKFLOW_STARTED` can carry it), which costs one extra store lookup compared with `run`.

```python
import asyncio

from nexura_runtime.workflow import ModuleRegistry, WorkflowEngine, WorkflowNode, WorkflowSpec

registry = ModuleRegistry()
registry.register("one", lambda state: {"a": 1})
spec = WorkflowSpec(
    name="demo",
    entry_node="n1",
    nodes={"n1": WorkflowNode(id="n1", handler="one")},
    terminal_nodes=["n1"],
)


async def main() -> None:
    events = [e async for e in WorkflowEngine(spec, registry).stream({})]
    assert [e.type.value for e in events] == [
        "workflow_started",
        "node_started",
        "node_completed",
        "workflow_completed",
    ]
    assert events[-1].data["result"].state == {"a": 1}


asyncio.run(main())
```

## Diagrams

`spec.to_mermaid()` returns a Mermaid flowchart (top to bottom) of a `WorkflowSpec`:

- a `start` circle points at the entry step, and branches are labelled with their
  transition names;
- `default` transitions are dotted, and targets outside the spec are circles;
- approval steps are marked `(approval)`, and terminal steps have a thicker border;
- sub-workflows are nested boxes, parallel steps list their branches, and `map_over`
  steps say `for each <list>`.

```python
from nexura_runtime.workflow import WorkflowNode, WorkflowSpec

spec = WorkflowSpec(
    name="refunds",
    entry_node="check",
    nodes={
        "check": WorkflowNode(id="check", handler="h", transitions={"ok": "pay"}),
        "pay": WorkflowNode(id="pay", handler="h", requires_approval=True),
    },
    terminal_nodes=["pay"],
)
assert '"pay (approval)"' in spec.to_mermaid()
```

## See also

- [Human approval](approvals.md): approval gates for agent tools.
- [Testing your agents](testing.md): offline testing patterns.
- [Documentation index](README.md): budgets, the REST service and troubleshooting.

## Tutorial: an approval-gated workflow

Handlers are trusted Python functions you register; workflow JSON only names them.
A node with `requires_approval=True` pauses *before* its handler runs.

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


@registry.register_handler("lookup_order")
def lookup_order(state: dict) -> dict:
    return {"customer": "Amit Verma", "amount": 4500}


@registry.register_handler("process_refund")
def process_refund(state: dict) -> dict:
    return {"refund_id": "REF-9901", "status": "refunded"}


spec = WorkflowSpec(
    name="RefundWorkflow",
    entry_node="lookup",
    nodes={
        "lookup": WorkflowNode(id="lookup", handler="lookup_order", default_transition="gate"),
        "gate": WorkflowNode(id="gate", handler="process_refund", requires_approval=True),
    },
    terminal_nodes=["gate"],
)


async def main() -> None:
    db = Path(tempfile.mkdtemp()) / "workflows.sqlite3"

    store = SQLiteWorkflowStore(db)
    paused = await WorkflowEngine(spec, registry, store=store).run({"order_id": "ORD-1"})
    print(paused.status)  # paused: the refund has not run
    await store.close()

    # A new engine on the same database (e.g. after a restart) resumes the run.
    store = SQLiteWorkflowStore(db)
    done = await WorkflowEngine(spec, registry, store=store).resume(
        paused.workflow_run_id,
        approval_id=paused.pending_approval.approval_id,
        decision="approve",
    )
    print(done.status, done.state["refund_id"])  # completed REF-9901
    await store.close()


asyncio.run(main())
```

Next steps, all keyless:

- `examples/oss/o07.py` runs the built-in order-support workflow through the SDK,
  the REST API and an exported standalone bundle and checks they agree.
- `examples/oss/o01.py` to `o06.py` cover budgets, durable recovery, export,
  usage diagnostics, provider cache planning and context selection.
- `examples/approval_workflow.py`, `examples/persistent_chat.py`,
  `examples/order_support.py` cover the core SDK.
- `examples/real_provider.py` calls a real model (the separate `examples/live/` checks do too); it reads
  your own `OPENAI_API_KEY` and is never run by tests or CI.

## Retries, human input mid-step, and time travel

Three workflow tools for real-world flows, all durable in `SQLiteWorkflowStore`:

- **Per-node retries.** `WorkflowNode(retry={"max_attempts": 3, "initial_delay": 0.5,
  "retry_on": ["ConnectionError"]})` retries a flaky step with exponential backoff and
  never sleeps past the workflow deadline. Validation refuses retries on modules that
  declare `effect_policy="non_idempotent_write"`, so a payment cannot be sent twice.
- **`interrupt(value)` inside a handler** pauses the run and hands `value` (any JSON) to
  a person. `engine.resume_interrupt(run_id, interrupt_id, answer)` re-runs that node,
  and the same `interrupt()` call returns `answer`. Code before the interrupt runs again
  on resume, so put side effects after it. Each answer can be claimed only once.
- **History, replay and fork.** `engine.history(run_id)` lists every checkpoint.
  `engine.fork(run_id, index, state_update=...)` continues from any checkpoint as a new
  run, optionally with edited state; the original run is never modified. Forking through
  an approval gate asks for approval again. Only the run's own workspace can read its
  history or fork it.

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
attempts = []


@registry.register_handler("fetch_order")
def fetch_order(state: dict) -> dict:
    attempts.append(1)
    if len(attempts) < 2:
        raise ConnectionError("order service reset the connection")
    return {"draft": f"Refund {state['order_id']} in full"}


@registry.register_handler("review")
def review(state: dict) -> dict:
    reply = interrupt({"question": "Edit or approve this reply", "draft": state["draft"]})
    return {"reply": reply}


spec = WorkflowSpec(
    name="SupportReply",
    entry_node="fetch",
    nodes={
        "fetch": WorkflowNode(
            id="fetch",
            handler="fetch_order",
            default_transition="review",
            retry={"max_attempts": 3, "initial_delay": 0.01},
        ),
        "review": WorkflowNode(id="review", handler="review"),
    },
    terminal_nodes=["review"],
)


async def main() -> None:
    engine = WorkflowEngine(spec, registry, store=MemoryWorkflowStore())
    paused = await engine.run({"order_id": "ORD-7"}, workflow_run_id="ticket-7")
    assert paused.status == "paused" and len(attempts) == 2  # one retry, then the question
    print(paused.interrupt["value"]["question"])

    done = await engine.resume_interrupt(
        "ticket-7", paused.interrupt["interrupt_id"], "Refunded ORD-7. Sorry for the wait!"
    )
    print(done.state["reply"])

    # Time travel: replay the review step from the checkpoint after fetch, with new state.
    steps = await engine.history("ticket-7")
    what_if = await engine.fork("ticket-7", 0, state_update={"draft": "Offer a voucher"})
    print(len(steps) > 1, what_if.interrupt["value"]["draft"])


asyncio.run(main())
```

## Sub-workflows and parallel steps

Build big flows from small ones, and do independent work at the same time:

- **`subgraph=`** runs a whole workflow as one step. `input_mapping={"child_key":
  "parent_key"}` chooses what goes in and `output_mapping={"child_key": "parent_key"}`
  what comes back; without an output mapping, the keys the sub-workflow added or
  changed come back. An approval or `interrupt()` inside a sub-workflow pauses the
  parent, with a path such as `kyc/ask` as the node id. You answer it on the parent run.
  Each sub-workflow is a run with its own history, with an id such as `<run>/kyc#1`.
- **`parallel=[...]`** runs several handlers at once on the same state.
- **`map_over="tickets"`** runs the handler (or sub-workflow) once per item of a state
  list, at most `max_concurrency` at a time. Each item sees `item` and `item_index`; use
  `map_as=` to rename `item`.
- **`reducers=`** combine results in declaration order, whatever the finishing order:
  `append`, `extend`, `sum`, `merge`, `max`, `min`. If two branches write the same key
  without a reducer, the step fails instead of silently keeping one of the values. The
  first failing branch cancels the rest. `interrupt()` is not allowed inside parallel
  branches or mapped items.

```python
import asyncio

from nexura_runtime.workflow import ModuleRegistry, WorkflowEngine, WorkflowNode, WorkflowSpec

registry = ModuleRegistry()


@registry.register_handler("search_orders")
async def search_orders(state: dict) -> dict:
    return {"evidence": [f"order {state['customer']} delayed"], "cost": 1}


@registry.register_handler("search_chats")
async def search_chats(state: dict) -> dict:
    return {"evidence": ["asked twice for a refund"], "cost": 2}


@registry.register_handler("label")
async def label(state: dict) -> dict:
    return {"labels": f"{state['item_index']}:{state['item'].upper()}"}


@registry.register_handler("decide")
def decide(state: dict) -> dict:
    return {"decision": "refund" if len(state["evidence"]) > 1 else "reply"}


triage = WorkflowSpec(
    name="Triage",
    entry_node="gather",
    nodes={
        "gather": WorkflowNode(
            id="gather",
            handler="",
            parallel=["search_orders", "search_chats"],
            reducers={"evidence": "extend", "cost": "sum"},
            default_transition="tag",
        ),
        "tag": WorkflowNode(
            id="tag",
            handler="label",
            map_over="evidence",
            reducers={"labels": "append"},
            default_transition="decide",
        ),
        "decide": WorkflowNode(id="decide", handler="decide"),
    },
    terminal_nodes=["decide"],
)

support = WorkflowSpec(
    name="Support",
    entry_node="triage",
    nodes={
        "triage": WorkflowNode(
            id="triage",
            handler="",
            subgraph=triage,
            input_mapping={"customer": "customer_id"},
            output_mapping={"decision": "next_action", "labels": "labels", "cost": "cost"},
        )
    },
    terminal_nodes=["triage"],
)


async def main() -> None:
    result = await WorkflowEngine(support, registry).run({"customer_id": "C-42"})
    print(result.state["next_action"], result.state["cost"])
    print(result.state["labels"])


asyncio.run(main())
```
