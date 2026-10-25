"""Composite uncertainty, approval and deadline boundaries; local fake effects only."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
from nexura_agents.contracts import BaseModule, ModuleManifest, ModuleResult
from nexura_agents.contracts.execution import (
    Applied,
    EffectStatus,
    ExecutionId,
    NotApplied,
    Unknown,
)
from nexura_agents.errors import WorkflowExecutionError
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


def spec_for(shape: str, *, gated: bool = False) -> WorkflowSpec:
    node = WorkflowNode(id="pay", handler="pay", requires_approval=gated)
    if shape == "mapped":
        node.map_over = "items"
    elif shape == "parallel":
        node.handler, node.parallel = "", ["pay"]
    elif shape == "nested":
        node.handler, node.subgraph = "", spec_for("plain")
    return WorkflowSpec(name="round3", entry_node="pay", nodes={"pay": node})


def open_store(backend: str, path: Path) -> Any:
    return MemoryWorkflowStore() if backend == "memory" else SQLiteWorkflowStore(path)


async def snapshot(store: Any, run_id: str = "r") -> Any:
    return await (await store.open_execution_store()).get(ExecutionId("default", run_id))


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("effect", ["non_idempotent", "reconcilable"])
@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("first_resolution", ["confirmed", "failed"])
async def test_sibling_failure_waits_for_every_uncertain_payment(
    backend: str,
    effect: str,
    cancelled: bool,
    first_resolution: str,
    tmp_path: Path,
) -> None:
    calls, reads = [], []
    entered = {name: asyncio.Event() for name in ("a", "b")}

    async def pay(name: str, state: Any) -> dict[str, Any]:
        calls.append(name)
        entered[name].set()
        if cancelled:
            await asyncio.sleep(10)
        raise ConnectionError("local fake debit applied; reply lost")

    async def read(state: Any) -> Any:
        await entered["a"].wait()
        await entered["b"].wait()
        reads.append("read")
        raise ValueError("sibling read failed")

    registry = ModuleRegistry()
    for name in ("a", "b"):

        async def bound(state: Any, name: str = name) -> Any:
            return await pay(name, state)

        registry.register(
            name,
            bound,
            effect=effect,
            reconcile=(lambda s, i: Unknown()) if effect == "reconcilable" else None,
        )
    registry.register("read", read, effect="pure")
    spec = WorkflowSpec(
        name="mixed",
        entry_node="both",
        nodes={
            "both": WorkflowNode(id="both", handler="", parallel=["a", "b", "read"]),
        },
    )
    db = tmp_path / "mixed.db"
    store = open_store(backend, db)
    try:
        engine = WorkflowEngine(spec, registry, store=store)
        paused = await engine.run({}, workflow_run_id="r")
        assert paused.status == "paused" and paused.pause_reason == "effect_unknown"
        assert "sibling read failed" in (paused.error or "")
        before = await snapshot(store)
        payments = [e for e in before.effects.values() if e.intent.effect_class.value == effect]
        assert len(payments) == 2 and all(e.status == EffectStatus.INDETERMINATE for e in payments)
        assert before.run.lease_owner is None
        if backend == "sqlite":
            await store.close()
            store = open_store(backend, db)
            engine = WorkflowEngine(spec, registry, store=store)
        for index, resolution in enumerate((first_resolution, "confirmed")):
            done = await engine.resolve_effect(
                "r", resolution, actor="person", result={"paid": True}
            )
            if index == 0:
                assert done.status == "paused" and done.pause_reason == "effect_unknown"
            else:
                assert done.status == "failed" and "sibling read failed" in (done.error or "")
        assert sorted(calls) == ["a", "b"] and reads == ["read"]
        after = await snapshot(store)
        assert all(after.effects[e.effect_id].status == EffectStatus.RESOLVED for e in payments)
    finally:
        await store.close()


async def test_mapped_read_error_preserves_a_cancelled_write() -> None:
    entered = asyncio.Event()
    calls = []

    class MixedOperations(BaseModule):
        manifest = ModuleManifest(
            module_id="local.mixed",
            version="1.0.0",
            kind="tool",
            title="Local fake",
            description="Local fake read and debit",
            write_operations=["pay"],
        )

        async def invoke(self, ctx: Any, inputs: Any, cfg: Any) -> ModuleResult:
            operation = inputs["operation"]
            calls.append(operation)
            if operation == "pay":
                entered.set()
                await asyncio.sleep(10)
                return ModuleResult(status="success")
            await entered.wait()
            raise ValueError("mapped read failed")

    registry = ModuleRegistry()
    registry.register("pay", MixedOperations())
    spec = spec_for("mapped")
    spec.nodes["pay"].input_mapping = {"operation": "item"}
    store = MemoryWorkflowStore()
    engine = WorkflowEngine(spec, registry, store=store)
    gate = await engine.run({"items": ["pay", "read"]}, workflow_run_id="r")
    paused = await engine.resume("r", gate.pending_approval.approval_id)
    assert paused.pause_reason == "effect_unknown" and "mapped read failed" in (paused.error or "")
    done = await engine.resolve_effect("r", "confirmed", actor="person", result={"paid": True})
    assert done.status == "failed" and "mapped read failed" in (done.error or "")
    assert calls == ["pay", "read"]


async def test_all_pure_composite_failure_has_no_effects() -> None:
    entered = asyncio.Event()

    async def read(state: Any) -> Any:
        entered.set()
        await asyncio.sleep(10)

    async def broken(state: Any) -> Any:
        await entered.wait()
        raise ValueError("pure read failed")

    registry = ModuleRegistry()
    registry.register("read", read, effect="pure")
    registry.register("broken", broken, effect="pure")
    spec = WorkflowSpec(
        name="pure",
        entry_node="both",
        nodes={
            "both": WorkflowNode(id="both", handler="", parallel=["read", "broken"]),
        },
    )
    store = MemoryWorkflowStore()
    with pytest.raises(WorkflowExecutionError, match="pure read failed"):
        await WorkflowEngine(spec, registry, store=store).run({}, workflow_run_id="r")
    result = await snapshot(store)
    assert result.run.status == "failed" and not result.effects


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("shape", ["mapped", "parallel"])
async def test_live_not_applied_regates_and_keeps_finished_siblings(
    backend: str,
    shape: str,
    tmp_path: Path,
) -> None:
    calls, completed, probes = [], [], []

    async def pay(state: Any) -> dict[str, Any]:
        if shape == "mapped" and state["item"] == "done":
            return await finish(state)
        calls.append(step_idempotency_key())
        if len(calls) == 1:
            raise ConnectionError("fake recipient proves not applied")
        return {"paid": True}

    async def finish(state: Any) -> dict[str, Any]:
        completed.append("done")
        return {"done": True}

    def probe(state: Any, intent: Any) -> Any:
        probes.append(intent.idempotency_key)
        return NotApplied()

    registry = ModuleRegistry()
    registry.register("pay", pay, effect="reconcilable", reconcile=probe)
    registry.register("done", finish, effect="pure")
    spec = spec_for(shape, gated=True)
    if shape == "parallel":
        spec.nodes["pay"].parallel = ["done", "pay"]
    db = tmp_path / "gated.db"
    store = open_store(backend, db)
    try:
        engine = WorkflowEngine(spec, registry, store=store)
        first = await engine.run({"items": ["done", "pay"]}, workflow_run_id="r")
        first_id = first.pending_approval.approval_id
        second = await engine.resume("r", first_id)
        assert second.status == "paused" and second.pending_approval is not None
        second_id = second.pending_approval.approval_id
        assert second_id != first_id and len(calls) == 1 and completed == ["done"]
        if backend == "sqlite":
            await store.close()
            store = open_store(backend, db)
            engine = WorkflowEngine(spec, registry, store=store)
        done = await engine.resume("r", second_id)
        assert done.status == "completed" and done.state["done"] and done.state["paid"]
        assert len(calls) == 2 and calls[0] == calls[1] and probes == [calls[0]]
        assert completed == ["done"]
        audit = (await snapshot(store)).events
        consumed = [e.payload["approval_id"] for e in audit if e.event_type == "approval_consumed"]
        assert second_id in consumed
    finally:
        await store.close()


@pytest.mark.parametrize("shape", ["plain", "mapped", "parallel", "nested"])
@pytest.mark.parametrize("failure", ["timeout", "error_then_timeout"])
async def test_automatic_reconciliation_shares_one_deadline(shape: str, failure: str) -> None:
    calls = []

    async def pay(state: Any) -> Any:
        calls.append(step_idempotency_key())
        if failure == "error_then_timeout" and len(calls) == 1:
            await asyncio.sleep(0.015)
            raise ConnectionError("local fake not applied")
        await asyncio.sleep(0.3)

    registry = ModuleRegistry()
    registry.register("pay", pay, effect="reconcilable", reconcile=lambda s, i: NotApplied())
    store = MemoryWorkflowStore()
    engine = WorkflowEngine(spec_for(shape), registry, store=store, timeout=0.06)
    started = time.monotonic()
    paused = await engine.run({"items": [1]}, workflow_run_id="r")
    elapsed = time.monotonic() - started
    assert 0.045 <= elapsed < 0.16
    assert paused.status == "paused" and paused.pause_reason == "effect_unknown"
    assert len(calls) == (1 if failure == "timeout" else 2)
    assert len(set(calls)) == 1


@pytest.mark.parametrize("shape", ["plain", "mapped", "parallel"])
async def test_repeated_not_applied_is_bounded_without_timeout(shape: str) -> None:
    calls = []

    async def pay(state: Any) -> Any:
        calls.append(step_idempotency_key())
        raise ConnectionError("local fake not applied")

    registry = ModuleRegistry()
    registry.register("pay", pay, effect="reconcilable", reconcile=lambda s, i: NotApplied())
    store = MemoryWorkflowStore()
    started = time.monotonic()
    paused = await WorkflowEngine(spec_for(shape), registry, store=store).run({"items": [1]})
    assert time.monotonic() - started < 0.3
    assert paused.pause_reason == "effect_unknown" and len(calls) == 4
    assert len(set(calls)) == 1


async def test_slow_reconciler_is_bounded_by_the_original_deadline() -> None:
    async def pay(state: Any) -> Any:
        raise ConnectionError("local fake lost response")

    async def probe(state: Any, intent: Any) -> Any:
        await asyncio.sleep(0.3)
        return Applied({"paid": True})

    registry = ModuleRegistry()
    registry.register("pay", pay, effect="reconcilable", reconcile=probe)
    started = time.monotonic()
    paused = await WorkflowEngine(
        spec_for("plain"), registry, store=MemoryWorkflowStore(), timeout=0.04
    ).run({})
    assert 0.03 <= time.monotonic() - started < 0.15
    assert paused.pause_reason == "effect_unknown" and "paid" not in paused.state


async def test_reconciled_result_continuation_keeps_the_deadline() -> None:
    calls = []

    async def pay(state: Any) -> Any:
        calls.append("pay")
        await asyncio.sleep(0.2)
        raise ConnectionError("local fake response lost")

    async def probe(state: Any, intent: Any) -> Any:
        await asyncio.sleep(0.2)
        return Applied({"paid": True})

    async def next_action(state: Any) -> Any:
        calls.append("next")
        await asyncio.sleep(3)

    registry = ModuleRegistry()
    registry.register("pay", pay, effect="reconcilable", reconcile=probe)
    registry.register("next", next_action)
    spec = spec_for("plain")
    spec.nodes["pay"].default_transition = "next"
    spec.nodes["next"] = WorkflowNode(id="next", handler="next")
    started = time.monotonic()
    paused = await WorkflowEngine(spec, registry, store=MemoryWorkflowStore(), timeout=0.6).run({})
    # Timings are scaled 10x so the shared deadline is not eaten by test-runner overhead.
    assert 0.45 <= time.monotonic() - started < 1.5
    assert paused.pause_reason == "effect_unknown" and paused.state["paid"] is True
    assert paused.pending_effect["node_id"] == "next" and calls == ["pay", "next"]


async def test_deadline_expiring_during_continuation_commit_keeps_the_effect_resolvable() -> None:
    class DelayedContinuationStore(MemoryExecutionStore):
        async def commit(self, lease: Any, **kwargs: Any) -> Any:
            if any(e.get("type") == "run_recovered" for e in kwargs.get("events", [])):
                await asyncio.sleep(0.04)  # real store latency after the NotApplied decision
            return await super().commit(lease, **kwargs)

    calls = []

    async def pay(state: Any) -> Any:
        calls.append("sent")
        raise ConnectionError("local fake not applied")

    registry = ModuleRegistry()
    registry.register("pay", pay, effect="reconcilable", reconcile=lambda s, i: NotApplied())
    store = MemoryWorkflowStore(execution_store=DelayedContinuationStore())
    engine = WorkflowEngine(spec_for("plain"), registry, store=store, timeout=0.02)
    started = time.monotonic()
    paused = await engine.run({}, workflow_run_id="r")
    assert time.monotonic() - started < 0.15
    assert paused.pause_reason == "effect_unknown" and calls == ["sent"]
    assert (await snapshot(store)).run.status == "paused"
    done = await engine.resolve_effect("r", "confirmed", actor="person", result={"paid": True})
    assert done.status == "completed" and calls == ["sent"]


async def test_sibling_cancellation_after_intent_commit_reloads_the_persisted_fact() -> None:
    committed = asyncio.Event()
    calls = []

    class DelayedIntentStore(MemoryExecutionStore):
        async def commit(self, lease: Any, **kwargs: Any) -> Any:
            result = await super().commit(lease, **kwargs)
            if any(e.get("step_key") == "both/pay#1" for e in kwargs.get("events", [])):
                committed.set()
                await asyncio.sleep(10)  # response to the intent commit is lost on cancellation
            return result

    async def pay(state: Any) -> Any:
        calls.append("sent")
        return {"paid": True}

    async def read(state: Any) -> Any:
        await committed.wait()
        raise ValueError("sibling read failed after intent committed")

    registry = ModuleRegistry()
    registry.register("pay", pay)
    registry.register("read", read, effect="pure")
    spec = WorkflowSpec(
        name="commit-race",
        entry_node="both",
        nodes={
            "both": WorkflowNode(id="both", handler="", parallel=["pay", "read"]),
        },
    )
    store = MemoryWorkflowStore(execution_store=DelayedIntentStore())
    engine = WorkflowEngine(spec, registry, store=store)
    with pytest.raises(WorkflowExecutionError, match="sibling read failed"):
        await engine.run({}, workflow_run_id="r")
    failed = await engine.get_result("r")
    assert failed.status == "failed" and failed.pending_effect is None
    payment = next(
        e
        for e in (await snapshot(store)).effects.values()
        if e.intent.effect_class.value == "non_idempotent"
    )
    assert payment.status == EffectStatus.RESOLVED
    assert payment.resolution.value == "not_applied"
    assert payment.resolved_by == "workflow:pre_dispatch"
    assert calls == []


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_cancelled_mapped_subworkflow_keeps_its_payment_resolvable(
    backend: str,
    tmp_path: Path,
) -> None:
    entered = asyncio.Event()
    calls = []

    async def pay(state: Any) -> Any:
        calls.append("pay")
        entered.set()
        await asyncio.sleep(10)

    async def read(state: Any) -> Any:
        await entered.wait()
        calls.append("read")
        raise ValueError("child read failed")

    registry = ModuleRegistry()
    registry.register("route", lambda s: {"_branch": s["item"]}, effect="pure")
    registry.register("pay", pay)
    registry.register("read", read, effect="pure")
    child = WorkflowSpec(
        name="routed-child",
        entry_node="route",
        nodes={
            "route": WorkflowNode(
                id="route", handler="route", transitions={"pay": "pay", "read": "read"}
            ),
            "pay": WorkflowNode(id="pay", handler="pay"),
            "read": WorkflowNode(id="read", handler="read"),
        },
    )
    spec = WorkflowSpec(
        name="mapped-children",
        entry_node="m",
        nodes={
            "m": WorkflowNode(id="m", handler="", subgraph=child, map_over="items"),
        },
    )
    store = open_store(backend, tmp_path / "children.db")
    try:
        engine = WorkflowEngine(spec, registry, store=store)
        paused = await engine.run({"items": ["pay", "read"]}, workflow_run_id="r")
        assert paused.pause_reason == "effect_unknown" and "child read failed" in (
            paused.error or ""
        )
        target = paused.pending_effect["run_id"]
        effect_id = paused.pending_effect["effect_id"]
        child_snapshot = await snapshot(store, target)
        assert child_snapshot.effects[effect_id].status == EffectStatus.INDETERMINATE
        failed = await engine.resolve_effect(
            "r", "confirmed", actor="person", result={"paid": True}
        )
        assert failed.status == "failed" and "child read failed" in (failed.error or "")
        assert calls == ["pay", "read"]
    finally:
        await store.close()
