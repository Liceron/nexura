"""A6 S6: WorkflowEngine on the execution journal (leases, intents, recover, resolve).

Crashes are simulated in-process with a ``BaseException`` the engine does not catch (the
run's lease is given back on the way out, as when a process exits and a restart on the
same host takes over); the real-process kill rows are in ``tests/durability``. Every
effect is a local fake counter.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
from nexura_agents.contracts import BaseModule, ExecutionContext, ModuleManifest, ModuleResult
from nexura_agents.contracts.execution import (
    Applied,
    EffectClass,
    EffectResolution,
    EffectStatus,
    ExecutionId,
    LeaseLostError,
    NotApplied,
    StepKey,
    Unknown,
    default_idempotency_key,
)
from nexura_agents.errors import (
    ApprovalError,
    WorkflowConflictError,
    WorkflowError,
    WorkflowTerminalError,
    WorkflowValidationError,
)
from nexura_agents.execution_store import EffectAlreadyResolvedError, MemoryExecutionStore

from nexura_runtime.workflow import (
    MemoryWorkflowStore,
    ModuleRegistry,
    SQLiteWorkflowStore,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
    step_fencing_token,
    step_idempotency_key,
)


class Crash(BaseException):
    """Stands in for the process dying: nothing in the engine catches it."""


def spec(gated: bool = False, terminal: str = "b") -> WorkflowSpec:
    return WorkflowSpec(
        name="s6",
        entry_node="a",
        nodes={
            "a": WorkflowNode(id="a", handler="note", default_transition="b"),
            "b": WorkflowNode(id="b", handler="pay", requires_approval=gated),
        },
        terminal_nodes=[terminal],
    )


class Ledger:
    """The external system's own record of what was applied."""

    def __init__(self) -> None:
        self.applied: dict[str, int] = {}
        self.keys: list[str | None] = []
        self.crash_in: set[str] = set()

    def effect(self, name: str) -> None:
        self.applied[name] = self.applied.get(name, 0) + 1
        self.keys.append(step_idempotency_key())
        if name in self.crash_in:
            self.crash_in.discard(name)
            raise Crash(name)


def registry(ledger: Ledger, pay_effect: Any = None, reconcile: Any = None) -> ModuleRegistry:
    reg = ModuleRegistry()

    @reg.register_handler("note", effect="pure")
    def note(state: dict[str, Any]) -> dict[str, Any]:
        ledger.effect("note")
        return {"noted": True}

    def pay(state: dict[str, Any]) -> dict[str, Any]:
        ledger.effect("pay")
        return {"paid": True}

    reg.register("pay", pay, effect=pay_effect, reconcile=reconcile)
    return reg


async def snapshot(store: Any, run_id: str) -> Any:
    journal = await store.open_execution_store()
    return await journal.get(ExecutionId("default", run_id))


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Any:
    if request.param == "memory":
        yield MemoryWorkflowStore()
        return
    sqlite = SQLiteWorkflowStore(tmp_path / "wf.sqlite3")
    yield sqlite
    sqlite.close_sync()


# ---------------------------------------------------------------- journal of a normal run


async def test_a_run_is_journaled_with_lease_intents_and_outcomes(store: Any) -> None:
    ledger = Ledger()
    engine = WorkflowEngine(spec(), registry(ledger), store=store)
    result = await engine.run({}, workflow_run_id="r1")
    assert result.status == "completed"

    snap = await snapshot(store, "r1")
    assert snap.run.kind == "workflow"
    assert snap.run.definition_digest == spec().canonical_hash()
    assert snap.run.status == "completed" and snap.run.lease_owner is None
    # "note" is declared pure: outcome only. "pay" is undeclared: non_idempotent intent.
    assert snap.steps["a#1"].intent is None and snap.steps["a#1"].outcome.status == "completed"
    pay = snap.steps["b#2"]
    assert pay.intent.effect_class == EffectClass.NON_IDEMPOTENT
    assert pay.intent.idempotency_key == default_idempotency_key(
        ExecutionId("default", "r1"), StepKey("b#2")
    )
    (effect,) = snap.effects.values()
    assert effect.status == EffectStatus.CONFIRMED
    assert snap.run.fencing_token == 1
    # Handlers saw the step's key; function handlers read it from step_idempotency_key().
    assert ledger.keys == [
        default_idempotency_key(ExecutionId("default", "r1"), StepKey("a#1")),
        pay.intent.idempotency_key,
    ]


