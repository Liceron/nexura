# Durable agent runs

A run interrupted by a crash, deploy or out-of-memory kill can continue from its last
completed step instead of starting again.

```python
import asyncio
from nexura_agents import Agent, FakeModel, Runner, SQLiteCheckpointStore, ToolCall, tool


@tool(idempotent=True)
def lookup(order_id: str) -> str:
    """Read an order (safe to repeat)."""
    return f"order {order_id}: shipped"


@tool
def refund(order_id: str) -> str:
    """Refund an order (must not run twice by accident)."""
    return f"refunded {order_id}"


def build_agent(model):
    return Agent(
        name="Support", instructions="Help with orders.", model=model, tools=[lookup, refund]
    )


async def main():
    store = SQLiteCheckpointStore("runs.db")  # file is created with mode 0600
    runner = Runner(checkpoint_store=store)
    model = FakeModel(
        responses=[[ToolCall(id="c1", name="lookup", arguments={"order_id": "A1"})], "It shipped."]
    )
    result = await runner.run_async(build_agent(model), "Where is A1?")
    print(result.final_output, await store.list_incomplete())  # finished runs leave nothing

    # After a crash, in a new process:
    #   for run_id in await store.list_incomplete():
    #       result = await runner.recover_async(build_agent(real_model), run_id)


asyncio.run(main())
```

`Runner(checkpoint_store=...)` turns checkpointing on. It is off by default and costs
nothing when off: the runner does not call any store.

The memory, SQLite and Postgres stores also keep an **execution journal** next to the
checkpoints (the [durable execution contract](durable-execution.md)): the run holds a
fenced lease from its first write, and each tool call that is not `pure` records an
intent just before it starts. That is how recovery knows which calls never started,
which may have run, and when another process still owns the run.

## What is checkpointed

The runner writes a checkpoint (the same JSON as `RunInterruption`, wrapped in an envelope):

- at the top of every turn, before the model is called (`phase="model_next"`);
- before tools run, and after each tool result (`phase="tools_pending"`, with the calls that
  still have no result);
- when the run pauses for approval (`phase="paused"`).

In the journal, the runner also writes:

- the run row, with its lease, before the first checkpoint;
- just before each non-pure tool call starts: an intent (step key
  `turn:<n>/call:<tool_call_id>`, the tool's effect class, a digest of the arguments and
  the idempotency key) and a `pending` effect record;
- after the call's result is in the checkpoint: its outcome, and the effect `confirmed`;
- `paused` (with `pause_reason`), `completed` or `failed` when the run stops.

A completed run, or one that fails with an exception, deletes its checkpoint, so
`await store.list_incomplete()` lists only runs that stopped abnormally (a crash or a
cancelled task) or wait for approval. A run that failed with an exception (a provider
error, a limit, a guardrail) is finished, not recoverable. Streaming runs checkpoint the same
way; a crash mid-stream loses only the partial text of that turn.

## Recovering

```python
import asyncio
from nexura_agents import Agent, FakeModel, MemoryCheckpointStore, Runner, ToolCall, tool


class Crash(BaseException):  # stands in for a killed process
    pass


@tool
def charge(order_id: str) -> str:
    """Charge a card (not idempotent)."""
    raise Crash


def build(model):
    return Agent(name="Billing", instructions="Bill orders.", model=model, tools=[charge])


async def main():
    store = MemoryCheckpointStore()
    runner = Runner(checkpoint_store=store)
    first = FakeModel(responses=[[ToolCall(id="c1", name="charge", arguments={"order_id": "A1"})]])
    try:
        await runner.run_async(build(first), "Bill A1")
    except Crash:
        pass
    (run_id,) = await store.list_incomplete()
    result = await runner.recover_async(
        build(FakeModel(responses=["done"])), run_id, on_uncertain="pause"
    )
    print(result.status, result.interruption.reason)  # paused outcome_unknown_after_crash


asyncio.run(main())
```

Pass the same agent definition (same name, tools and handoffs) and a model. Recovery raises
`RunRecoveryError` for an unknown run id, a corrupt or unsupported checkpoint, a checkpoint
of a different agent, or an active agent that is no longer reachable through handoffs.
`runner.recover(...)` is the synchronous form and `recover_stream_async` yields events.

