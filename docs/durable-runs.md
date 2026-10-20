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

## What is checkpointed

The runner writes a checkpoint (the same JSON as `RunInterruption`, wrapped in an envelope):

- at the top of every turn, before the model is called (`phase="model_next"`);
- before tools run, and after each tool result (`phase="tools_pending"`, with the calls that
  still have no result);
- when the run pauses for approval (`phase="paused"`).

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
- **`tools_pending`**: calls that already have a result are not run again. The rest are
  *uncertain*, because the crash may have happened while they were executing.

Output retries already spent (schema failures and output validators) also come from
the checkpoint, so recovery does not grant a fresh retry budget. Usage, request and turn counts come from the checkpoint, so `UsageLimits` and budgets see
the whole run. The overall `timeout` restarts on recovery. Handoffs restore the active agent.

## Tool semantics: at-least-once or at-most-once

Nexura cannot know whether a tool that was running at the time of the crash finished its
side effect. You choose per tool:

- `@tool(idempotent=True)`: safe to repeat. Recovery re-runs it (**at-least-once**).
- Any other tool (default): recovery never re-runs it silently (**at-most-once**). What
  happens instead depends on `on_uncertain`:
  - `"pause"` (default): returns a paused result whose `interruption.reason` is
    `"outcome_unknown_after_crash"` and whose `pending` lists the uncertain calls. Approve
    (digest-bound, as in [approvals](approvals.md)) to run them, or reject, which tells the
    model "the outcome of this action is unknown; it was not retried".
  - `"fail"`: raises `RunRecoveryError`.
  - `"tell_model"`: gives the model that "unknown outcome" result and continues.

Tools that talk to systems with idempotency keys can also be marked `idempotent=True`
after you pass a key derived from `ctx.run_id` and `ctx.tool_call_id`.

## Concurrency, completion and storage errors

- **One recoverer per run.** `recover` takes an exclusive lease (`claim(run_id, owner,
  lease_seconds)` / `release`, implemented by the memory and SQLite stores; default lease
  one hour). A second concurrent recoverer raises `RunRecoveryError`. A crashed recoverer's
  lease expires. Custom stores without `claim` get no protection.
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

  Limits: fencing protects checkpoints, not your tools or the session store. A stale
  writer can still finish the step it is in before its next save is refused. Sessions
  use their own revision check. A writer without a lease is not fenced once the
  recoverer's lease has expired or been released. Recover only runs whose process is
  gone. Custom stores opt in by accepting `owner=` in `save`, and `owner=`/`fenced=` in
  `delete` (see `check_fence` in `nexura_agents.durable`).
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

- `MemoryCheckpointStore()`: tests and demos; does not survive the process.
- `SQLiteCheckpointStore(path)`: stdlib `sqlite3`, atomic upserts, a schema version, and a
  0600 file.
- Your own: implement `RunCheckpointStore` (`save`, `load`, `delete`, `list_incomplete`).

## Security and limitations

- Checkpoints contain the **full conversation transcript** (and tool arguments and results).
  Treat the store like session data: restrict access, encrypt the disk, delete old rows.
  The memory block of a run is stored as in an approval interruption; recalled memories are
  never written to sessions.
- A checkpoint that fails to save stops the run with the store's error (the durability
  promise is not silently dropped).
- Teams: a team member's run is checkpointed if the team's runner has a store, but
  recovering a team as a unit is not supported.
- Runs cancelled with `CancelledError` keep their checkpoint and can be recovered.
- Each checkpoint rewrites the whole transcript; very long runs write proportionally more.