async def test_checkpoint_saves_are_fenced_by_revision(store: Any) -> None:
    engine = WorkflowEngine(spec(), registry(Ledger()), store=store)
    await engine.run({}, workflow_run_id="r2")
    cp = await store.get("r2")
    stale = await store.get("r2")
    assert cp.revision == stale.revision and cp.revision >= 3
    await store.save(cp)  # read -> save: accepted, and the object follows the store
    assert cp.revision == stale.revision + 1
    await store.save(cp)
    with pytest.raises(WorkflowConflictError):
        await store.save(stale)
    stale.revision = None  # an unfenced save keeps the old last-writer-wins behaviour
    await store.save(stale)


async def test_register_handler_effect_validation() -> None:
    reg = ModuleRegistry()
    with pytest.raises(WorkflowValidationError):
        reg.register("x", lambda s: {}, effect="sometimes")
    with pytest.raises(WorkflowValidationError):
        reg.register("x", lambda s: {}, effect="reconcilable")
    with pytest.raises(WorkflowValidationError):
        reg.register("x", lambda s: {}, effect="pure", reconcile=lambda s, i: Unknown())
    reg.register("x", lambda s: {}, effect="idempotent_write")  # manifest spelling
    assert reg.declared_effect("x") == (EffectClass.IDEMPOTENT, None)
    reg.register("x", lambda s: {})
    assert reg.declared_effect("x") is None


async def test_module_nodes_get_ctx_idempotency_key_and_fencing_token() -> None:
    seen: list[tuple[str | None, int | None]] = []

    class Charge(BaseModule):
        manifest = ModuleManifest(
            module_id="pay.charge",
            version="1.0.0",
            kind="tool",
            title="Charge",
            description="Fake charge.",
            effect_policy="idempotent_write",
        )

        async def invoke(self, ctx: ExecutionContext, inputs: Any, cfg: Any) -> ModuleResult:
            seen.append((ctx.idempotency_key(), ctx.fencing_token))
            return ModuleResult(status="success", outputs={"charged": True})

    reg = ModuleRegistry()
    reg.register("charge", Charge())
    one = WorkflowSpec(
        name="one", entry_node="c", nodes={"c": WorkflowNode(id="c", handler="charge")}
    )
    store = MemoryWorkflowStore()
    await WorkflowEngine(one, reg, store=store).run({}, workflow_run_id="m1")
    snap = await snapshot(store, "m1")
    assert snap.steps["c#1"].intent.effect_class == EffectClass.IDEMPOTENT
    assert seen == [(snap.steps["c#1"].intent.idempotency_key, 1)]
    # Without a store there is no lease, but the key is still derived from the step.
    seen.clear()
    await WorkflowEngine(one, reg).run({}, workflow_run_id="m2")
    assert seen == [(default_idempotency_key(ExecutionId("default", "m2"), "c#1"), None)]


async def test_mapped_items_get_distinct_keys() -> None:
    keys: list[str | None] = []
    reg = ModuleRegistry()

    @reg.register_handler("each", effect="idempotent")
    def each(state: dict[str, Any]) -> dict[str, Any]:
        keys.append(step_idempotency_key())
        return {}

    mapped = WorkflowSpec(
        name="map",
        entry_node="m",
        nodes={"m": WorkflowNode(id="m", handler="each", map_over="items")},
    )
    await WorkflowEngine(mapped, reg, store=MemoryWorkflowStore()).run({"items": [1, 2, 3]})
    assert len(set(keys)) == 3 and None not in keys


# ---------------------------------------------------------------- recover after a crash


