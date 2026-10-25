"""Terminal commits and recovery allowances; all external actions are local fakes."""

from __future__ import annotations

import asyncio
import dataclasses
import time
from pathlib import Path
from typing import Any

import pytest
from nexura_agents.contracts.execution import Applied, EffectStatus, ExecutionId, NotApplied
from nexura_agents.errors import WorkflowError

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
    """Abrupt stop after a committed intent and local fake dispatch."""


def spec_for(shape: str, *, prepare: bool = False) -> WorkflowSpec:
    action = WorkflowNode(id="action", handler="pay")
    if shape == "parallel":
        action.handler, action.parallel = "", ["pay"]
    elif shape == "mapped":
        action.map_over = "items"
    elif shape == "nested":
        action.handler, action.subgraph = "", spec_for("plain")
    nodes = {"action": action}
    if prepare:
        nodes["prepare"] = WorkflowNode(
            id="prepare", handler="prepare", default_transition="action"
        )
    return WorkflowSpec(name="round4", entry_node="prepare" if prepare else "action", nodes=nodes)


def open_store(backend: str, path: Path) -> Any:
    return MemoryWorkflowStore() if backend == "memory" else SQLiteWorkflowStore(path)


async def snapshot(store: Any, run_id: str = "r") -> Any:
    return await (await store.open_execution_store()).get(ExecutionId("default", run_id))


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("shape", ["parallel", "mapped", "nested"])
@pytest.mark.parametrize("effect", ["non_idempotent", "reconcilable"])
@pytest.mark.parametrize("resolution", ["confirmed", "not_applied", "failed"])
async def test_step_limit_preserves_settlement_and_forbids_dispatch(
    backend: str, shape: str, effect: str, resolution: str, tmp_path: Path
) -> None:
    calls, probes = [], []

    async def pay(state: Any) -> Any:
        calls.append(step_idempotency_key())
        raise Crash

    def probe(state: Any, intent: Any) -> NotApplied:
        probes.append(intent.step_key)
        return NotApplied()

    registry = ModuleRegistry()
    registry.register("prepare", lambda s: {}, effect="pure")
    registry.register(
        "pay", pay, effect=effect, reconcile=probe if effect == "reconcilable" else None
    )
    spec = spec_for(shape, prepare=True)
    db = tmp_path / "limits.db"
    store = open_store(backend, db)
    try:
        with pytest.raises(Crash):
            await WorkflowEngine(spec, registry, store=store).run(
                {"items": [1]}, workflow_run_id="r"
            )
        limited = WorkflowEngine(spec, registry, store=store, max_steps=1)
        paused = await limited.recover("r")
        assert paused.status == "paused" and paused.pause_reason == "effect_unknown"
        assert paused.error == "Workflow exceeded maximum allowed steps (1 >= 1)."
        target = paused.pending_effect["run_id"] if shape == "nested" else "r"
        action = await snapshot(store, target)
        unsafe = [e for e in action.effects.values() if e.intent.effect_class.value == effect]
        assert len(unsafe) == 1 and unsafe[0].status == EffectStatus.INDETERMINATE
        assert (await snapshot(store)).run.status == "paused"
        assert not probes and len(calls) == 1
        if backend == "sqlite":
            await store.close()
            store = open_store(backend, db)
        # Raising the limit cannot evade the already-deferred failure, even after reopen.
        fresh = WorkflowEngine(spec, registry, store=store, max_steps=100)
        waiting = await fresh.recover("r")
        assert waiting.pause_reason == "effect_unknown" and waiting.error == paused.error
        done = await fresh.resolve_effect(
            "r", resolution, actor="operator", result={"paid": True}, reason="limit settlement"
        )
        assert done.status == "failed" and done.error == paused.error
        assert len(calls) == 1 and not probes
        settled = await snapshot(store, target)
        recorded = settled.effects[unsafe[0].effect_id]
        assert recorded.status == EffectStatus.RESOLVED and recorded.resolved_by == "operator"
        assert (await snapshot(store)).run.lease_owner is None
    finally:
        await store.close()


@pytest.mark.parametrize("shape", ["parallel", "mapped", "nested"])
async def test_deferred_limit_waits_for_all_payments(shape: str) -> None:
    calls = []
    dispatched = asyncio.Event()

    async def pay(state: Any) -> Any:
        calls.append(step_idempotency_key())
        if len(calls) == 2:
            dispatched.set()
        await dispatched.wait()
        raise Crash

    registry = ModuleRegistry()
    registry.register("prepare", lambda s: {}, effect="pure")
    registry.register("pay", pay)
    registry.register("other", pay)
    spec = spec_for(shape, prepare=True)
    if shape == "parallel":
        spec.nodes["action"].parallel.append("other")
    elif shape == "nested":
        child = spec.nodes["action"].subgraph
        assert child is not None
        child.nodes["action"].handler, child.nodes["action"].parallel = "", ["pay", "other"]
    store = MemoryWorkflowStore()
    # A process crash can leave multiple committed dispatches; TaskGroup starts both here.
    with pytest.raises(Crash):
        await WorkflowEngine(spec, registry, store=store).run(
            {"items": [1, 2]}, workflow_run_id="r"
        )
    assert len(calls) == 2
    engine = WorkflowEngine(spec, registry, store=store, max_steps=1)
    paused = await engine.recover("r")
    first = await engine.resolve_effect("r", "failed", actor="operator")
    assert first.pause_reason == "effect_unknown" and first.error == paused.error
    last = await engine.resolve_effect("r", "not_applied", actor="operator")
    assert last.status == "failed" and last.error == paused.error
    assert len(calls) == 2


