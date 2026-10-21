"""A6 S7: parallel branches, mapped items and sub-workflow children on the journal.

Each branch (``<node>/<branch>#<step>``) and mapped item (``<node>#<step>[<item>]``) has
its own intent and outcome; a sub-workflow child keeps its own journal with
``parent_run_id``. After a crash, ``recover()`` decides per part (design §2.5): a part whose
outcome was committed is not run again, ``pure``/``idempotent`` parts run again with the
same key, ``reconcilable`` parts are probed and a ``non_idempotent`` part pauses the run
``effect_unknown`` naming it.

Crashes are simulated in-process with a ``BaseException`` the engine does not catch; the
real-process kill rows are in ``tests/durability``. Every effect is a local fake counter.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from nexura_agents.contracts import BaseModule, ExecutionContext, ModuleManifest, ModuleResult
from nexura_agents.contracts.execution import (
    Applied,
    EffectClass,
    EffectStatus,
    ExecutionId,
    NotApplied,
    StepKey,
    Unknown,
    default_idempotency_key,
)
from nexura_agents.errors import WorkflowError, WorkflowTerminalError
from nexura_agents.execution_store import MemoryExecutionStore

from nexura_runtime.workflow import (
    MemoryWorkflowStore,
    ModuleRegistry,
    SQLiteWorkflowStore,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
    step_idempotency_key,
)


class Crash(BaseException):
    """Stands in for the process dying: nothing in the engine catches it."""


class Ledger:
    """The external system's own record: how often each effect really happened."""

    def __init__(self) -> None:
        self.applied: dict[str, int] = {}
        self.keys: dict[str, list[str | None]] = {}
        self.crash_in: set[str] = set()
        self.fail_in: set[str] = set()

    def effect(self, name: str) -> None:
        self.applied[name] = self.applied.get(name, 0) + 1
        self.keys.setdefault(name, []).append(step_idempotency_key())
        if name in self.crash_in:
            self.crash_in.discard(name)
            raise Crash(name)
        if name in self.fail_in:
            self.fail_in.discard(name)
            raise RuntimeError(f"{name} failed once")


def key(run_id: str, step_key: str) -> str:
    return default_idempotency_key(ExecutionId("default", run_id), StepKey(step_key))


async def snapshot(store: Any, run_id: str) -> Any:
    journal = await store.open_execution_store()
    return await journal.get(ExecutionId("default", run_id))