- **`model_next`**: the model is called again with the stored transcript. At most one model
  call is repeated.
- **`paused`**: the stored `RunInterruption` is returned as a paused result; approve or reject
  it with `resume_async` as usual.
- **`tools_pending`**: calls that already have a result are not run again. With a
  journal, a call without an intent never started and runs normally. A call with an
  intent and no result may have run: what happens depends on the effect class recorded
  in its intent (below); re-declaring the tool after the crash can only make that
  stricter. Without a journal (a custom store) every call without a result is
  *uncertain*, and so are the unanswered calls of a checkpoint written before the run
  had a journal (a file from before this release) when it is first recovered.

Output retries already spent (schema failures and output validators) also come from
the checkpoint, so recovery does not grant a fresh retry budget. Usage, request and turn counts come from the checkpoint, so `UsageLimits` and budgets see
the whole run. The overall `timeout` restarts on recovery. Handoffs restore the active agent.

## Tool semantics: effect classes

Nexura cannot know whether a tool that was running at the time of the crash finished its
side effect. You declare per tool what recovery may do (`@tool(effect=...)`):

- `effect="pure"`: no side effect (a read). Recovery re-runs it. Pure calls write no
  intent.
- `effect="idempotent"` (alias: `idempotent=True`): safe to repeat. Recovery re-runs it
  with the **same** `ctx.idempotency_key` (**at-least-once**). Forward the key to the
  remote system (an `Idempotency-Key` header, a request id column) so it de-duplicates.
- `effect="non_idempotent"`, the default for any tool that declares nothing: recovery
  never re-runs it silently (**at-most-once**). What happens instead depends on
  `on_uncertain`:
  - `"pause"` (default): returns a paused result whose `interruption.pause_reason` is
    `"effect_unknown"` (its `reason` is `"outcome_unknown_after_crash"`) and whose
    `pending` lists the uncertain calls. Resolve them (next section), approve
    (digest-bound, as in [approvals](approvals.md)) to send them again, or reject, which
    tells the model "the outcome of this action is unknown; it was not retried".
  - `"fail"`: raises `RunRecoveryError`.
  - `"tell_model"`: gives the model that "unknown outcome" result and continues.
- `effect="reconcilable", reconcile=fn`: recovery asks `fn(ctx, intent)` whether the
  action happened. Return `Applied(result)` (the result becomes the tool result; nothing
  runs again), `NotApplied()` (it runs again with the same key; a tool that requires
  approval first pauses for a fresh approval) or `Unknown()` (handled like
  `non_idempotent`). An exception in `fn` counts as `Unknown()`. `Applied`,
  `NotApplied` and `Unknown` are in `nexura_agents.contracts.execution`.

Every tool call gets `ctx.idempotency_key` (`nx-` plus 32 hex characters, derived from
the workspace, run id and step key, so a crash re-run gets the same key) and, in a run
with a journal, `ctx.fencing_token` (the run lease's token, which only goes up; pass it
to systems that can refuse a stale caller). Retries and `cache=True` need
`effect="pure"` or `"idempotent"`.

## Resolving an unknown outcome

A person who checked the remote system resolves each paused call with
`ResolvedOutcome`, passed to `resume_async(..., outcomes={call_id: ...})`:

- `ResolvedOutcome(result)`: it happened. `result` becomes the tool result and the call is
  not run again.
- `ResolvedOutcome.not_applied()`: it did not happen. The call is sent again as a new
  attempt with a new idempotency key. A tool with `requires_approval` pauses first for a
  **fresh approval** (`interruption.reason == "effect_not_applied"`), because the
  original approval was used by the first attempt.
- `ResolvedOutcome.failed(reason)`: it failed. The run fails with `ToolExecutionError`.

