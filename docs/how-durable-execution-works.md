# How durable execution works

A process can die at any moment: a deploy, an out-of-memory kill, a lost machine. Nexura
keeps enough on disk to continue a stopped run from where it was, **without running a
completed step again and without silently repeating an action whose outcome is unknown**.
This page explains what happens, for SDK agents, workflows, trigger jobs and the service.
The exact contract (identities, keys, tables, store API) is the reference page
[Durable execution contract](durable-execution.md).

## One journal for every path

Every durable path writes the same **execution journal** (`nexura_agents.execution_store`):

| Path | Turn it on | Journal lives in |
|---|---|---|
| SDK agent (`Runner`) | `Runner(checkpoint_store=SQLiteCheckpointStore(...))` (or the memory or Postgres store) | the same database as the checkpoints |
| Workflow (`WorkflowEngine`) | `WorkflowEngine(spec, registry, store=SQLiteWorkflowStore(...))` (or `MemoryWorkflowStore`, `nexura_postgres.PostgresWorkflowStore`) | the same database as the checkpoints |
| Service runs (`nexura-runtime serve`) | on by default | the service database, or Postgres with `--workflow-state-url` |
| Trigger jobs (`nexura-runtime worker`) | on by default | the job's workflow run (above) |

For each run the journal records:

- **A lease with a fencing token.** The process that starts a run holds its lease from the
  first write and renews it every `lease_ttl / 3` (30 s by default). Every journal write
  checks the lease, so a process that lost the run (frozen past its lease, then taken
  over) gets `LeaseLostError` and writes nothing.
- **An intent before each step that is not `pure`.** Just before a tool call or workflow
  step runs, one fenced commit records that it is about to be sent, with its effect class
  and a stable **idempotency key** (`ctx.idempotency_key`, `step_idempotency_key()`).
- **An outcome after it returns.** The result and the next position are committed
  together. Only then does the run move on.
- **An effect record** per intent: `pending`, then `confirmed`, or `indeterminate` when
  nobody knows whether it happened, then `resolved` once a person decides.

## What happens after a crash

Recovery is explicit: `Runner.recover_async(agent, run_id)`,
`WorkflowEngine.recover(run_id)`, `POST /api/v1/runs/{run_id}/recover`, or a trigger worker
reclaiming its job. Recovery first takes the run's lease: at once when the owner was a
process on the same host that has exited, otherwise once the lease has expired. While
another live process holds it, recovery is refused, so a run that is only slow is never
run twice.

Then it continues **from the step the run was on**:

- **Completed steps are never run again.** Their outcomes are in the journal and the
  checkpoint; recovery starts at the cursor.
- **A journaled non-pure step with no intent has not dispatched**, so it can run.
  Pure steps deliberately write no intent and may already have started; they can repeat.
- **A step whose outcome was committed** but whose checkpoint was not saved is not run
  again: the recorded outcome is applied.
- **A step that was dispatched and has no outcome** is in doubt. What happens depends on
  the class it declared:

| Effect class | How you declare it | After a crash mid-step |
|---|---|---|
| `pure` | `effect="pure"`, manifest `pure` / `read` | runs again |
| `idempotent` | `effect="idempotent"`, `@tool(idempotent=True)`, manifest `idempotent_write` | runs again **with the same idempotency key**, so the remote side can de-duplicate |
| `reconcilable` | `effect="reconcilable", reconcile=fn` | `fn` is asked what happened: `Applied(result)` continues with that result, `NotApplied()` sends it again with the same key, `Unknown()` pauses |
| `non_idempotent` (the default for anything undeclared) | `effect="non_idempotent"`, or nothing | **never sent again silently**: the run pauses |

A paused run has `status="paused"` and `pause_reason="effect_unknown"`, and names the step
in `pending_effect`. Its effect is `indeterminate`. A person checks the remote system and
resolves it once:

- `confirmed` (with the result): it happened. The step's output is that result and the
  run continues **without calling the step again**.
- `not_applied`: it did not happen. The step is sent again as a new attempt with a new
  key; a step that needs approval asks for a fresh approval first.
- `failed`: the run fails.

Resolve with `WorkflowEngine.resolve_effect(run_id, resolution, actor=..., result=...)`,
`Runner.resume_async(agent, interruption, outcomes={call_id: ResolvedOutcome(result)})`, or
`POST /api/v1/runs/{run_id}/effects/{effect_id}/resolve` (then `POST .../recover`). The
decision and who made it are journaled; a second resolution is refused (`409
EFFECT_ALREADY_RESOLVED`).

