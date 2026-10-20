# Durable execution contract

**Contract v1 (being implemented; today's behaviour per path is in the design report)**

This page describes the target shared contract for SDK agents, workflows and trigger
jobs. S1 adds value types and documentation; S2 adds standalone memory, SQLite
and Postgres execution stores. S3 rebuilds the deprecated `DurableExecutionEngine`
on those stores, and S4 adds effect resolution over REST and the TypeScript client
(see [DurableExecutionEngine on the journal](#durableexecutionengine-on-the-journal-s3)
and [Resolving an unknown effect](#resolving-an-unknown-effect-s4)). The SDK `Runner`,
`WorkflowEngine` and trigger worker do not use the journal yet, so their dispatch and
recovery behavior is unchanged. The A6 design report
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

The definition digest is a workflow's `canonical_hash()`, or the planned
`Agent.definition_digest()`: agent name, instructions hash, tool names, JSON
schemas and effect classes, handoff targets, and output type.

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
the lease every `ttl/3`. Failed renewal cancels the local task, and the runtime
checks `assert_lease()` immediately before each non-pure dispatch.

The store's clock determines expiry: Postgres uses `now()`. SQLite is supported
on one host; multi-host operation requires Postgres. Effects receive
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
importing `nexura_runtime`. `effect_class_from_idempotent` maps today's tool
boolean. Missing declarations map to `non_idempotent`; unknown policy strings
are rejected. Existing manifest defaults and tool APIs are unchanged by S1;
the declarations above describe the target executor integrations.

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
Readers accept versions N and N-1; writers write N. Migrations go through
`storage/migrations.py`, extended to Postgres.

Takeover with a different digest raises `DefinitionMismatchError`. A future
explicit `migrate=` hook is the only bypass. Agents get one release of warnings
before this check becomes mandatory. S1 introduces the error type, without
changing current definition checks.

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
  returns marks it `confirmed` with its output; a handler that raises or times out
  leaves it `indeterminate` (the remote system may have applied it). A missing handler
  is detected before any intent is written.
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

## See also

- [Durable runs today](durable-runs.md)
- [Workflows today](workflows.md)
- [Human approval](approvals.md)
- [API reference](api-reference.md)
- [Documentation index](README.md)