async def test_crash_mid_non_idempotent_node_pauses_effect_unknown(store: Any) -> None:
    ledger = Ledger()
    ledger.crash_in.add("pay")
    engine = WorkflowEngine(spec(), registry(ledger), store=store)
    with pytest.raises(Crash):
        await engine.run({}, workflow_run_id="k3")
    assert (await store.get("k3")).status == "running"

    paused = await WorkflowEngine(spec(), registry(ledger), store=store).recover("k3")
    assert paused.status == "paused" and paused.pause_reason == "effect_unknown"
    assert paused.pending_effect["node_id"] == "b"
    assert paused.pending_effect["effect_class"] == "non_idempotent"
    assert ledger.applied == {"note": 1, "pay": 1}  # not repeated
    cp = await store.get("k3")
    assert cp.pause_reason == "effect_unknown" and cp.pending_effect == paused.pending_effect
    snap = await snapshot(store, "k3")
    assert snap.run.status == "paused" and snap.run.state["pause_reason"] == "effect_unknown"
    assert snap.effects[paused.pending_effect["effect_id"]].status == EffectStatus.INDETERMINATE

    # Recovering again changes nothing until a person resolves it.
    again = await WorkflowEngine(spec(), registry(ledger), store=store).recover("k3")
    assert again.status == "paused" and ledger.applied["pay"] == 1


async def test_resolve_confirmed_continues_with_the_result_without_rerunning() -> None:
    store = MemoryWorkflowStore()
    ledger = Ledger()
    ledger.crash_in.add("pay")
    with pytest.raises(Crash):
        await WorkflowEngine(spec(), registry(ledger), store=store).run({}, workflow_run_id="c1")
    engine = WorkflowEngine(spec(), registry(ledger), store=store)
    await engine.recover("c1")
    done = await engine.resolve_effect(
        "c1", "confirmed", actor="ops@example.test", result={"paid": "seen on the ledger"}
    )
    assert done.status == "completed" and done.state["paid"] == "seen on the ledger"
    assert ledger.applied["pay"] == 1
    snap = await snapshot(store, "c1")
    (effect,) = snap.effects.values()
    assert effect.resolution == EffectResolution.CONFIRMED
    assert effect.resolved_by == "ops@example.test"
    with pytest.raises(WorkflowTerminalError):
        await engine.recover("c1")


async def test_resolve_not_applied_redispatches_with_a_new_key() -> None:
    store = MemoryWorkflowStore()
    ledger = Ledger()
    ledger.crash_in.add("pay")
    with pytest.raises(Crash):
        await WorkflowEngine(spec(), registry(ledger), store=store).run({}, workflow_run_id="n1")
    engine = WorkflowEngine(spec(), registry(ledger), store=store)
    paused = await engine.recover("n1")
    done = await engine.resolve_effect("n1", "not_applied", actor="ops")
    assert done.status == "completed" and ledger.applied["pay"] == 2
    first, second = ledger.keys[1], ledger.keys[2]
    assert first != second
    assert second == default_idempotency_key(ExecutionId("default", "n1"), "b#2.2")
    snap = await snapshot(store, "n1")
    assert snap.effects[paused.pending_effect["effect_id"]].resolution == "not_applied"
    with pytest.raises(WorkflowTerminalError):
        await engine.resolve_effect("n1", "confirmed", actor="late")


async def test_effect_resolved_elsewhere_lets_recover_continue_and_is_resolved_once() -> None:
    store = MemoryWorkflowStore()
    ledger = Ledger()
    ledger.crash_in.add("pay")
    with pytest.raises(Crash):
        await WorkflowEngine(spec(), registry(ledger), store=store).run({}, workflow_run_id="e1")
    engine = WorkflowEngine(spec(), registry(ledger), store=store)
    paused = await engine.recover("e1")
    effect_id = paused.pending_effect["effect_id"]
    # As the effects REST route does: claim, resolve (releases the paused run's lease).
    journal = await store.open_execution_store()
    eid = ExecutionId("default", "e1")
    lease = await journal.claim(eid, "api", 30, definition_digest=spec().canonical_hash())
    await journal.resolve_effect(
        lease, effect_id, EffectResolution.CONFIRMED, actor="api-user", result={"paid": "ok"}
    )
    with pytest.raises(EffectAlreadyResolvedError):
        await engine.resolve_effect("e1", "not_applied", actor="someone-else")
    done = await engine.recover("e1")
    assert done.status == "completed" and done.state["paid"] == "ok"
    assert ledger.applied["pay"] == 1