For workflow calls, a raised handler exception, timeout or lost response after the intent
is also an uncertain outcome: non-idempotent calls pause, and reconcilable calls first
probe for `Applied`, `NotApplied` or `Unknown`. Pure failures fail the run without effect
records; idempotent calls may exhaust their configured retries and then fail. Pass
`reason=...` with a workflow resolution, or `"reason"` in the REST body, to audit why the
actor chose that outcome.

A failing parallel branch or mapped item cannot make an unresolved external action terminal.
The run pauses on the uncertain part, including a part cancelled when its sibling failed.
The sibling's error is saved; once every uncertain action is settled, the run fails with that
error without repeating the payments. A failure with only pure parts remains terminal and
creates no effects.

When a mapped or parallel part's reconciler returns `NotApplied`, a gated step asks for a
fresh approval before resending. Finished siblings retain their recorded outcomes, and the
part retains its idempotency key. Automatic reconciliation, resends and continuation share
one workflow deadline within the caller's invocation. An expired deadline preserves an
uncertain effect as `effect_unknown`. Even without a timeout, automatic reconciliation permits
at most three resends per part before pausing. An explicit resume or recovery starts a new
invocation with its own timeout allowance.

## Example: a payment that must not happen twice

```python
import asyncio
import tempfile
from pathlib import Path

from nexura_runtime.workflow import (
    ModuleRegistry, SQLiteWorkflowStore, WorkflowEngine, WorkflowNode, WorkflowSpec,
)


class Killed(BaseException):
    """Stands in for the process dying (nothing in the engine catches it)."""


async def main():
    ledger, calls, kill = [], {"check": 0, "pay": 0}, [True]

    def check(state):
        calls["check"] += 1
        return {"approved_amount": 100}

    def pay(state):  # undeclared, so non_idempotent
        calls["pay"] += 1
        ledger.append(state["approved_amount"])  # the payment provider's own record
        if kill[0]:
            kill[0] = False
            raise Killed  # the payment happened; its result was never recorded
        return {"paid": True}

    registry = ModuleRegistry()
    registry.register("check", check, effect="pure")
    registry.register("pay", pay)
    spec = WorkflowSpec(
        name="refund", entry_node="check", terminal_nodes=["pay"],
        nodes={
            "check": WorkflowNode(id="check", handler="check", default_transition="pay"),
            "pay": WorkflowNode(id="pay", handler="pay"),
        },
    )
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "runs.db"
        store = SQLiteWorkflowStore(db)
        try:
            await WorkflowEngine(spec, registry, store=store).run({}, workflow_run_id="r1")
        except Killed:
            pass
        await store.close()

        # A new process on the same database:
        store = SQLiteWorkflowStore(db)
        engine = WorkflowEngine(spec, registry, store=store)
        paused = await engine.recover("r1")
        assert paused.status == "paused" and paused.pause_reason == "effect_unknown"
        assert ledger == [100] and calls["check"] == 1  # nothing ran again

        # A person checked the provider: the payment went through.
        done = await engine.resolve_effect(
            "r1", "confirmed", actor="ops@example.test", result={"paid": True}
        )
        assert done.status == "completed" and ledger == [100] and calls["pay"] == 1
        await store.close()


asyncio.run(main())
```

## Approvals, waiting runs and budgets

- **A run waiting for a person holds no lease.** A run paused for approval or input
  survives any crash, and any process can resume it.
- **An approval is used up by the step it was given for.** Claiming the approval and
  writing the dispatch intent are separate operations. In the claimed-but-no-intent
  window, recovery checks the approval's binding and expiry and can dispatch under that
  approval; an expired one is asked for again. For workflows, the intent and its journal
  consumed marker commit together, after the separate approval claim. Once an intent
  exists, even a crash before the network send leaves an uncertain dispatch: recovery
  follows the effect-class rules. A `not_applied` resolution of a gated step needs a
  fresh approval.