@pytest.mark.parametrize("shape", ["parallel", "mapped", "nested"])
@pytest.mark.parametrize("terminal_status", ["completed", "failed", "rejected"])
async def test_terminal_commit_boundary_cannot_drop_a_dispatched_action(
    shape: str, terminal_status: str
) -> None:
    calls = []

    async def pay(state: Any) -> Any:
        calls.append("charged")
        raise Crash

    registry = ModuleRegistry()
    registry.register("pay", pay)
    spec = spec_for(shape)
    store = MemoryWorkflowStore()
    with pytest.raises(Crash):
        await WorkflowEngine(spec, registry, store=store).run({"items": [1]}, workflow_run_id="r")

    class TerminalExit(WorkflowEngine):
        async def _recover_taken(self, journal: Any, run_id: str, policy: str) -> Any:
            # Fault injection at the common save boundary, independent of a specific limit.
            cp = await self.store.get(run_id)
            journal.cp_revision = cp.revision
            terminal = dataclasses.replace(
                cp,
                status=terminal_status,
                current_node_id=None if terminal_status == "completed" else cp.current_node_id,
                error="resource allowance exhausted",
            )
            await self._save(terminal)
            pytest.fail("terminal save must unwind before further execution")

    paused = await TerminalExit(spec, registry, store=store).recover("r")
    assert paused.pause_reason == "effect_unknown" and calls == ["charged"]
    assert (await snapshot(store)).run.status == "paused"
    done = await WorkflowEngine(spec, registry, store=store).resolve_effect(
        "r", "confirmed", actor="operator", result={"paid": True}
    )
    assert done.status == "failed" and done.error == "resource allowance exhausted"
    assert calls == ["charged"]


@pytest.mark.parametrize("shape", ["plain", "parallel", "mapped", "nested"])
@pytest.mark.parametrize("synchronous", [False, True])
async def test_recovery_probe_and_children_share_one_deadline(
    shape: str, synchronous: bool
) -> None:
    calls, probes = [], []

    async def pay(state: Any) -> Any:
        calls.append(step_idempotency_key())
        raise Crash

    async def async_probe(state: Any, intent: Any) -> Applied:
        probes.append(intent.step_key)
        await asyncio.sleep(0.12)
        return Applied({"paid": True})

    def sync_probe(state: Any, intent: Any) -> Applied:
        probes.append(intent.step_key)
        time.sleep(0.12)
        return Applied({"paid": True})

    registry = ModuleRegistry()
    registry.register(
        "pay", pay, effect="reconcilable", reconcile=sync_probe if synchronous else async_probe
    )
    spec, store = spec_for(shape), MemoryWorkflowStore()
    with pytest.raises(Crash):
        await WorkflowEngine(spec, registry, store=store).run({"items": [1]}, workflow_run_id="r")
    started = time.monotonic()
    paused = await WorkflowEngine(spec, registry, store=store, timeout=0.03).recover("r")
    assert time.monotonic() - started < 0.10
    assert paused.pause_reason == "effect_unknown" and len(calls) == len(probes) == 1
    target = paused.pending_effect.get("run_id", "r")
    unknown = (await snapshot(store, target)).effects[paused.pending_effect["effect_id"]]
    assert unknown.status == EffectStatus.INDETERMINATE
    # The expired invocation does not leak its deadline into a later explicit recovery.
    done = await WorkflowEngine(spec, registry, store=store).resolve_effect(
        "r", "confirmed", actor="operator", result={"paid": True}
    )
    assert done.status == "completed" and len(calls) == len(probes) == 1