@pytest.fixture(params=["memory", "sqlite", "postgres"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Any:
    if request.param == "memory":
        yield MemoryWorkflowStore()
        return
    if request.param == "postgres":  # A6 S8; needs NEXURA_TEST_POSTGRES_URI
        yield request.getfixturevalue("postgres_workflow_store")
        return
    sqlite = SQLiteWorkflowStore(tmp_path / "wf.sqlite3")
    yield sqlite
    sqlite.close_sync()


# ------------------------------------------------------------------------------ map steps


def map_spec(retry: dict[str, Any] | None = None, gated: bool = False) -> WorkflowSpec:
    return WorkflowSpec(
        name="s7-map",
        entry_node="m",
        nodes={
            "m": WorkflowNode(
                id="m",
                handler="pay_item",
                map_over="items",
                reducers={"paid": "append"},
                max_concurrency=1,
                default_transition="done",
                retry=retry or {},
                requires_approval=gated,
            ),
            "done": WorkflowNode(id="done", handler="finish"),
        },
        terminal_nodes=["done"],
    )


def map_registry(ledger: Ledger, effect: Any = None, reconcile: Any = None) -> ModuleRegistry:
    reg = ModuleRegistry()

    def pay_item(state: dict[str, Any]) -> dict[str, Any]:
        ledger.effect(f"pay[{state['item']}]")
        return {"paid": state["item"]}

    reg.register("pay_item", pay_item, effect=effect, reconcile=reconcile)
    reg.register("finish", lambda state: {"finished": True}, effect="pure")
    return reg


ITEMS = {"items": [10, 11, 12]}


async def test_map_items_get_their_own_intents_outcomes_and_keys(store: Any) -> None:
    ledger = Ledger()
    done = await WorkflowEngine(map_spec(), map_registry(ledger), store=store).run(
        ITEMS, workflow_run_id="m1"
    )
    assert done.status == "completed" and done.state["paid"] == [10, 11, 12]

    snap = await snapshot(store, "m1")
    # The step itself: a container intent of class pure (its parts guard themselves).
    assert snap.steps["m#1"].intent.effect_class == EffectClass.PURE
    assert snap.steps["m#1"].outcome.status == "completed"
    for index, item in enumerate(ITEMS["items"]):
        part = snap.steps[f"m#1[{index}]"]
        assert part.intent.effect_class == EffectClass.NON_IDEMPOTENT
        assert part.intent.idempotency_key == key("m1", f"m#1[{index}]")
        assert part.outcome.output == {"update": {"paid": item}}
        assert ledger.keys[f"pay[{item}]"] == [part.intent.idempotency_key]
    assert {e.status for e in snap.effects.values()} == {EffectStatus.CONFIRMED}
    assert len(snap.effects) == 4  # the container and three items


async def test_crash_mid_item_recovers_per_item_and_pauses_on_the_unknown_one(
    store: Any,
) -> None:
    ledger = Ledger()
    ledger.crash_in.add("pay[11]")
    with pytest.raises(Crash):
        await WorkflowEngine(map_spec(), map_registry(ledger), store=store).run(
            ITEMS, workflow_run_id="m2"
        )
    assert ledger.applied == {"pay[10]": 1, "pay[11]": 1}

    engine = WorkflowEngine(map_spec(), map_registry(ledger), store=store)
    paused = await engine.recover("m2")
    assert paused.status == "paused" and paused.pause_reason == "effect_unknown"
    pending = paused.pending_effect
    assert pending["part"] == "item" and pending["item"] == 1
    assert pending["step_key"] == "m#1[1]" and pending["node_id"] == "m"
    assert pending["run_id"] == "m2" and pending["effect_class"] == "non_idempotent"
    # Item 10 finished before the crash: not run again. Item 12 never started: it ran.
    assert ledger.applied == {"pay[10]": 1, "pay[11]": 1, "pay[12]": 1}
    snap = await snapshot(store, "m2")
    assert snap.effects[pending["effect_id"]].status == EffectStatus.INDETERMINATE
    assert snap.steps["m#1[2]"].outcome is not None  # recorded, so not run again later

    again = await engine.recover("m2")  # nothing changes until a person decides
    assert again.status == "paused" and again.pending_effect == pending
    assert sum(ledger.applied.values()) == 3

    done = await engine.resolve_effect("m2", "confirmed", actor="ops", result={"paid": 11})
    assert done.status == "completed" and done.state["paid"] == [10, 11, 12]
    assert ledger.applied == {"pay[10]": 1, "pay[11]": 1, "pay[12]": 1}
    with pytest.raises(WorkflowTerminalError):
        await engine.recover("m2")


@pytest.mark.parametrize("effect", ["pure", "idempotent"])
async def test_pure_and_idempotent_items_rerun_with_the_same_key(effect: str) -> None:
    store = MemoryWorkflowStore()
    ledger = Ledger()
    ledger.crash_in.add("pay[11]")
    with pytest.raises(Crash):
        await WorkflowEngine(map_spec(), map_registry(ledger, effect), store=store).run(
            ITEMS, workflow_run_id="m3"
        )
    done = await WorkflowEngine(map_spec(), map_registry(ledger, effect), store=store).recover("m3")
    assert done.status == "completed" and done.state["paid"] == [10, 11, 12]
    assert ledger.applied == {"pay[10]": 1, "pay[11]": 2, "pay[12]": 1}
    first, second = ledger.keys["pay[11]"]
    assert first == second == key("m3", "m#1[1]")


@pytest.mark.parametrize(
    ("answer", "status", "runs"),
    [
        (Applied({"paid": "found"}), "completed", 1),
        (NotApplied(), "completed", 2),
        (Unknown(), "paused", 1),
    ],
)
async def test_reconcilable_items_are_probed_one_by_one(
    answer: Any, status: str, runs: int
) -> None:
    store = MemoryWorkflowStore()
    ledger = Ledger()
    ledger.crash_in.add("pay[11]")
    probes: list[tuple[Any, str]] = []

    def probe(state: dict[str, Any], intent: Any) -> Any:
        probes.append((state["item"], intent.idempotency_key))
        return answer

    reg = map_registry(ledger, "reconcilable", probe)
    with pytest.raises(Crash):
        await WorkflowEngine(map_spec(), reg, store=store).run(ITEMS, workflow_run_id="m4")
    result = await WorkflowEngine(map_spec(), reg, store=store).recover("m4")
    assert result.status == status and ledger.applied["pay[11]"] == runs
    assert probes == [(11, key("m4", "m#1[1]"))]  # only the item in doubt is probed
    assert ledger.applied["pay[10]"] == 1
    if isinstance(answer, Applied):
        assert result.state["paid"] == [10, "found", 12]
    if isinstance(answer, NotApplied):
        assert ledger.keys["pay[11]"][0] == ledger.keys["pay[11]"][1]
    if isinstance(answer, Unknown):
        assert result.pending_effect["item"] == 1


async def test_item_resolved_not_applied_is_sent_again_with_a_new_key() -> None:
    store = MemoryWorkflowStore()
    ledger = Ledger()
    ledger.crash_in.add("pay[11]")
    engine = WorkflowEngine(map_spec(), map_registry(ledger), store=store)
    with pytest.raises(Crash):
        await engine.run(ITEMS, workflow_run_id="m5")
    await engine.recover("m5")
    done = await engine.resolve_effect("m5", "not_applied", actor="ops")
    assert done.status == "completed" and done.state["paid"] == [10, 11, 12]
    assert ledger.applied == {"pay[10]": 1, "pay[11]": 2, "pay[12]": 1}
    assert ledger.keys["pay[11]"] == [key("m5", "m#1[1]"), key("m5", "m#1.2[1]")]
    snap = await snapshot(store, "m5")
    assert snap.steps["m#1.2[1]"].outcome.output == {"update": {"paid": 11}}


async def test_item_resolved_failed_fails_the_run_and_on_uncertain_fail() -> None:
    store = MemoryWorkflowStore()
    ledger = Ledger()
    ledger.crash_in.add("pay[11]")
    engine = WorkflowEngine(map_spec(), map_registry(ledger), store=store)
    with pytest.raises(Crash):
        await engine.run(ITEMS, workflow_run_id="m6")
    await engine.recover("m6")
    failed = await engine.resolve_effect("m6", "failed", actor="ops")
    assert failed.status == "failed" and "resolved as failed" in (failed.error or "")
    assert (await snapshot(store, "m6")).run.status == "failed"

    ledger.crash_in.add("pay[11]")
    with pytest.raises(Crash):
        await engine.run(ITEMS, workflow_run_id="m7")
    result = await engine.recover("m7", on_uncertain="fail")
    assert result.status == "failed" and "on_uncertain='fail'" in (result.error or "")
    assert (await store.get("m7")).status == "failed"
    snap = await snapshot(store, "m7")
    assert snap.run.status == "failed"
    assert snap.effects[next(iter(snap.effects))].status == EffectStatus.INDETERMINATE


async def test_an_in_process_retry_keeps_finished_items_and_reruns_the_failed_one() -> None:
    store = MemoryWorkflowStore()
    ledger = Ledger()
    ledger.fail_in.add("pay[11]")
    reg = map_registry(ledger, "idempotent")
    spec = map_spec(retry={"max_attempts": 2, "initial_delay": 0})
    done = await WorkflowEngine(spec, reg, store=store).run(ITEMS, workflow_run_id="m8")
    assert done.status == "completed" and done.state["paid"] == [10, 11, 12]
    # Item 10 completed before item 11 failed: the retry used its recorded outcome.
    assert ledger.applied == {"pay[10]": 1, "pay[11]": 2, "pay[12]": 1}
    assert ledger.keys["pay[11]"][0] == ledger.keys["pay[11]"][1]


# ------------------------------------------------------------------------- parallel steps


def parallel_spec(gated: bool = False) -> WorkflowSpec:
    return WorkflowSpec(
        name="s7-parallel",
        entry_node="p",
        nodes={
            "p": WorkflowNode(
                id="p",
                handler="",
                parallel=["look", "charge", "notify"],
                max_concurrency=1,
                requires_approval=gated,
            )
        },
        terminal_nodes=["p"],
    )


def parallel_registry(ledger: Ledger, charge_effect: Any = None) -> ModuleRegistry:
    reg = ModuleRegistry()

    @reg.register_handler("look", effect="pure")
    def look(state: dict[str, Any]) -> dict[str, Any]:
        ledger.effect("look")
        return {"looked": True}

    def charge(state: dict[str, Any]) -> dict[str, Any]:
        ledger.effect("charge")
        return {"charged": True}

    def notify(state: dict[str, Any]) -> dict[str, Any]:
        ledger.effect("notify")
        return {"notified": True}

    reg.register("charge", charge, effect=charge_effect)
    reg.register("notify", notify)
    return reg


async def test_parallel_branches_are_journaled_one_by_one(store: Any) -> None:
    ledger = Ledger()
    done = await WorkflowEngine(parallel_spec(), parallel_registry(ledger), store=store).run(
        {}, workflow_run_id="p1"
    )
    assert done.status == "completed"
    snap = await snapshot(store, "p1")
    assert snap.steps["p#1"].intent.effect_class == EffectClass.PURE
    # A pure branch records only its outcome; the others an intent with their own key.
    assert snap.steps["p/look#1"].intent is None
    assert snap.steps["p/look#1"].outcome.output == {"update": {"looked": True}}
    for name in ("charge", "notify"):
        intent = snap.steps[f"p/{name}#1"].intent
        assert intent.effect_class == EffectClass.NON_IDEMPOTENT
        assert intent.idempotency_key == key("p1", f"p/{name}#1")
        assert ledger.keys[name] == [intent.idempotency_key]


async def test_crash_in_one_branch_pauses_naming_it_and_keeps_the_others(store: Any) -> None:
    ledger = Ledger()
    ledger.crash_in.add("charge")
    engine = WorkflowEngine(parallel_spec(), parallel_registry(ledger), store=store)
    with pytest.raises(Crash):
        await engine.run({}, workflow_run_id="p2")
    paused = await engine.recover("p2")
    assert paused.status == "paused" and paused.pause_reason == "effect_unknown"
    pending = paused.pending_effect
    assert pending["part"] == "branch" and pending["branch"] == "charge"
    assert pending["step_key"] == "p/charge#1"
    assert ledger.applied == {"look": 1, "charge": 1, "notify": 1}

    done = await engine.resolve_effect("p2", "confirmed", actor="ops", result={"charged": "ok"})
    assert done.status == "completed"
    assert done.state == {"looked": True, "charged": "ok", "notified": True}
    assert ledger.applied == {"look": 1, "charge": 1, "notify": 1}


async def test_idempotent_branch_reruns_with_its_key() -> None:
    store = MemoryWorkflowStore()
    ledger = Ledger()
    ledger.crash_in.add("charge")
    reg = parallel_registry(ledger, "idempotent")
    with pytest.raises(Crash):
        await WorkflowEngine(parallel_spec(), reg, store=store).run({}, workflow_run_id="p3")
    done = await WorkflowEngine(parallel_spec(), reg, store=store).recover("p3")
    assert done.status == "completed"
    assert ledger.applied == {"look": 1, "charge": 2, "notify": 1}
    assert ledger.keys["charge"] == [key("p3", "p/charge#1")] * 2


async def test_not_applied_branch_of_a_gated_step_needs_a_fresh_approval() -> None:
    store = MemoryWorkflowStore()
    ledger = Ledger()
    engine = WorkflowEngine(parallel_spec(gated=True), parallel_registry(ledger), store=store)
    asked = await engine.run({}, workflow_run_id="p4")
    first = asked.pending_approval.approval_id
    ledger.crash_in.add("charge")
    with pytest.raises(Crash):
        await engine.resume("p4", first)
    paused = await engine.recover("p4")
    assert paused.pending_effect["branch"] == "charge"
    assert paused.pending_effect["approval_id"] == first
    regated = await engine.resolve_effect("p4", "not_applied", actor="ops")
    assert regated.status == "paused" and regated.pending_approval is not None
    second = regated.pending_approval.approval_id
    # "notify" never started before the crash: recover ran it under the first approval.
    assert second != first and ledger.applied == {"look": 1, "charge": 1, "notify": 1}

    done = await engine.resume("p4", second)
    assert done.status == "completed"
    assert ledger.applied == {"look": 1, "charge": 2, "notify": 1}
    assert ledger.keys["charge"] == [key("p4", "p/charge#1"), key("p4", "p/charge#1.2")]
    snap = await snapshot(store, "p4")
    assert snap.steps["p/charge#1.2"].intent.approval_id == second
    assert [c["approval_id"] for c in snap.run.state["consumed_approvals"]] == [first, second]


# --------------------------------------------------------------------- sub-workflow children


def child_spec() -> WorkflowSpec:
    return WorkflowSpec(
        name="s7-child",
        entry_node="note",
        nodes={
            "note": WorkflowNode(id="note", handler="note", default_transition="pay"),
            "pay": WorkflowNode(id="pay", handler="pay"),
        },
        terminal_nodes=["pay"],
    )


def parent_spec(map_over: str | None = None) -> WorkflowSpec:
    return WorkflowSpec(
        name="s7-parent",
        entry_node="sub",
        nodes={
            "sub": WorkflowNode(
                id="sub",
                handler="",
                subgraph=child_spec(),
                map_over=map_over,
                max_concurrency=1,
                reducers={"paid": "append"} if map_over else {},
                output_mapping={"paid": "paid"},
            )
        },
        terminal_nodes=["sub"],
    )


def child_registry(ledger: Ledger, pay_effect: Any = None) -> ModuleRegistry:
    reg = ModuleRegistry()

    @reg.register_handler("note", effect="pure")
    def note(state: dict[str, Any]) -> dict[str, Any]:
        ledger.effect(f"note[{state.get('item', '-')}]")
        return {"noted": True}

    def pay(state: dict[str, Any]) -> dict[str, Any]:
        ledger.effect(f"pay[{state.get('item', '-')}]")
        return {"paid": state.get("item", True)}

    reg.register("pay", pay, effect=pay_effect)
    return reg


async def test_a_child_run_is_journaled_with_its_parent(store: Any) -> None:
    ledger = Ledger()
    done = await WorkflowEngine(parent_spec(), child_registry(ledger), store=store).run(
        {}, workflow_run_id="c1"
    )
    assert done.status == "completed" and done.state["paid"] is True
    child = await snapshot(store, "c1/sub#1")
    assert child.run.parent_run_id == "c1" and child.run.kind == "workflow"
    assert child.run.definition_digest == child_spec().canonical_hash()
    assert child.run.status == "completed" and child.run.lease_owner is None
    assert child.steps["pay#2"].intent.effect_class == EffectClass.NON_IDEMPOTENT
    assert ledger.keys["pay[-]"] == [key("c1/sub#1", "pay#2")]
    parent = await snapshot(store, "c1")
    assert parent.run.parent_run_id is None
    assert parent.steps["sub#1"].intent.effect_class == EffectClass.PURE


async def test_crash_in_a_child_pauses_the_parent_and_resolves_through_it(store: Any) -> None:
    ledger = Ledger()
    ledger.crash_in.add("pay[-]")
    engine = WorkflowEngine(parent_spec(), child_registry(ledger), store=store)
    with pytest.raises(Crash):
        await engine.run({}, workflow_run_id="c2")
    paused = await engine.recover("c2")
    assert paused.status == "paused" and paused.pause_reason == "effect_unknown"
    pending = paused.pending_effect
    assert pending["part"] == "subworkflow" and pending["run_id"] == "c2/sub#1"
    assert pending["node_id"] == "sub/pay" and pending["step_key"] == "pay#2"
    child_cp = await store.get("c2/sub#1")
    assert (
        child_cp.status == "paused"
        and child_cp.pending_effect["effect_id"] == (pending["effect_id"])
    )
    assert ledger.applied == {"note[-]": 1, "pay[-]": 1}

    still = await engine.recover("c2")
    assert still.status == "paused" and ledger.applied["pay[-]"] == 1

    done = await engine.resolve_effect("c2", "confirmed", actor="ops", result={"paid": "seen"})
    assert done.status == "completed" and done.state["paid"] == "seen"
    assert ledger.applied == {"note[-]": 1, "pay[-]": 1}
    child = await snapshot(store, "c2/sub#1")
    assert child.run.status == "completed"
    assert child.effects[pending["effect_id"]].resolved_by == "ops"


async def test_idempotent_child_step_reruns_with_its_key_from_the_parent_recover() -> None:
    store = MemoryWorkflowStore()
    ledger = Ledger()
    ledger.crash_in.add("pay[-]")
    reg = child_registry(ledger, "idempotent")
    with pytest.raises(Crash):
        await WorkflowEngine(parent_spec(), reg, store=store).run({}, workflow_run_id="c3")
    done = await WorkflowEngine(parent_spec(), reg, store=store).recover("c3")
    assert done.status == "completed"
    assert ledger.applied == {"note[-]": 1, "pay[-]": 2}
    assert ledger.keys["pay[-]"] == [key("c3/sub#1", "pay#2")] * 2


async def test_mapped_children_recover_one_by_one() -> None:
    store = MemoryWorkflowStore()
    ledger = Ledger()
    ledger.crash_in.add("pay[11]")
    engine = WorkflowEngine(parent_spec("items"), child_registry(ledger), store=store)
    with pytest.raises(Crash):
        await engine.run(ITEMS, workflow_run_id="c4")
    paused = await engine.recover("c4")
    assert paused.pause_reason == "effect_unknown"
    assert paused.pending_effect["run_id"] == "c4/sub#1[1]"
    assert paused.pending_effect["item"] == 1
    # Item 10's child completed before the crash; item 12's child ran during recover.
    assert ledger.applied == {
        "note[10]": 1,
        "pay[10]": 1,
        "note[11]": 1,
        "pay[11]": 1,
        "note[12]": 1,
        "pay[12]": 1,
    }
    done = await engine.resolve_effect("c4", "not_applied", actor="ops")
    assert done.status == "completed" and done.state["paid"] == [10, 11, 12]
    assert ledger.applied["pay[11]"] == 2 and ledger.applied["pay[10]"] == 1
    assert ledger.keys["pay[11]"] == [key("c4/sub#1[1]", "pay#2"), key("c4/sub#1[1]", "pay#2.2")]


async def test_a_live_child_lease_stops_the_parent_recover_without_failing_it() -> None:
    now = [time.time()]
    journal = MemoryExecutionStore(clock=lambda: now[0])
    store = MemoryWorkflowStore(execution_store=journal)
    ledger = Ledger()
    ledger.crash_in.add("pay[-]")
    with pytest.raises(Crash):
        await WorkflowEngine(parent_spec(), child_registry(ledger), store=store).run(
            {}, workflow_run_id="c5"
        )
    child = ExecutionId("default", "c5/sub#1")
    lease = await journal.claim(
        child, "live-worker", 30, definition_digest=child_spec().canonical_hash()
    )
    assert lease is not None
    engine = WorkflowEngine(parent_spec(), child_registry(ledger), store=store)
    with pytest.raises(WorkflowError, match="another worker"):
        await engine.recover("c5")
    assert (await store.get("c5")).status == "running"  # not failed: try again later
    now[0] += 31
    paused = await engine.recover("c5")
    assert paused.pause_reason == "effect_unknown" and ledger.applied["pay[-]"] == 1


# ------------------------------------------------------------- modules see the part's key


async def test_modules_in_branches_and_items_get_the_part_key_from_the_context() -> None:
    seen: list[tuple[str | None, str | None, int | None]] = []

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
            seen.append((ctx.step_key, ctx.idempotency_key(), ctx.fencing_token))
            return ModuleResult(status="success", outputs={})

    reg = ModuleRegistry()
    reg.register("charge", Charge())
    reg.register("other", Charge())
    spec = WorkflowSpec(
        name="mods",
        entry_node="m",
        nodes={
            "m": WorkflowNode(id="m", handler="charge", map_over="items", default_transition="p"),
            "p": WorkflowNode(id="p", handler="", parallel=["charge", "other"]),
        },
        terminal_nodes=["p"],
    )
    store = MemoryWorkflowStore()
    await WorkflowEngine(spec, reg, store=store).run({"items": [1, 2]}, workflow_run_id="k1")
    snap = await snapshot(store, "k1")
    expected = {
        (k, snap.steps[k].intent.idempotency_key, 1)
        for k in ("m#1[0]", "m#1[1]", "p/charge#2", "p/other#2")
    }
    assert set(seen) == expected
    assert all(i == key("k1", k) for k, i, _ in expected)