async def test_not_applied_on_a_gated_node_needs_a_fresh_approval() -> None:
    store = MemoryWorkflowStore()
    ledger = Ledger()
    ledger.crash_in.add("pay")
    first = await WorkflowEngine(spec(gated=True), registry(ledger), store=store).run(
        {}, workflow_run_id="g1"
    )
    engine = WorkflowEngine(spec(gated=True), registry(ledger), store=store)
    with pytest.raises(Crash):
        await engine.resume("g1", first.pending_approval.approval_id)
    paused = await engine.recover("g1")
    assert paused.pause_reason == "effect_unknown"
    assert paused.pending_effect["approval_id"] == first.pending_approval.approval_id
    regated = await engine.resolve_effect("g1", "not_applied", actor="ops")
    assert regated.status == "paused" and regated.pending_approval is not None
    assert regated.pending_approval.approval_id != first.pending_approval.approval_id
    assert ledger.applied["pay"] == 1
    with pytest.raises(ApprovalError):  # the consumed approval cannot send it again
        await engine.resume("g1", first.pending_approval.approval_id)
    done = await engine.resume("g1", regated.pending_approval.approval_id)
    assert done.status == "completed" and ledger.applied["pay"] == 2
    snap = await snapshot(store, "g1")
    consumed = [c["approval_id"] for c in snap.run.state["consumed_approvals"]]
    assert consumed == [first.pending_approval.approval_id, regated.pending_approval.approval_id]


async def test_resolve_failed_fails_the_run() -> None:
    store = MemoryWorkflowStore()
    ledger = Ledger()
    ledger.crash_in.add("pay")
    with pytest.raises(Crash):
        await WorkflowEngine(spec(), registry(ledger), store=store).run({}, workflow_run_id="f1")
    engine = WorkflowEngine(spec(), registry(ledger), store=store)
    await engine.recover("f1")
    failed = await engine.resolve_effect("f1", EffectResolution.FAILED, actor="ops")
    assert failed.status == "failed" and "resolved as failed" in (failed.error or "")
    assert (await snapshot(store, "f1")).run.status == "failed"


async def test_on_uncertain_fail() -> None:
    store = MemoryWorkflowStore()
    ledger = Ledger()
    ledger.crash_in.add("pay")
    with pytest.raises(Crash):
        await WorkflowEngine(spec(), registry(ledger), store=store).run({}, workflow_run_id="u1")
    result = await WorkflowEngine(spec(), registry(ledger), store=store).recover(
        "u1", on_uncertain="fail"
    )
    assert result.status == "failed" and ledger.applied["pay"] == 1
    snap = await snapshot(store, "u1")
    assert snap.run.status == "failed"
    assert [e.status for e in snap.effects.values()] == [EffectStatus.INDETERMINATE]


@pytest.mark.parametrize("effect", ["pure", "idempotent"])
async def test_pure_and_idempotent_nodes_rerun_with_the_same_key(effect: str) -> None:
    store = MemoryWorkflowStore()
    ledger = Ledger()
    ledger.crash_in.add("pay")
    with pytest.raises(Crash):
        await WorkflowEngine(spec(), registry(ledger, effect), store=store).run(
            {}, workflow_run_id="i1"
        )
    done = await WorkflowEngine(spec(), registry(ledger, effect), store=store).recover("i1")
    assert done.status == "completed" and ledger.applied["pay"] == 2
    assert ledger.keys[1] == ledger.keys[2]  # the same idempotency key both times