The resolution and who made it are recorded on the call's effect record: pass
`resume_async(..., actor="ops@example.com")` (default `"sdk:resume"`). Approving an
`effect_unknown` call instead sends it again now and records the first attempt as
`not_applied` by the same actor.
If the effect is resolved directly in the journal instead (for example through the
effects API, see [durable execution](durable-execution.md#resolving-an-unknown-effect-s4)),
`recover_async` continues the run with that resolution and the old pause can no longer
be resumed.

```python
import asyncio
from nexura_agents import (
    Agent, FakeModel, MemoryCheckpointStore, ResolvedOutcome, RunContext, Runner, ToolCall, tool,
)
from nexura_agents.contracts.execution import Applied


class Crash(BaseException):  # stands in for a killed process
    pass


calls = {"charge": 0, "refund": 0}


@tool
def charge(ctx: RunContext[None], order_id: str) -> str:
    """Charge a card (not idempotent: a crash while it runs pauses the run)."""
    calls["charge"] += 1
    raise Crash


def already_refunded(ctx: RunContext[None], intent) -> Applied:
    # Ask the payment system about intent.idempotency_key (here: a local fake answer).
    return Applied(f"refund {intent.idempotency_key} found")


@tool(effect="reconcilable", reconcile=already_refunded)
def refund(ctx: RunContext[None], order_id: str) -> str:
    """Refund an order; the payment system can tell whether it already happened."""
    calls["refund"] += 1
    raise Crash


def build(model, tool_):
    return Agent(name="Billing", instructions="Bill orders.", model=model, tools=[tool_])


async def main():
    store = MemoryCheckpointStore()
    runner = Runner(checkpoint_store=store)
    for tool_, name in ((charge, "charge"), (refund, "refund")):
        call = ToolCall(id="c1", name=name, arguments={"order_id": "A1"})
        try:
            await runner.run_async(build(FakeModel(responses=[[call]]), tool_), "Bill A1")
        except Crash:
            pass
    charge_run, refund_run = await store.list_incomplete()

    paused = await runner.recover_async(build(FakeModel(responses=["done"]), charge), charge_run)
    assert paused.interruption.pause_reason == "effect_unknown"
    # A person checked the payment system: the charge went through.
    done = await runner.resume_async(
        build(FakeModel(responses=["done"]), charge),
        paused.interruption,
        outcomes={"c1": ResolvedOutcome("charged A1")},
    )
    assert done.status == "completed" and calls["charge"] == 1

    # The reconcilable refund is settled by its reconcile function: no pause, no re-run.
    done = await runner.recover_async(build(FakeModel(responses=["done"]), refund), refund_run)
    assert done.status == "completed" and calls["refund"] == 1
    print(done.messages[-2].content)  # refund nx-... found


asyncio.run(main())
```

## Concurrency, completion and storage errors

- **The run holds a lease from its first write.** With the memory, SQLite and Postgres
  stores, a new run (and `resume`) takes the run's lease before its first checkpoint: a
  journal lease with a fencing token, plus the store's claim, both under one owner.
  A heartbeat renews them every `lease_ttl / 3` (`Runner(lease_ttl=30.0)`, in seconds).
  Pausing, finishing or failing gives them back.
- **One owner per run.** `recover` takes the same lease. While another process holds it,
  `recover` raises `RunRecoveryError` ("already being recovered"); so a run that is
  only slow is not recovered twice. When the owner is a process **on the same host that
  has exited** (killed, out of memory), the lease is taken over at once: the owner id
  names the process, and the store's run lease is given back on its behalf. An owner on
  another host, or on a host without a boot id (macOS, BSD, Windows), is taken over once
  its lease expires (at most `lease_ttl` after it stopped renewing). SQLite is for one
  host; use Postgres across hosts.
- **A stale owner stops.** When another process took the run over (for example after
  this one was frozen past its lease), the heartbeat notices and cancels the in-flight
  model call or `async` tool, and the run ends with `LeaseLostError`; any later journal
  write is refused too, and a re-run of a call is checked against the store before it is
  sent. A synchronous tool already running in a thread cannot be stopped; its call keeps
  its intent, so the new owner treats it as in doubt.
- Custom stores keep the older behavior: `recover` takes `claim(run_id, owner,
  lease_seconds)` / `release` when the store has them (default lease one hour), and the
  original run holds no lease. A second concurrent recoverer raises `RunRecoveryError`.
  Custom stores without `claim` get no protection and a `DeprecationWarning`; they will
  not be accepted in 0.6.
- **Fenced writes.** The memory and SQLite stores check the lease on every checkpoint save
  (in the same transaction for SQLite), and each save by the lease holder renews the lease:
  - A recoverer that stalled past its lease, and was taken over by another, gets
    `CheckpointLeaseLostError` (a `RunRecoveryError`) on its next save and stops. It cannot
    overwrite the newer checkpoint.
  - A run without a lease (the original process, or `resume`) is refused while a
    recoverer holds a live lease. So a run that was only slow, not dead, stops instead
    of racing its recoverer.
  - A refused save leaves the current checkpoint in place.
  - The runner's cleanup delete is fenced the same way: when a stale run fails, it
    cannot delete the recoverer's checkpoint or lease. A plain `store.delete(run_id)`
    from your own code is an operator purge and always deletes.

  Limits: fencing protects checkpoints and the journal, not your tools or the session
  store. A stale writer can still finish the step it is in before its next write is
  refused. Sessions use their own revision check. With a custom store, a writer without
  a lease is not fenced once the recoverer's lease has expired or been released. Custom
  stores opt in by accepting `owner=` in `save`, and `owner=`/`fenced=` in `delete` (see
  `check_fence` in `nexura_agents.durable`), and get the journal by adding
  `open_execution_store()`.
- **Completed phase.** After the session is saved the runner writes a `completed`
  checkpoint. If the process dies before cleanup, `recover` replays the final answer
  without calling the model or saving the session again. `on_run_end` runs at least once.
- **Storage errors keep the last good checkpoint.** A failing checkpoint save never deletes
  the previous checkpoint. A failing delete after completion is ignored; the stale
  `completed` checkpoint is finished by `recover`.
- **Handoff callbacks run at most once.** The handoff is checkpointed before `on_handoff`
  runs, so a crash during or after the callback does not repeat it (a crash before the
  checkpoint skips nothing; the callback has not run yet).

## Stores

- `MemoryCheckpointStore()`: tests and demos; does not survive the process. Its journal
  keeps finished runs' rows in memory for the life of the store.
- `SQLiteCheckpointStore(path)`: stdlib `sqlite3`, atomic upserts, a schema version, and a
  0600 file. The journal (`SQLiteExecutionStore`) lives in the same file, which it
  switches to WAL mode. Checkpoints, claims and approvals are scoped by `workspace_id=`
  (an older file is migrated on open; its rows belong to `"default"`). One host only.
- `nexura_postgres.PostgresCheckpointStore(dsn)` (or `pool=`): the same store on
  Postgres, with the journal (`PostgresExecutionStore`) in the same database. Leases use
  the database clock, so several hosts can share it. Tables are created on first use
  (`await store.init_db()` does it up front); rows are scoped by `workspace_id=`. The
  asyncpg pool belongs to one event loop; `await store.close()` closes a pool the store
  created.
- Your own: implement `RunCheckpointStore` (`save`, `load`, `delete`, `list_incomplete`).

All three built-in stores take `workspace_id=` (default `"default"`), which scopes the
journal (and, on Postgres, the checkpoints). `await store.open_execution_store()`
returns the journal, for example to list a run's effects.

## Security and limitations

- Checkpoints contain the **full conversation transcript** (and tool arguments and results).
  Treat the store like session data: restrict access, encrypt the disk, delete old rows.
  The memory block of a run is stored as in an approval interruption; recalled memories are
  never written to sessions.
- A checkpoint that fails to save stops the run with the store's error (the durability
  promise is not silently dropped).
- Teams: a team member's run is checkpointed if the team's runner has a store, but
  recovering a team as a unit is not supported.
- Runs cancelled with `CancelledError` keep their checkpoint and can be recovered; the
  cancelled task gives its lease back.
- Recovering or resuming with a changed agent definition (`Agent.definition_digest()`:
  tools, argument schemas, effect classes, instructions, handoffs, output type) emits a
  `RuntimeWarning` in this release; a later release refuses it.
- Budget reservations and approval records are not linked to tool intents yet.
- Each checkpoint rewrites the whole transcript; very long runs write proportionally more.