@pytest.mark.parametrize("shape", ["plain", "parallel", "mapped", "nested"])
async def test_recovery_resends_include_first_decision_in_allowance(shape: str) -> None:
    calls, probes = [], []
    crashing = True

    async def pay(state: Any) -> Any:
        calls.append(step_idempotency_key())
        if crashing:
            raise Crash
        raise ConnectionError("local fake not applied")

    def probe(state: Any, intent: Any) -> NotApplied:
        probes.append(intent.step_key)
        return NotApplied()

    registry = ModuleRegistry()
    registry.register("pay", pay, effect="reconcilable", reconcile=probe)
    spec, store = spec_for(shape), MemoryWorkflowStore()
    with pytest.raises(Crash):
        await WorkflowEngine(spec, registry, store=store).run({"items": [1]}, workflow_run_id="r")
    original_key = calls.pop()
    crashing = False
    engine = WorkflowEngine(spec, registry, store=store)
    paused = await engine.recover("r")
    assert paused.pause_reason == "effect_unknown"
    assert calls == [original_key] * 3 and len(probes) == 4
    target = paused.pending_effect.get("run_id", "r")
    unknown = (await snapshot(store, target)).effects[paused.pending_effect["effect_id"]]
    assert unknown.status == EffectStatus.INDETERMINATE
    done = await engine.resolve_effect("r", "confirmed", actor="operator", result={"paid": True})
    assert done.status == "completed" and calls == [original_key] * 3


async def test_applied_recovery_probe_does_not_restart_deadline_for_next_action() -> None:
    calls = []
    crashing = True

    async def pay(state: Any) -> Any:
        calls.append("first")
        if crashing:
            raise Crash
        return {"paid": True}

    # Generous budget for loaded hosts: the probe uses half of the 2 s deadline, so the next
    # action (1.5 s) can start (1 s of slack) but cannot finish before the shared deadline.
    # A deadline restarted after the probe would give it 2 s and let it complete.
    async def probe(state: Any, intent: Any) -> Applied:
        await asyncio.sleep(1.0)
        return Applied({"paid": True})

    async def next_action(state: Any) -> Any:
        calls.append("next")
        await asyncio.sleep(1.5)
        return {"next_paid": True}

    registry = ModuleRegistry()
    registry.register("pay", pay, effect="reconcilable", reconcile=probe)
    registry.register("next", next_action)
    spec = spec_for("plain")
    spec.nodes["action"].default_transition = "next"
    spec.nodes["next"] = WorkflowNode(id="next", handler="next")
    store = MemoryWorkflowStore()
    with pytest.raises(Crash):
        await WorkflowEngine(spec, registry, store=store).run({}, workflow_run_id="r")
    crashing = False
    started = time.monotonic()
    paused = await WorkflowEngine(spec, registry, store=store, timeout=2.0).recover("r")
    assert time.monotonic() - started < 3.5
    assert paused.pause_reason == "effect_unknown" and calls == ["first", "next"]
    done = await WorkflowEngine(spec, registry, store=store).resolve_effect(
        "r", "confirmed", actor="operator", result={"next_paid": True}
    )
    assert done.status == "completed" and done.state == {"paid": True, "next_paid": True}
    assert calls == ["first", "next"]


async def test_recovery_discards_applied_probe_returned_after_cancellation() -> None:
    calls = []

    async def pay(state: Any) -> Any:
        calls.append("charged")
        raise Crash

    async def probe(state: Any, intent: Any) -> Applied:
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            return Applied({"paid": True})
        pytest.fail("recovery probe was not bounded")

    registry = ModuleRegistry()
    registry.register("pay", pay, effect="reconcilable", reconcile=probe)
    spec, store = spec_for("plain"), MemoryWorkflowStore()
    with pytest.raises(Crash):
        await WorkflowEngine(spec, registry, store=store).run({}, workflow_run_id="r")
    started = time.monotonic()
    paused = await WorkflowEngine(spec, registry, store=store, timeout=0.03).recover("r")
    assert time.monotonic() - started < 0.10
    assert paused.pause_reason == "effect_unknown" and calls == ["charged"]
    unknown = (await snapshot(store)).effects[paused.pending_effect["effect_id"]]
    assert unknown.status == EffectStatus.INDETERMINATE


async def test_limit_refuses_a_live_child_then_preserves_its_settlement() -> None:
    calls = []

    async def pay(state: Any) -> Any:
        calls.append("charged")
        raise Crash

    registry = ModuleRegistry()
    registry.register("prepare", lambda s: {}, effect="pure")
    registry.register("pay", pay)
    spec, store = spec_for("nested", prepare=True), MemoryWorkflowStore()
    with pytest.raises(Crash):
        await WorkflowEngine(spec, registry, store=store).run({}, workflow_run_id="r")
    child_run = await store.get("r/action#2")
    assert child_run is not None
    execution = await store.open_execution_store()
    held = await execution.claim(
        ExecutionId("default", child_run.workflow_run_id),
        "other",
        30,
        definition_digest=child_run.spec_hash,
    )
    assert held is not None
    engine = WorkflowEngine(spec, registry, store=store, max_steps=1)
    try:
        with pytest.raises(WorkflowError, match="held by another worker"):
            await engine.recover("r")
        assert (await snapshot(store)).run.status == "running" and calls == ["charged"]
    finally:
        await execution.release(held)
    paused = await engine.recover("r")
    assert paused.pause_reason == "effect_unknown"
    done = await engine.resolve_effect("r", "confirmed", actor="operator", result={"paid": True})
    assert done.status == "failed" and done.error == paused.error and calls == ["charged"]