@pytest.mark.parametrize(
    ("answer", "status", "pay_runs"),
    [
        (Applied({"paid": "found"}), "completed", 1),
        (NotApplied(), "completed", 2),
        (Unknown(), "paused", 1),
    ],
)
async def test_reconcilable_nodes_are_probed(answer: Any, status: str, pay_runs: int) -> None:
    store = MemoryWorkflowStore()
    ledger = Ledger()
    ledger.crash_in.add("pay")
    probes: list[str] = []

    def probe(state: dict[str, Any], intent: Any) -> Any:
        probes.append(intent.idempotency_key)
        return answer

    reg = registry(ledger, "reconcilable", probe)
    with pytest.raises(Crash):
        await WorkflowEngine(spec(), reg, store=store).run({}, workflow_run_id="p1")
    result = await WorkflowEngine(spec(), reg, store=store).recover("p1")
    assert result.status == status and ledger.applied["pay"] == pay_runs
    assert probes == [ledger.keys[1]]
    if isinstance(answer, Applied):
        assert result.state["paid"] == "found"
    if isinstance(answer, NotApplied):
        assert ledger.keys[1] == ledger.keys[2]  # same key
    if isinstance(answer, Unknown):
        assert result.pause_reason == "effect_unknown"


async def test_crash_after_outcome_commit_before_checkpoint_save_does_not_rerun() -> None:
    class LoseSave(MemoryWorkflowStore):
        lose = True

        async def save(self, checkpoint: Any) -> None:
            if self.lose and checkpoint.current_node_id is None:
                self.lose = False
                raise Crash("after the journal outcome, before the checkpoint")
            await super().save(checkpoint)

    store = LoseSave()
    ledger = Ledger()
    with pytest.raises(Crash):
        await WorkflowEngine(spec(), registry(ledger), store=store).run({}, workflow_run_id="o1")
    snap = await snapshot(store, "o1")
    assert snap.run.status == "completed"  # the journal finished; the checkpoint did not
    assert (await store.get("o1")).status == "running"
    done = await WorkflowEngine(spec(), registry(ledger), store=store).recover("o1")
    assert done.status == "completed" and done.state["paid"] is True
    assert ledger.applied == {"note": 1, "pay": 1}


async def test_crash_after_node_commit_continues_from_the_cursor(store: Any) -> None:
    ledger = Ledger()
    ledger.crash_in.add("note")  # pure: re-run is allowed
    with pytest.raises(Crash):
        await WorkflowEngine(spec(), registry(ledger), store=store).run({}, workflow_run_id="k5")
    done = await WorkflowEngine(spec(), registry(ledger), store=store).recover("k5")
    assert done.status == "completed"
    assert ledger.applied == {"note": 2, "pay": 1}


# ---------------------------------------------------------------- approvals across a crash


class CrashAfterClaim(MemoryWorkflowStore):
    crash = True

    async def claim_approval(self, workflow_run_id: str, approval_id: str) -> bool:
        claimed = await super().claim_approval(workflow_run_id, approval_id)
        if claimed and self.crash:
            self.crash = False
            raise Crash("claimed, nothing dispatched")
        return claimed


async def test_approval_claimed_then_crash_dispatches_once_under_that_approval() -> None:
    store = CrashAfterClaim()
    ledger = Ledger()
    paused = await WorkflowEngine(spec(gated=True), registry(ledger), store=store).run(
        {}, workflow_run_id="k8"
    )
    approval_id = paused.pending_approval.approval_id
    with pytest.raises(Crash):
        await WorkflowEngine(spec(gated=True), registry(ledger), store=store).resume(
            "k8", approval_id
        )
    assert ledger.applied.get("pay", 0) == 0
    done = await WorkflowEngine(spec(gated=True), registry(ledger), store=store).recover("k8")
    assert done.status == "completed" and ledger.applied["pay"] == 1
    snap = await snapshot(store, "k8")
    assert snap.steps["b#2"].intent.approval_id == approval_id
    assert [c["approval_id"] for c in snap.run.state["consumed_approvals"]] == [approval_id]


