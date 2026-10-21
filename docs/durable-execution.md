# Durable execution contract

**Contract v1 (being implemented; today's behaviour per path is in the design report)**

This page describes the target shared contract for SDK agents, workflows and trigger
jobs. S1 adds value types and documentation; S2 adds standalone memory, SQLite
and Postgres execution stores. S3 rebuilds the deprecated `DurableExecutionEngine`
on those stores, and S4 adds effect resolution over REST and the TypeScript client
(see [DurableExecutionEngine on the journal](#durableexecutionengine-on-the-journal-s3)
and [Resolving an unknown effect](#resolving-an-unknown-effect-s4)). S5 puts the SDK
`Runner` on the journal (see [SDK agents on the journal](#sdk-agents-on-the-journal-s5)).
S6 puts top-level `WorkflowEngine` runs on it (see
[Workflows on the journal](#workflows-on-the-journal-s6)), and S8 adds a Postgres workflow
store (see [Workflows on Postgres](#workflows-on-postgres-s8)). The trigger worker does not use
the journal yet, so its retry behavior is unchanged. The A6 design report
(`P35_A6_DURABLE_EXECUTION_DESIGN.md`, an internal planning record that is not part of
the public repository) gives today's behavior per path in §1 and the implementation
sequence in §3. Teams and
`AgentCoordinator` are outside this contract for now.

The founder approved these defaults:

- **Undeclared effects are `non_idempotent`.** A tool or workflow function must
  declare another class to permit automatic recovery after an uncertain dispatch.
- **Unknown outcomes pause the run:** status `paused`,
  `pause_reason="effect_unknown"`. A person resolves the outcome before it continues.
- **A reclaimed trigger job continues the same run.** A completed run is never
  executed again. The planned `retry_mode="new_run"` compatibility option preserves
  the old behavior for one release.

## Execution identity

`ExecutionId` is the pair `(workspace_id, run_id)`. The run id is opaque. A run
also records its kind (`agent`, `workflow` or `durable_graph`), its
`definition_digest`, and its origin (`api`, `sdk`, or a trigger's `job_id` and
`attempt`). Sub-workflows, map items and sub-agents record `parent_run_id`;
existing child id formats stay supported. One trigger job owns one run throughout
its life, including reclaim attempts.

The definition digest is a workflow's `canonical_hash()`, or
`Agent.definition_digest()` (S5): agent name, instructions hash, tool names, argument
JSON schemas and effect classes, handoff targets, and output schema.

Each execution step has a `StepKey`:

- Agent tools: `turn:<n>/call:<tool_call_id>`; model calls: `turn:<n>/model`.
- Workflows: `<node_id>#<step_count>[.<attempt>][[<item>]]`. The optional map item
  uses one literal bracket pair, for example `charge#3.2[0]`. The helper omits
  attempt 1, matching existing child ids. Counters and item indices are
  non-negative integers; attempts start at 1.

An effect has a zero-based `effect_index` within its step. Its `effect_id` is the
SHA-256 hex digest over `(workspace_id, run_id, step_key, effect_index)`. S1 makes
the tuple encoding explicit: a compact JSON array in that order, with UTF-8,
`ensure_ascii=False`, and separators `(',', ':')`. This preserves field boundaries
even when opaque ids contain separators. The default idempotency key is `nx-`
plus the first 32 hex characters of that digest.

Takeover and retries of the same persisted step reuse its key. A new graph visit
has a new step count and therefore a different key. Helpers are
`StepKey.agent_tool`, `StepKey.agent_model`, `StepKey.workflow`,
`derive_effect_id`, and `default_idempotency_key`, exported from
`nexura_agents.contracts`. These helpers do not dispatch effects.

## Leases and fencing

Each run has one lease on its run row: `lease_owner`, `lease_expires_at`, and a
monotonically increasing `fencing_token`. `claim(run, owner, ttl)` succeeds if the
lease is free, expired, or already held by that owner. Takeover increments the
token. The original owner holds a lease from its first write. Pausing releases
the lease; resuming claims it again.

Every journal write is one transaction guarded by
`WHERE fencing_token = :mine AND lease_owner = :me`. A stale owner receives
`LeaseLostError`; the transaction writes and deletes nothing. A heartbeat renews
the lease every `ttl/3`. In the SDK `Runner`, a failed renewal (or any refused journal
write) stops the run: its in-flight model call or `async` tool is cancelled and the run
ends with `LeaseLostError`; a synchronous tool running in a thread cannot be stopped and
finishes, but its result is never written. The runtime also checks the lease against the
store immediately before each non-pure dispatch (the intent commit, or a fenced renew
when a crash re-run reuses its intent).

The store's clock determines expiry: Postgres uses `clock_timestamp()` read after the row
lock (`now()` is the transaction start and could be stale after a lock wait). SQLite is
supported on one host; multi-host operation requires Postgres. Effects receive
`ctx.fencing_token`; connectors should forward it when the remote system supports
a header or compare-and-swap field. Fencing a journal commit cannot stop an
already running synchronous tool thread from finishing a remote effect, so its
dispatched intent remains authoritative. A trigger queue lease coordinates the
queue only; the run lease controls execution.

## Step intents, outcomes and effects

Immediately before each non-pure call, persist a `StepIntent` containing its
`step_key`, `effect_class`, `payload_digest`, `idempotency_key`, optional
`approval_id`, `reservation_ids`, and `status="dispatched"`. Commit it with the
cursor and state in the same fenced transaction. Pure steps need no intent;
their output is included in the outcome commit.

After a call returns, commit `StepOutcome(status="completed" | "failed", output)`
and advance the cursor together. Intent granularity is one effect: one tool call
just before invocation, rather than a whole batch, or one workflow node, branch
or map item.

The planned `nexura_exec_effects` journal uses `EffectRecord` with states
`pending → confirmed | indeterminate → resolved`. Resolution is a separate
machine-readable value: `confirmed`, `not_applied` or `failed`, with the resolving
actor recorded. `(workspace_id, idempotency_key)` must be unique. S1's dataclasses
describe records; later store steps enforce transitions, uniqueness and fencing.
For JSON-compatible payloads, `dataclasses.asdict` and stdlib JSON serialize the
records; their constructors restore nested identity/intent records and enums.

## Effect classes

| Class | Declaration or existing alias | Recovery promise |
| --- | --- | --- |
| `pure` | Manifest `pure` or `read`; handler `effect="pure"` | May repeat freely. |
| `idempotent` | Manifest `idempotent_write`; `@tool(idempotent=True)`; `effect="idempotent"` | Repeat with the same key from the intent. The tool must forward `ctx.idempotency_key` or be naturally idempotent. |
| `non_idempotent` | Manifest `non_idempotent_write`; plain `@tool`; undeclared workflow function | Never silently repeat after an uncertain dispatch. |
| `reconcilable` | Tool `effect="reconcilable", reconcile=fn`; module `reconcile_operation` | Probe with `reconcile(ctx, intent)` before deciding whether to repeat. |

Model calls are `pure` for execution; reservations handle uncertain billing.
`effect_class_from_policy` maps existing manifest/runtime string values without
importing `nexura_runtime`. `effect_class_from_idempotent` maps the tool
boolean. Missing declarations map to `non_idempotent`; unknown policy strings
are rejected. SDK tools declare their class with `@tool(effect=..., reconcile=...)`
since S5; workflow handlers use `register_handler(name, effect=..., reconcile=...)` since
S6.

## Recovery after a crash

Before an intent commits, the step may run: no dispatch has been recorded. After
an outcome commits, execution continues from the stored cursor. Between those
two commits, the remote system may have received nothing, committed an effect
whose response was lost, or returned a result that was never saved. Recovery
uses the class rules in all three cases:

| Class | Dispatched intent exists, outcome missing |
| --- | --- |
| `pure` | Repeat. |
| `idempotent` | Repeat with the persisted key; remote de-duplication or natural idempotence prevents a duplicate effect. |
| `non_idempotent` | Pause with `pause_reason="effect_unknown"`. |
| `reconcilable` | Probe: `Applied(result)` records the outcome and continues; `NotApplied` permits repeating with the same key; `Unknown` pauses. |

An unknown effect sets the run status to `paused`, stores the intent as
`pending_effect`, and marks the effect `indeterminate`. A person chooses:

- `confirmed(result)`: it happened; continue with that result.
- `not_applied`: it did not happen; a gated step needs a **fresh approval** before
  re-dispatch.
- `fail`: stop; the stored resolution is `failed`.

For agents, existing `on_uncertain="pause" | "fail" | "tell_model"` options remain
caller policy on top of this contract.

## Versions and definition changes

The run row carries `journal_schema_version` for storage, `state_version` for the
engine envelope (agent and workflow checkpoint v2), and `definition_digest`.
Readers accept versions N and N-1; writers write N. Journal migrations go through
`nexura_agents.execution_store.migrations`, shared by SQLite and Postgres.

Takeover with a different digest raises `DefinitionMismatchError`. A future
explicit `migrate=` hook is the only bypass. Agents get one release of warnings
before this check becomes mandatory: since S5, recovering or resuming an agent run
with a different `Agent.definition_digest()` emits a `RuntimeWarning` and continues.

## Approvals and budgets across takeover

Claiming an approval and writing the linked step intent happen in one fenced
transaction, called "claim for dispatch".

- Waiting for approval holds no lease and survives a crash.
- If an approval was claimed but no intent was written, the new owner rechecks
  the payload digest and expiry. It can dispatch using the same approval, or ask
  again if that approval expired.
- Once the intent is dispatched, the class rules above apply and the approval
  is consumed. A `not_applied` resolution needs a fresh approval for a gated
  re-dispatch. A2 supplies SDK consumed records; A6 links them to intents.

Reservations carry `run_id` and `metadata.step_key`. Takeover examines active
reservations: a dispatched intent without an outcome settles as **unknown usage
at the estimate** and ends the reservation; a reservation with no intent is
released because it never dispatched. Restore the run budget with
`manager.get_run_budget(run_id)` before executing another step, shared with A1.

The contract and memory/SQLite stores live in `nexura_agents`, using the standard
library and existing SDK error types. Postgres remains optional in
`nexura-postgres`. S1 and S2 add no SDK dependencies.

## Standalone store API (S2)

`nexura_agents.execution_store` exports `ExecutionStore`, `MemoryExecutionStore`,
`SQLiteExecutionStore`, lease/snapshot types and store errors.
`nexura_postgres.PostgresExecutionStore` implements the same asynchronous protocol.
`DurableExecutionEngine` is the first executor that uses them (S3).

Call `init_db()` before using a new SQL database and `close()` when finished.
`create(execution_id, owner=..., definition_digest=..., state=..., ttl=...)`
creates the run with its original lease already held. It returns an `ExecutionRun`;
its `.lease` is the handle for fenced operations. `claim` requires the definition
digest and returns a lease or `None` when another owner is still active. Missing
runs raise `KeyError`; digest mismatches raise `DefinitionMismatchError`.

`commit(lease, state=..., cursor=..., steps=..., effects=..., events=...)` replaces
the state/cursor and appends journal facts atomically. The optional
`expected_revision=` also refuses the commit with `ExecutionConflictError` when the run's
revision changed since the caller read it (a compare-and-swap inside the fenced
transaction). `steps` accepts S1
`StepIntent` and `StepOutcome` objects; effects must link to a persisted intent.
State and outputs must already be JSON-compatible. A step has at most one
immutable intent and one immutable outcome; exact repeats are accepted. New
effects start `pending`, then become `confirmed` or `indeterminate`. Run status
`paused`, `completed` or `failed` releases the lease in that transaction.

`get(execution_id)` returns a detached, consistent snapshot. `renew` and `release`
require a current lease; stale or expired handles raise `LeaseLostError`.
`list_recoverable(workspace_id=...)` lists only running/interrupted runs whose
lease is free or expired, excluding paused and terminal runs. Terminal runs cannot
be claimed again.

To resolve an indeterminate effect, claim the run and call
`resolve_effect(lease, effect_id, resolution, actor=..., result=...)`.
Resolution and an actor audit event commit together. A second resolution raises
`EffectAlreadyResolvedError`; other journal/key conflicts raise
`ExecutionConflictError`. Resolving an effect does not execute, approve or resume
anything. If the run remains paused, resolution releases the temporary lease.

SQLite uses WAL with `synchronous=FULL` and short `BEGIN IMMEDIATE` transactions.
It is **single-host only**; methods can block the event loop while waiting for
SQLite's writer lock. Its SQL clock supplies expiry time. Postgres uses
`FOR UPDATE` and the server's `clock_timestamp()` after acquiring the row lock;
transaction-start `now()` could already be stale after a lock wait.

The new tables are `nexura_exec_runs`, `nexura_exec_steps`, `nexura_exec_effects`
and `nexura_exec_events`. Versioned transactional migrations live in
`nexura_agents.execution_store.migrations`, shared by both SQL backends, with a
separate `nexura_exec_migrations` ledger. This keeps the SDK independent of runtime
storage migrations and leaves older tables untouched. Unknown migration versions
are refused. Readers accept journal versions 0/1 and state versions 1/2; engine
state commits write journal version 1 and state version 2. Callers supply the v2
state payload when upgrading; the store does not transform engine envelopes.

An offline example using only the memory store:

```python
import asyncio
from nexura_agents.contracts import ExecutionId, StepKey, StepOutcome
from nexura_agents.execution_store import MemoryExecutionStore

async def example():
    store = MemoryExecutionStore()
    await store.init_db()
    identity = ExecutionId("local", "example")
    run = await store.create(
        identity, owner="worker", definition_digest="example-v1", state={}
    )
    await store.commit(
        run.lease,
        state={"answer": "local result"},
        status="completed",
        steps=[StepOutcome(StepKey.agent_model(0), "completed", "local result")],
    )
    snapshot = await store.get(identity)
    assert snapshot.run.state["answer"] == "local result"
    assert snapshot.run.lease_owner is None
    await store.close()

asyncio.run(example())
```

## DurableExecutionEngine on the journal (S3)

`DurableExecutionEngine` (`nexura_runtime.runtime.execution`, deprecated, removed in
0.6) keeps its public methods and errors, but its runs now live in the execution
journal as `kind="durable_graph"`:

- Pass a `StorageRepository` as before: `SQLiteRepository` uses a
  `SQLiteExecutionStore` on the same database file and `PostgresRepository` a
  `PostgresExecutionStore` on the same pool. You can also pass an `ExecutionStore`
  directly. `engine.get_execution_store()` returns the store in use.
- `repository.get_run`, `update_run` and `get_run_events` serve these runs from the
  journal (the legacy `runs`/`run_events` rows are not written for them), so existing
  callers keep working and a stale `update_run` is still refused.
- Leases, fencing tokens and expiry come from the journal and the store's clock.
  `claim_run(..., lease_duration=)` bounds each step as before: this engine does
  **not** heartbeat, and a step that outlives its lease cannot commit
  (`LeaseExpiredError`). A changed spec raises `DurableDefinitionMismatchError`, which is
  both a `DurableExecutionError` and a `DefinitionMismatchError`.
- Before a non-pure node runs, the same fenced commit that records the legacy
  `pre_dispatch_intent` writes a `StepIntent` and a `pending` effect. A handler that
  returns marks it `confirmed` with its output. A missing handler is detected before any
  intent is written.
- A non-pure handler that **raises or times out** may still have applied its effect (a
  lost response looks like a refusal), so the run pauses (`status="paused"`,
  `pause_reason="effect_unknown"`, effect `indeterminate`, lease released) and the
  exception (`DeadlineExceededError` for a timeout) propagates; resolve it as below. A
  `pure`/`read` handler that raises or times out fails the run as before.
- Cancelling the worker task (worker shutdown) is treated as a crash: nothing is
  committed, the dispatched intent stays, and after the lease expires the next worker
  applies the crash rules below. `request_cancellation` while a node is in flight ends the
  run `cancelled` and marks that node's effect `indeterminate`; the handler's late result
  is refused.
- Handlers that take an `ExecutionContext` get `ctx.attributes["idempotency_key"]`
  (stable for this graph visit across takeover) and `ctx.attributes["fencing_token"]`
  (the journal token; `DurableRunState.fencing_token` keeps the legacy numbering, one
  lower).
- After a crash with a dispatched intent: `pure`/`read` nodes re-run, `idempotent_write`
  nodes re-run with the same key, and `non_idempotent_write` nodes now **pause**
  (`status="paused"`, `pause_reason="effect_unknown"`, `pending_effect` set, effect
  `indeterminate`) and raise `IndeterminateEffectError`, instead of failing.
- **Behavior change:** `DurableNodeSpec.effect_policy` now defaults to
  `"non_idempotent_write"` (it was `"pure"`), following the founder default that
  undeclared effects are `non_idempotent`. Declare `effect_policy="pure"` or `"read"`
  for nodes that may repeat. This also changes the spec digest, so a run started with an
  undeclared policy must continue with the same declaration.
- Upgrade note: runs started before 0.5 live only in the legacy `runs` table and are not
  visible to the rebuilt engine; finish them on 0.4 first.

`EffectReconciler` (`nexura_runtime.runtime.effects`) is now a deprecated synchronous
wrapper (removed in 0.6) over `SQLiteExecutionStore` effects: each legacy `effect_id`
is a journal run `legacy-effect:<effect_id>` with one effect. `reconcile_effect(...,
actor=...)` records who resolved it; `rolled_back` is stored as `not_applied` and
`voided` as `failed`; a second resolution raises `ReconciliationError`. Rows in the
pre-0.5 `effect_records` table are imported when the wrapper opens the database (the old
table is left as it was). Idempotency keys are unique per workspace, so a second effect
with the same key is refused.

## Resolving an unknown effect (S4)

A paused run waits for a person. `DurableExecutionEngine.resolve_effect(workspace_id,
run_id, effect_id, resolution, actor=..., result=...)`, `ExecutionStore.resolve_effect`
and the REST endpoints record the decision once:

- `confirmed` with `result`: it happened; the node completes with `result` as its output
  and the handler is **not** called again.
- `not_applied`: it did not happen; the node runs again as a new attempt with a new
  idempotency key.
- `failed`: the run fails.

The decision and actor are journaled together (an `effect_resolved` event). A second
resolution raises `EffectAlreadyResolvedError` (REST: `409 EFFECT_ALREADY_RESOLVED`).
Resolution runs nothing; the next `claim_run`/`run_until_idle` continues the run.

```python
import asyncio
from nexura_agents.contracts.errors import IndeterminateEffectError
from nexura_agents.contracts.execution import EffectResolution, ExecutionId
from nexura_runtime.runtime.execution import (
    DurableExecutionEngine, DurableNodeSpec, DurableWorkflowSpec, NodeIntent,
)
from nexura_runtime.storage import SQLiteRepository

spec = DurableWorkflowSpec(
    workflow_id="refund", entry_node="pay", terminal_nodes=["pay"],
    nodes={"pay": DurableNodeSpec(node_id="pay", handler="pay")},  # non_idempotent
)

async def example():
    repository = SQLiteRepository(":memory:")
    engine = DurableExecutionEngine(repository, {"pay": lambda state: {"paid": "local fake"}})
    await engine.init_run("local", "run-1", spec)
    # Simulate a worker that crashed right after recording its dispatch.
    state = await engine.claim_run("local", "run-1", "crashed-worker")
    state.pre_dispatch_intent = NodeIntent(
        node_id="pay", effect_policy="non_idempotent_write", dispatched=True
    )
    await repository.update_run(
        "local", "run-1", state.revision, "running", state.model_dump()
    )
    try:
        await engine.execute_step("local", "run-1", "crashed-worker", spec)
    except IndeterminateEffectError:
        pass
    paused = await engine.get_state("local", "run-1")
    assert paused.status == "paused" and paused.pause_reason == "effect_unknown"

    store = await engine.get_execution_store()
    snapshot = await store.get(ExecutionId("local", "run-1"))
    (effect_id,) = snapshot.effects
    await engine.resolve_effect(
        "local", "run-1", effect_id, EffectResolution.CONFIRMED,
        actor="ops@example.test", result={"paid": "confirmed on the ledger"},
    )
    done = await engine.run_until_idle("local", "run-1", "worker-2", spec)
    assert done.status == "completed" and done.state["paid"] == "confirmed on the ledger"
    await repository.close()

asyncio.run(example())
```

Over HTTP, `GET /api/v1/runs/{run_id}/effects` lists a run's effects and
`POST /api/v1/runs/{run_id}/effects/{effect_id}/resolve` with
`{"resolution": "not_applied"}` resolves one; the authenticated caller is recorded as
`resolved_by` and the `approve` permission is required. The service reads the journal
in its own database (`create_api_app(execution_store=...)` supplies another store, for
example Postgres). Only `DurableExecutionEngine` writes effects there today; other runs
list none. The TypeScript client has `client.runs.effects(runId)` and
`client.runs.resolveEffect(runId, effectId, {resolution, result})`. Studio has no effects
view yet. Endpoint details are in [Studio and service](studio-and-service.md).

## SDK agents on the journal (S5)

`Runner(checkpoint_store=...)` with `MemoryCheckpointStore`, `SQLiteCheckpointStore` or
`nexura_postgres.PostgresCheckpointStore` keeps an execution journal (`kind="agent"`)
next to the checkpoints: an `ExecutionStore` on the same database, opened with
`await store.open_execution_store()`. The checkpoint transcript stays what the model
sees; the journal records the lease and what was dispatched. Details and examples are
in [Durable agent runs](durable-runs.md).

- **Lease from the first write.** A run creates its journal row with a lease held by
  its own owner before its first checkpoint, and takes the store's recovery claim with
  the same owner. A heartbeat renews both every `lease_ttl / 3`
  (`Runner(lease_ttl=30.0)`). `recover` refuses a run whose lease is live (`"already
  being recovered"`), except when the lease owner is a process on the same host that
  has exited: owners are `nx1.<host>.<pid>.<start>.<uuid>` and
  `nexura_agents.durable.lease_owner_is_dead` checks `/proc`, so a restart on the same
  host recovers at once. A pid that `/proc` hides (`hidepid`) is confirmed with
  `kill(pid, 0)` (`EPERM` means alive). Owners on other hosts, and every owner on a host
  without a boot id (macOS, BSD, Windows, where the hostname alone does not identify a
  machine), are taken over only after the lease expires. A lost lease stops the run: the
  heartbeat cancels its in-flight model call or `async` tool, and the run ends with
  `LeaseLostError` (a synchronous tool thread cannot be stopped; its result is not
  written). Its checkpoint saves are refused as before (`CheckpointLeaseLostError`).
  Pausing, completing or failing releases the lease; `resume` claims it again.
- **Per-call intents.** Just before a non-pure tool call starts (after its input
  guardrails, cache and `before_tool` hook), the runner commits a `StepIntent` (step key
  `turn:<n>/call:<tool_call_id>`, with `%` and `#` in the id written as `%25` and `%23`;
  attempt k adds `#<k>`) and a `pending` effect. A crash re-run that reuses an existing
  intent first renews the lease against the store. When the result is in the checkpoint
  it commits the `StepOutcome` and marks the effect `confirmed` (a timed-out call:
  `indeterminate`); if a crash came between the two, recovery confirms it from the
  checkpoint. Pure calls write no intent. On recovery, an unanswered call without an
  intent never started and simply runs; a sibling in the same batch is no longer a false
  "unknown". The class rules use the class **persisted in the intent** (or the current
  declaration if that is stricter), so re-declaring a crashed tool cannot loosen them.
- **Keys and tokens.** Tools get `ctx.idempotency_key` (`nx-` + 32 hex characters of the
  effect id of their step key; the same for a crash re-run, a new one for a new
  attempt) and `ctx.fencing_token` (the run lease's token; `None` without a journal).
- **Effect classes.** `@tool(effect="pure" | "idempotent" | "non_idempotent" |
  "reconcilable", reconcile=fn)`; `idempotent=True` is the alias of
  `effect="idempotent"`; undeclared tools are `non_idempotent`.
- **Unknown outcome.** A dispatched `non_idempotent` call (or a reconcilable one whose
  probe says `Unknown`) pauses the run: `RunResult.interruption.pause_reason ==
  "effect_unknown"` (its `reason` stays `"outcome_unknown_after_crash"`), journal status
  `paused` with `state["pause_reason"]`, the effect `indeterminate`. Resolve it with
  `runner.resume_async(agent, interruption, outcomes={call_id: ResolvedOutcome(result)})`,
  `ResolvedOutcome.not_applied()` (sent again as attempt 2, step key `...#2`, with a new
  key; a tool that requires approval pauses for a fresh approval, `reason
  "effect_not_applied"`) or `ResolvedOutcome.failed(reason)` (the run fails). The
  resolution and actor (`resume_async(..., actor="ops@example")`, default
  `"sdk:resume"`) are journaled with `resolve_effect`. Effects resolved directly in the
  journal (for example through the effects API) let `recover_async` continue, and the
  stale pause can no longer be resumed. `approve` sends the call again and journals
  attempt 1 as `not_applied` (same actor); `reject` tells the model the outcome is
  unknown and journals nothing. A pause that reached the checkpoint but not the journal
  (a crash between them) is written to the journal by the next `recover`.
- **Reconcilable tools.** On recovery, `reconcile(ctx, intent)` runs with the call's
  `idempotency_key`: `Applied(result)` records the result and the call is not run again;
  `NotApplied()` runs it again with the same key (a gated tool first needs a fresh
  approval); `Unknown()`, an exception or a wrong return value pauses as above.
- **Definition digest.** The run stores `Agent.definition_digest()` of the starting
  agent; a difference on recover or resume is a `RuntimeWarning` in this release.
- **Older stores.** A store whose `save` does not accept `owner=` or that has no
  `open_execution_store()` works as before (no run lease, batch-level uncertainty). A
  store without `claim` gets a `DeprecationWarning` from `Runner(...)`; it will not be
  accepted in 0.6. Checkpoints written before S5 (or by a store without a journal) are
  journaled when they are first recovered or resumed, with `state["pre_journal_turn"]`:
  their unanswered calls of that turn or earlier have no intent although they may have
  been sent, so they keep the old rule (a `non_idempotent` one pauses as
  `effect_unknown`, a reconcilable one is probed).
- **Workspaces.** The SQLite checkpoint, claim and approval tables are scoped by
  `workspace_id` (checkpoint schema 2; a schema-1 file is migrated on open and its rows
  belong to `"default"`), like Postgres. A store opened with another workspace neither
  lists nor recovers the run, and no journal row is created for it.
- **Finishing.** A run's terminal journal status is committed before its checkpoint is
  deleted; a crash in between leaves a terminal row whose `completed` checkpoint
  `recover` replays (another phase is deleted with a "has already failed" error).

Limits: budgets and approval records are not yet linked to intents (S11); teams and
`AgentCoordinator` are outside the contract; the memory journal keeps finished runs'
rows for the life of the store.

## Workflows on the journal (S6)

`WorkflowEngine` with `MemoryWorkflowStore` or `SQLiteWorkflowStore` keeps an execution
journal (`kind="workflow"`, `definition_digest` = the spec's `canonical_hash()`) for each
top-level run: a `MemoryExecutionStore`, or a `SQLiteExecutionStore` in the same database
file, opened with `await store.open_execution_store()`. The checkpoint stays the state the
graph reads; the journal records the lease and what was dispatched. User guide and example:
[Workflows: recovering after a crash](workflows.md#recovering-after-a-crash).

- **Lease and fencing.** `run` and `fork` create the journal run with a lease held by the
  caller (owner `nx1.<host>.<pid>.<start>.<uuid>`); a heartbeat renews it every
  `lease_ttl / 3` (`WorkflowEngine(lease_ttl=30.0)`). Pausing, completing or failing
  releases it; `resume` and `resume_interrupt` claim the approval first (the first
  claimer wins; others get `ApprovalAlreadyClaimedError`), then take the lease. Checkpoints carry a
  `revision`: `get()` returns it, a save of a checkpoint whose `revision` is set succeeds
  only if the stored revision is unchanged (`WorkflowConflictError` otherwise), and
  `claim_approval` advances it. The journal commit comes first (token-fenced), then the
  checkpoint save (revision-fenced), so a stale owner's next write raises
  `LeaseLostError` and writes nothing.
- **Intents per node.** Just before a node that is not `pure` dispatches, one fenced
  commit writes its `StepIntent` (step key `<node>#<step>[.<attempt>]`, payload digest of
  the inputs the node reads, idempotency key, effect class, and the `approval_id` it runs
  under), a `pending` effect, and the approval as consumed ("claim for dispatch"). After
  the node returns, its `StepOutcome` (state update, branch, next node), the effect
  `confirmed`, and the new cursor are committed together, then the checkpoint is saved.
  A pure node writes only the outcome.
- **Effect classes.** `ModuleRegistry.register(name, fn, effect=..., reconcile=...)` and
  `register_handler(name, effect=...)` accept `pure`, `idempotent`, `non_idempotent`,
  `reconcilable` (with `reconcile(state, intent)` returning `Applied`, `NotApplied` or
  `Unknown`) and the manifest spellings. Undeclared function handlers are
  `non_idempotent`. Modules use their manifest `effect_policy`; a module that leaves it
  unset but declares write operations counts as `non_idempotent` for a call that writes.
  Sub-workflow, parallel and mapped steps are journaled per part (S7, below).
- **Keys and tokens.** `ExecutionContext.idempotency_key(index=0)` and
  `ExecutionContext.fencing_token` for modules; `step_idempotency_key(index=0)` and
  `step_fencing_token()` in `nexura_runtime.workflow` for function handlers. Mapped items
  (`node#3[0]`) and parallel branches (`node/branch#3`) get their own keys.
- **`WorkflowEngine.recover(run_id, *, on_uncertain="pause")`** continues a `running` or
  `interrupted` run (and a paused `effect_unknown` run whose effect is resolved) per the
  crash table above: no intent -> run; outcome committed but checkpoint lost -> apply it;
  `pure`/`idempotent` -> run again (same key); `reconcilable` -> probe; `non_idempotent`
  -> `paused`, `pause_reason="effect_unknown"`, `pending_effect`, effect `indeterminate`
  (`on_uncertain="fail"` fails the run). Approval claimed but no intent written -> the
  step runs under the same approval after re-checking its node, payload digest, binding
  and expiry; otherwise the approval is asked for again. A run whose lease is live
  raises `WorkflowError`; a dead owner on the same host is taken over at once. Other
  paused runs are returned unchanged.
- **Resolution.** `WorkflowEngine.resolve_effect(run_id, resolution, actor=...,
  result=...)` (or the effects REST route followed by `recover`): `confirmed` -> the
  node's output is `result` and it does not run again; `not_applied` -> new attempt
  (`<node>#<step>.2`, new key), a gated node first asks for a fresh approval; `failed` ->
  the run fails. `WorkflowRunResult` has `pause_reason` and `pending_effect`;
  `WorkflowCheckpoint` has `pending_effect`, `revision` and a `pause_reason` property.

Limits: `SQLiteWorkflowStore` is single-host (for several hosts use
`PostgresWorkflowStore`, S8 below); the service still marks runs interrupted with its startup sweep and has no recover
endpoint (S9); trigger reclaim still starts a new run (S10); budgets are not yet linked to
intents (S11).

## Composite workflow steps and module keys (S7)

User guide and example:
[Workflows: parallel, map and sub-workflow steps](workflows.md#parallel-map-and-sub-workflow-steps).

- **Container intent.** A parallel, `map_over` or sub-workflow node with any non-pure part
  writes an intent of class `pure` for the step itself (`<node>#<step>`, carrying the
  approval it runs under): running the step again is safe because each part decides for
  itself. Its outcome (the merged update) is committed as for any node.
- **Parts.** Each parallel branch (`<node>/<branch>#<step>[.<attempt>]`) and mapped item
  (`<node>#<step>[.<attempt>][<item>]`) that is not `pure` writes its own intent (payload
  digest of its inputs, its own idempotency key, its effect class, the step's approval)
  just before it runs; every part's outcome (`{"update": ...}`) and confirmed effect are
  committed as soon as it returns. Part attempts start at the step's attempt; a part
  resolved `not_applied` is dispatched as the next attempt (`m#3.2[1]`).
- **Recovery per part.** When `recover` runs the step again: a part with an outcome is not
  run again (its update is used); a part without an intent runs; a part dispatched by a
  stopped process follows §"Recovery after a crash" with its own key, and a
  `non_idempotent` part (or a probe answering `Unknown`) is in doubt. The other parts
  finish first; then the run pauses `effect_unknown` with a `pending_effect` naming the
  first part in doubt (`part`, `item` / `branch`, `run_id`) and every part in doubt
  becomes `indeterminate`. A resolution applies to that part; `not_applied` on a part of
  a gated step asks for a fresh approval of the step. An in-process retry (`retry=`)
  keeps finished parts and re-runs the failed ones with their keys.
- **Sub-workflow children.** A child run is journaled under its own id with
  `parent_run_id`, its own lease and heartbeat, and its own intents. The parent's
  `recover` recovers it (dead same-host owner released, otherwise after its lease
  expires; a live child lease makes the parent's `recover` raise `WorkflowError` without
  failing the run). A child paused `effect_unknown` pauses the parent with
  `"part": "subworkflow"`, the child's `run_id` and `node_id` `<node>/<child node>`;
  `WorkflowEngine.resolve_effect` on the parent journals the decision in the child's run,
  and the child continues when the parent is recovered.
- **Module keys.** `ExecutionContext.idempotency_key()` returns the part's key inside a
  branch or item. `nexura.http.request` sends the step's key as `Idempotency-Key` on
  writes without an explicit key and then retries POST/PATCH (`retries`, at most 5, on
  transport errors and 429/5xx, same key every try); without any key a POST/PATCH retry is
  still refused. The email and WhatsApp channel modules use the step's key as the outbox
  de-duplication key; connector writes send it as `Idempotency-Key`
  (`ConnectorModule.SENDS_IDEMPOTENCY_KEY`; `False` for S3, which signs its headers).

Limits: recover top-level runs (`parent_run_id` is `None`); `list_recoverable()` also lists
child runs, which their parent recovers. A part in doubt is reported one at a time. The
effects REST route lists and resolves a child's effects under the child's run id.

## Workflows on Postgres (S8)

`nexura_postgres.PostgresWorkflowStore` (package `nexura-postgres`) is the workflow store
for several hosts sharing one database. It has the same protocol as `SQLiteWorkflowStore`,
so everything in the S6 and S7 sections works unchanged on it:

- **Checkpoints and revisions.** The latest checkpoint of each run, saved with a revision
  compare-and-swap when the checkpoint carries a revision (`WorkflowConflictError`
  otherwise), last writer wins when it does not. `create()` is create-if-absent (one winner
  among concurrent starts with the same id).
- **History.** Every saved checkpoint is appended to `nexura_workflow_checkpoint_history`,
  for `WorkflowEngine.history`, replay and `fork`.
- **Approvals.** `claim_approval` moves a `paused` run to `running` once; an approval past
  its `expires_at` is never claimed. Expiry is judged by the database clock.
- **Listing.** `list_children(parent_id)` (natural child order) and
  `list_incomplete(limit=100, *, workspace_id=None)`: top-level runs whose latest
  checkpoint is `running`, oldest first, the candidates for `recover()`. The memory and
  SQLite stores have the same method.
- **Journal on the same pool.** `await store.open_execution_store()` (or the
  `store.execution_store` property) is a `PostgresExecutionStore` on the store's own pool,
  so the run lease, intents and effects live in the same database. Its `close()` does
  nothing; `store.close()` closes the pool.
- **Transactions.** Every write is one transaction under a per-run advisory lock, with the
  row read `FOR UPDATE`; times come from `clock_timestamp()`.
- **Migrations.** Tables are created on first use (or `await store.init_db()`) by
  versioned migrations recorded in `nexura_workflow_migrations`, under an advisory lock
  so several processes can start together. A database migrated by a newer
  `nexura-postgres` is refused with `WorkflowError`.
- **Workspaces.** Without `workspace_id`, one store serves every workspace and
  `WorkflowEngine` checks each run's owner, as with SQLite.
  `PostgresWorkflowStore(dsn, workspace_id="team-a")` scopes the store: runs of other
  workspaces are invisible to it and cannot be written or claimed through it.
- **Pools.** Give a DSN (a pool is created on first use, `max_pool_size=10`) or an asyncpg
  `pool=` you own. A pool belongs to one event loop: a pool the store created is replaced
  when the store is used from another loop, and opened again after `close()`.

```python notest
import asyncio

from nexura_postgres import PostgresWorkflowStore
from nexura_runtime.workflow import ModuleRegistry, WorkflowEngine, WorkflowNode, WorkflowSpec


async def main() -> None:
    store = PostgresWorkflowStore("postgresql://nexura:secret@db.internal:5432/nexura")
    registry = ModuleRegistry()
    registry.register("note", lambda state: {"noted": True}, effect="pure")
    spec = WorkflowSpec(
        name="hello", entry_node="a", nodes={"a": WorkflowNode(id="a", handler="note")},
        terminal_nodes=["a"],
    )
    try:
        engine = WorkflowEngine(spec, registry, store=store, workspace_id="team-a")
        result = await engine.run({}, workflow_run_id="hello-1")
        print(result.status, await store.list_incomplete())
        # After a crash, any host recovers the run once its lease has expired:
        # await engine.recover("hello-1")
    finally:
        await store.close()


asyncio.run(main())
```

**The service.** `create_api_app(workflow_state_url="postgresql://...")`,
`nexura-runtime serve --workflow-state-url postgresql://...` (also `nexura-runtime worker`),
or the environment variable `NEXURA_WORKFLOW_STATE_URL` keep workflow checkpoints, their
history and the workflow runs' journal in Postgres. Unset (the default), they stay in the
SQLite database given by `--db`. The tables are migrated when the app starts. Unless
`execution_store=` is passed, the effects API reads the same Postgres journal. Studio run
records, triggers, sessions and the other service tables stay in SQLite. A URL that is not
`postgresql://` is refused at startup (exit code 2 from the CLI) without echoing it, and a
missing `nexura-postgres` package raises `MissingDependencyError` naming
`pip install nexura-postgres`. An explicit empty `workflow_state_url=""` keeps SQLite even
when the environment variable is set; `workflow_state_url` together with your own `store=`
raises `ValueError` (give that `StudioStore` its `checkpoint_store=` instead).

Tests: one contract suite runs against memory, SQLite and Postgres
(`tests/workflows/test_workflow_store_contract.py`), and the workflow kill rows in
`tests/durability` run on Postgres when `NEXURA_TEST_POSTGRES_URI` names a disposable
database.

Limits: the service's startup sweep and Studio run records are still SQLite-only (S9),
so two service hosts sharing one Postgres workflow state are not yet supported; the
trigger worker keeps its SQLite job store (S10b).

## See also

- [Durable runs today](durable-runs.md)
- [Workflows today](workflows.md)
- [Human approval](approvals.md)
- [API reference](api-reference.md)
- [Documentation index](README.md)