- **Budget reservations end exactly once.** A reservation is linked to the step it was
  made for. On a takeover, a reservation of a step that was dispatched is settled as
  unknown usage at its estimate, and one of a step that never dispatched is released. A
  run started under a budget is not continued without one. Details:
  [Budgets and approvals across a takeover](durable-execution.md#budgets-and-approvals-across-a-takeover-s11).

## Trigger jobs continue their run

A schedule fire or webhook delivery keeps the same workflow run across attempts when
that run has an execution journal.
When the worker running it dies, another worker reclaims the job and **continues the same
run** with `WorkflowEngine.recover`: completed steps are not run again, an in-doubt step
follows its effect class, a run that already completed is never run again, and a run
waiting for a person is left waiting. The job's queue lease only decides which worker
handles the job; the run lease decides who executes the run.

The compatibility fallback in `TriggerWorker._continue` handles an old run without a
journal by marking it interrupted and calling `_start` to create a replacement run.
It preserves the old id in `DurableTriggerJob.previous_run_ids`. The one-run guarantee
applies to journaled runs; this fallback can repeat work whose effects were not journaled.

## Backends

- **Memory** stores: tests and demos; nothing survives the process.
- **SQLite** (`SQLiteCheckpointStore`, `SQLiteWorkflowStore`, the service default): one
  host. Lease expiry uses SQLite's clock, so do not share one file between machines.
- **Postgres** (`nexura-postgres`: `PostgresCheckpointStore`, `PostgresWorkflowStore`,
  `PostgresTriggerStore`): several hosts; expiry uses the database clock.

The crash points (kill before dispatch, mid-step, after the remote committed, after the
outcome, a frozen stale owner, approval windows, racing recoverers) are tested with real
`SIGKILL`ed processes in `tests/durability`, on SQLite and, given a test database, Postgres.

## What is not covered

- **Teams and `AgentCoordinator`** runs are not journaled; recover a team member's run on
  its own, not the team as a unit.
- **Custom checkpoint stores** without `open_execution_store()` keep the older behaviour:
  no run lease for the original run, and every call of an interrupted batch counts as in
  doubt.
- **`fork`** starts a new run from a checkpoint and runs the forked step again, on purpose.
  **In-process retries** (`retry=` on a node or a tool) repeat a step inside a live run.
  Workflows and SDK tools allow them only for `pure` or `idempotent` handlers. Workflow
  validation and runtime dispatch both refuse retries of `non_idempotent` (including
  undeclared functions) and `reconcilable` handlers, including inside composite steps.
- **A synchronous tool running in a thread** cannot be stopped when its process loses the
  lease; it may finish its call, but its result is never written and the new owner treats
  the call as in doubt.
- A run killed **before its first journal write** has no journal; the service refuses to
  recover it (`409 RUN_NOT_JOURNALED`); fork it from a checkpoint instead.
- **SQLite** leases are single-host. Use Postgres for several machines.

## Removed APIs

Three older paths were removed once everything above ran on the journal (A6 S12). Importing
one raises `RemovedAPIError` (an `ImportError`) that names the replacement:

| Removed | Use instead |
|---|---|
| `DurableExecutionEngine`, `DurableWorkflowSpec`, `DurableNodeSpec`, `DurableRunState`, `DurableExecutionModule`, `NodeIntent`, `WorkerLease`, `FencingConflictError`, `LeaseExpiredError` (`nexura_runtime.runtime.execution`) | `WorkflowEngine` with `SQLiteWorkflowStore` or `PostgresWorkflowStore` (`recover`, `resolve_effect`); leases and fencing in `nexura_agents.execution_store` (a stale owner gets `LeaseLostError`) |
| `EffectReconciler` and its models and errors (`nexura_runtime.runtime.effects`) | effect records in the journal: `ExecutionStore.resolve_effect`, `WorkflowEngine.resolve_effect`, `POST /api/v1/runs/{run_id}/effects/{effect_id}/resolve` |
| `TriggerWorker(retry_mode="new_run")`, `reclaim_expired_jobs(keep_run=False)` | the default: a reclaimed job continues its run. Start over on purpose with `fork` or a new run |

Runs the removed engine left in a database stay readable (`StorageRepository.get_run`,
`get_run_events`, the journal and the effects API), and their effects can still be
resolved; `update_run` on one is refused. The old `effect_records` table of the removed
`EffectReconciler` is left as it was and is no longer read.

## See also

- [Durable execution contract](durable-execution.md): the reference.
- [Durable agent runs](durable-runs.md): checkpoints, recovery and resolution for agents.
- [Workflows: recovering after a crash](workflows.md#recovering-after-a-crash)
- [Human approval](approvals.md)
- [Budgets and usage](budgets-and-usage.md)