async def test_approval_claimed_then_crash_asks_again_once_expired() -> None:
    store = CrashAfterClaim()
    ledger = Ledger()
    engine = WorkflowEngine(spec(gated=True), registry(ledger), store=store, approval_ttl=0.2)
    paused = await engine.run({}, workflow_run_id="k8x")
    with pytest.raises(Crash):
        await engine.resume("k8x", paused.pending_approval.approval_id)
    await asyncio.sleep(0.3)
    again = await engine.recover("k8x")
    assert again.status == "paused" and again.pending_approval is not None
    assert again.pending_approval.approval_id != paused.pending_approval.approval_id
    assert ledger.applied.get("pay", 0) == 0


# ---------------------------------------------------------------- leases and stale owners


async def test_recover_refuses_a_run_with_a_live_lease_and_takes_an_expired_one() -> None:
    now = [time.time()]
    journal = MemoryExecutionStore(clock=lambda: now[0])
    store = MemoryWorkflowStore(execution_store=journal)
    ledger = Ledger()
    ledger.crash_in.add("pay")
    with pytest.raises(Crash):
        await WorkflowEngine(spec(), registry(ledger), store=store).run({}, workflow_run_id="L1")
    eid = ExecutionId("default", "L1")
    lease = await journal.claim(eid, "live-worker", 30, definition_digest=spec().canonical_hash())
    assert lease is not None
    engine = WorkflowEngine(spec(), registry(ledger), store=store)
    with pytest.raises(WorkflowError, match="another worker"):
        await engine.recover("L1")
    now[0] += 31  # the other worker stopped renewing
    paused = await engine.recover("L1")
    assert paused.pause_reason == "effect_unknown"
    assert (await journal.get(eid)).run.fencing_token == 3


async def test_a_stale_owner_cannot_commit_after_takeover() -> None:
    now = [time.time()]
    journal = MemoryExecutionStore(clock=lambda: now[0])
    store = MemoryWorkflowStore(execution_store=journal)
    started, release = asyncio.Event(), asyncio.Event()
    reg = ModuleRegistry()
    tokens: list[int | None] = []

    @reg.register_handler("note", effect="pure")
    async def note(state: dict[str, Any]) -> dict[str, Any]:
        tokens.append(step_fencing_token())
        started.set()
        await release.wait()
        return {"noted": True}

    reg.register("pay", lambda s: {"paid": True})
    stale = asyncio.create_task(
        WorkflowEngine(spec(), reg, store=store).run({}, workflow_run_id="S1")
    )
    await started.wait()
    now[0] += 31
    eid = ExecutionId("default", "S1")
    taken = await journal.claim(eid, "new-owner", 30, definition_digest=spec().canonical_hash())
    assert taken is not None and taken.fencing_token == tokens[0] + 1
    release.set()
    with pytest.raises(LeaseLostError):
        await stale
    cp = await store.get("S1")
    assert cp.current_node_id == "a" and cp.step_count == 0  # nothing written


async def test_recover_needs_a_journal_and_leaves_waiting_runs_alone() -> None:
    class Plain:
        def __init__(self) -> None:
            self.inner = MemoryWorkflowStore()

        async def get(self, run_id: str) -> Any:
            return await self.inner.get(run_id)

        async def save(self, cp: Any) -> None:
            await self.inner.save(cp)

        async def create(self, cp: Any) -> bool:
            return await self.inner.create(cp)

        async def claim_approval(self, run_id: str, approval_id: str) -> bool:
            return await self.inner.claim_approval(run_id, approval_id)

        async def close(self) -> None:
            pass

    with pytest.raises(WorkflowError, match="execution journal"):
        await WorkflowEngine(spec(), registry(Ledger()), store=Plain()).recover("x")

    store = MemoryWorkflowStore()
    engine = WorkflowEngine(spec(gated=True), registry(Ledger()), store=store)
    paused = await engine.run({}, workflow_run_id="w1")
    same = await engine.recover("w1")
    assert same.status == "paused" and same.pending_approval == paused.pending_approval
    done = await engine.resume("w1", paused.pending_approval.approval_id)
    assert done.status == "completed"
    with pytest.raises(WorkflowTerminalError):
        await engine.recover("w1")
