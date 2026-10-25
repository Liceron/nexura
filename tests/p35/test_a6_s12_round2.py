"""S12 parity on WorkflowEngine. Payments and reconciliation are local fakes only."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from nexura_agents.contracts import BaseModule, ExecutionContext, ModuleManifest, ModuleResult
from nexura_agents.contracts.execution import (
    Applied,
    EffectStatus,
    ExecutionId,
    NotApplied,
    Unknown,
)
from nexura_agents.errors import (
    WorkflowExecutionError,
    WorkflowTimeoutError,
    WorkflowValidationError,
)

from nexura_runtime.workflow import (
    MemoryWorkflowStore,
    ModuleRegistry,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
    step_idempotency_key,
)


def spec_for(shape: str, *, retry: bool = False, gated: bool = False) -> WorkflowSpec:
    node = WorkflowNode(id="pay", handler="pay", requires_approval=gated)
    if shape == "mapped":
        node.map_over = "items"
    elif shape == "parallel":
        node.handler, node.parallel = "", ["pay"]
    elif shape == "nested":
        node.handler = ""
        node.subgraph = spec_for("plain")
    if retry:
        node.retry = {"max_attempts": 2, "initial_delay": 0}
    return WorkflowSpec(name="fake-payment", entry_node="pay", nodes={"pay": node})


async def journal(store: Any, run: str = "r") -> Any:
    return await (await store.open_execution_store()).get(ExecutionId("default", run))


@pytest.mark.parametrize("mode", ["error", "timeout"])
@pytest.mark.parametrize("resolution", ["confirmed", "not_applied", "failed"])
@pytest.mark.parametrize("gated", [False, True])
async def test_unknown_payment_resolutions(mode: str, resolution: str, gated: bool) -> None:
    calls: list[str | None] = []

    async def pay(state: Any) -> dict[str, Any]:
        calls.append(step_idempotency_key())
        if len(calls) == 1:
            if mode == "timeout":
                await asyncio.sleep(1)
            raise ConnectionError("local fake payment applied; response lost")
        return {"paid": "retried"}

    registry = ModuleRegistry()
    registry.register("pay", pay)
    store = MemoryWorkflowStore()
    engine = WorkflowEngine(spec_for("plain", gated=gated), registry, store=store, timeout=0.05)
    paused = await engine.run({}, workflow_run_id="r")
    if gated:
        paused = await engine.resume("r", paused.pending_approval.approval_id, decision="approve")
    assert paused.status == "paused" and paused.pause_reason == "effect_unknown"
    assert paused.pending_effect and paused.error
    before = await journal(store)
    assert before.run.lease_owner is None
    (effect,) = before.effects.values()
    assert effect.status == EffectStatus.INDETERMINATE
    assert len(calls) == 1 and "paid" not in paused.state
    assert (await engine.recover("r")).pause_reason == "effect_unknown"
    done = await engine.resolve_effect(
        "r",
        resolution,
        actor="person",
        result={"paid": "ledger"},
        reason="Checked fake ledger",
    )
    if resolution == "not_applied" and gated:
        assert done.status == "paused" and done.pending_approval
        assert done.pending_approval.approval_id != paused.pending_effect.get("approval_id")
        assert len(calls) == 1
        done = await engine.resume("r", done.pending_approval.approval_id, decision="approve")
    assert done.status == ("failed" if resolution == "failed" else "completed")
    assert len(calls) == (2 if resolution == "not_applied" else 1)
    if resolution != "failed":
        assert done.state["paid"] == ("retried" if resolution == "not_applied" else "ledger")
    if resolution == "not_applied":
        assert calls[0] and calls[0] != calls[1]
    after = await journal(store)
    resolved = after.effects[effect.effect_id]
    assert resolved.resolved_by == "person" and resolved.resolution == resolution
    audit = [e.payload for e in after.events if e.event_type == "effect_resolved"]
    assert audit == [
        {
            "type": "effect_resolved",
            "effect_id": effect.effect_id,
            "resolution": resolution,
            "actor": "person",
            "reason": "Checked fake ledger",
        }
    ]


@pytest.mark.parametrize("mode", ["error", "timeout"])
async def test_pure_failure_is_terminal_with_no_effects(mode: str) -> None:
    async def pay(state: Any) -> Any:
        if mode == "timeout":
            await asyncio.sleep(1)
        raise ValueError("fake pure failure")

    registry = ModuleRegistry()
    registry.register("pay", pay, effect="pure")
    store = MemoryWorkflowStore()
    engine = WorkflowEngine(spec_for("plain"), registry, store=store, timeout=0.05)
    with pytest.raises(WorkflowTimeoutError if mode == "timeout" else WorkflowExecutionError):
        await engine.run({}, workflow_run_id="r")
    snapshot = await journal(store)
    assert snapshot.run.status == "failed" and not snapshot.effects


@pytest.mark.parametrize("mode", ["error", "timeout"])
@pytest.mark.parametrize("answer", ["applied", "not_applied", "unknown"])
@pytest.mark.parametrize(
    ("shape", "gated"),
    [
        ("plain", False),
        ("mapped", False),
        ("parallel", False),
        ("nested", False),
        ("plain", True),
        ("mapped", True),
        ("parallel", True),
    ],
)
async def test_reconciler_runs_before_unknown_pause(
    mode: str,
    answer: str,
    shape: str,
    gated: bool,
) -> None:
    calls, probes = [], []

    async def pay(state: Any) -> dict[str, bool]:
        calls.append(step_idempotency_key())
        if len(calls) == 1:
            if mode == "timeout":
                await asyncio.sleep(1)
            raise ConnectionError("fake lost response")
        return {"paid": True}

    def probe(state: Any, intent: Any) -> Any:
        probes.append(intent.idempotency_key)
        return {
            "applied": Applied({"paid": True}),
            "not_applied": NotApplied(),
            "unknown": Unknown(),
        }[answer]

    registry = ModuleRegistry()
    registry.register("pay", pay, effect="reconcilable", reconcile=probe)
    store = MemoryWorkflowStore()
    engine = WorkflowEngine(spec_for(shape, gated=gated), registry, store=store, timeout=0.05)
    started = time.monotonic()
    result = await engine.run({"items": [1]}, workflow_run_id="r")
    approval_id = None
    if gated:
        approval_id = result.pending_approval.approval_id
        result = await engine.resume("r", approval_id)
    assert time.monotonic() - started < 0.2
    if mode == "timeout":
        assert result.pause_reason == "effect_unknown" and len(calls) == 1 and probes == []
        done = await engine.resolve_effect("r", "confirmed", actor="person", result={"paid": True})
        assert done.status == "completed" and len(calls) == 1
        return
    if gated and answer == "not_applied":
        assert result.status == "paused" and result.pending_approval is not None
        assert result.pending_approval.approval_id != approval_id and len(calls) == 1
        result = await engine.resume("r", result.pending_approval.approval_id)
    assert probes == [calls[0]]
    assert len(calls) == (2 if answer == "not_applied" else 1)
    assert result.status == ("paused" if answer == "unknown" else "completed")
    if answer == "unknown":
        assert result.pause_reason == "effect_unknown"
        done = await engine.resolve_effect("r", "confirmed", actor="person", result={"paid": True})
        assert done.status == "completed" and len(calls) == 1


@pytest.mark.parametrize("effect", [None, "non_idempotent", "reconcilable"])
@pytest.mark.parametrize("shape", ["plain", "mapped", "parallel", "nested"])
def test_unsafe_retries_refused_before_dispatch(effect: Any, shape: str) -> None:
    registry = ModuleRegistry()
    registry.register(
        "pay",
        lambda state: pytest.fail("must never dispatch"),
        effect=effect,
        reconcile=(lambda state, intent: Unknown()) if effect == "reconcilable" else None,
    )
    with pytest.raises(WorkflowValidationError, match="non-idempotent|non_idempotent"):
        WorkflowEngine(spec_for(shape, retry=True), registry, store=MemoryWorkflowStore())


@pytest.mark.parametrize("shape", ["plain", "mapped", "parallel", "nested"])
async def test_retry_policy_rechecked_when_registry_changes(shape: str) -> None:
    registry = ModuleRegistry()
    calls = []
    registry.register("pay", lambda state: calls.append("unsafe"), effect="pure")
    store = MemoryWorkflowStore()
    engine = WorkflowEngine(spec_for(shape, retry=True), registry, store=store)
    registry.register("pay", lambda state: calls.append("unsafe"), effect="non_idempotent")
    with pytest.raises(WorkflowValidationError, match="cannot retry"):
        await engine.run({"items": [1]}, workflow_run_id="r")
    assert calls == [] and not (await journal(store)).effects


@pytest.mark.parametrize("effect", ["pure", "idempotent"])
async def test_safe_retries_exhaust_with_stable_key(effect: str) -> None:
    calls = []

    def pay(state: Any) -> Any:
        calls.append(step_idempotency_key())
        raise ConnectionError("local safe fake failed")

    registry = ModuleRegistry()
    registry.register("pay", pay, effect=effect)
    store = MemoryWorkflowStore()
    with pytest.raises(WorkflowExecutionError):
        await WorkflowEngine(spec_for("plain", retry=True), registry, store=store).run(
            {}, workflow_run_id="r"
        )
    snapshot = await journal(store)
    assert len(calls) == 2 and calls[0] == calls[1] and calls[0]
    assert snapshot.run.status == "failed"
    assert len(snapshot.effects) == (0 if effect == "pure" else 1)


class LocalOperations(BaseModule):
    """Fake read/write module with an implicit manifest policy and explicit write names."""

    manifest = ModuleManifest(
        module_id="fake.operations",
        version="1.0.0",
        kind="tool",
        title="Fake operations",
        description="Local read/write test double; no network.",
        write_operations=["pay"],
    )

    def __init__(self) -> None:
        self.calls: list[str | None] = []

    async def invoke(self, ctx: ExecutionContext, inputs: Any, cfg: Any) -> ModuleResult:
        self.calls.append(ctx.idempotency_key())
        if len(self.calls) == 1:
            raise ConnectionError("local read failed once")
        return ModuleResult(status="success", outputs={"ok": True})


@pytest.mark.parametrize("operation", ["pay", None])
@pytest.mark.parametrize("shape", ["plain", "mapped", "parallel", "nested"])
def test_manifest_write_or_unknown_operation_refuses_retries(operation: Any, shape: str) -> None:
    registry = ModuleRegistry()
    registry.register("pay", LocalOperations())
    spec = spec_for(shape, retry=True)
    leaf = spec.nodes["pay"]
    if shape == "nested":
        leaf = leaf.subgraph.nodes["pay"]
    leaf.input_mapping = {"operation": {"$literal": operation}}
    with pytest.raises(WorkflowValidationError, match="non-idempotent"):
        WorkflowEngine(spec, registry)


async def test_known_read_is_safe_and_runtime_rejects_a_changed_write_binding() -> None:
    registry = ModuleRegistry()
    module = LocalOperations()
    registry.register("pay", module)
    spec = spec_for("plain", retry=True)
    spec.nodes["pay"].input_mapping = {"operation": {"$literal": "read"}}
    store = MemoryWorkflowStore()
    engine = WorkflowEngine(spec, registry, store=store)
    done = await engine.run({}, workflow_run_id="read")
    assert done.status == "completed" and len(module.calls) == 2
    assert module.calls[0] == module.calls[1]
    assert not (await journal(store, "read")).effects
    # The operation becomes a write after validation; runtime must refuse before a send.
    spec.nodes["pay"].input_mapping = {"operation": {"$literal": "pay"}}
    with pytest.raises(WorkflowValidationError, match="cannot retry"):
        await engine.run({}, workflow_run_id="write")
    assert len(module.calls) == 2
    assert not (await journal(store, "write")).effects


@pytest.mark.parametrize("shape", ["plain", "mapped", "parallel", "nested"])
async def test_idempotent_registry_declaration_allows_composite_retries(shape: str) -> None:
    calls = []

    def pay(state: Any) -> dict[str, bool]:
        calls.append(step_idempotency_key())
        if len(calls) == 1:
            raise ConnectionError("local idempotent fake lost response")
        return {"paid": True}

    registry = ModuleRegistry()
    registry.register("pay", pay, effect="idempotent")
    store = MemoryWorkflowStore()
    done = await WorkflowEngine(spec_for(shape, retry=True), registry, store=store).run(
        {"items": [1]}
    )
    assert done.status == "completed" and len(calls) == 2
    if shape != "nested":  # nested retries create a fresh child, the existing contract
        assert calls[0] == calls[1]


@pytest.mark.parametrize("shape", ["mapped", "parallel", "nested"])
@pytest.mark.parametrize("resolution", ["confirmed", "not_applied", "failed"])
async def test_live_non_idempotent_part_remains_resolvable(shape: str, resolution: str) -> None:
    calls = []

    async def pay(state: Any) -> dict[str, bool]:
        calls.append(step_idempotency_key())
        if len(calls) == 1:
            raise ConnectionError("local fake side effect applied; reply lost")
        return {"paid": True}

    registry = ModuleRegistry()
    registry.register("pay", pay)
    store = MemoryWorkflowStore()
    engine = WorkflowEngine(spec_for(shape), registry, store=store)
    paused = await engine.run({"items": [1]}, workflow_run_id="r")
    assert paused.status == "paused" and paused.pause_reason == "effect_unknown"
    assert len(calls) == 1
    done = await engine.resolve_effect(
        "r",
        resolution,
        actor="person",
        reason="Checked fake recipient",
        result={"paid": True},
    )
    assert done.status == ("failed" if resolution == "failed" else "completed")
    assert len(calls) == (2 if resolution == "not_applied" else 1)


async def test_module_id_binding_uses_the_same_registry_declaration_at_runtime() -> None:
    from nexura_runtime.modules.registry import VersionedModuleRegistry

    module = LocalOperations()
    versioned = VersionedModuleRegistry()
    versioned.register(module)
    registry = ModuleRegistry(versioned)
    registry.register(module.manifest.module_id, module, effect="idempotent")
    spec = WorkflowSpec(
        name="module-id",
        entry_node="pay",
        nodes={
            "pay": WorkflowNode(
                id="pay",
                handler="",
                module_id=module.manifest.module_id,
                input_mapping={"operation": {"$literal": "pay"}},
                retry={"max_attempts": 2, "initial_delay": 0},
            ),
        },
    )
    store = MemoryWorkflowStore()
    engine = WorkflowEngine(spec, registry, store=store)
    gated = await engine.run({}, workflow_run_id="r")
    assert gated.pending_approval is not None and module.calls == []
    done = await engine.resume("r", gated.pending_approval.approval_id, decision="approve")
    assert done.status == "completed" and len(module.calls) == 2
    assert module.calls[0] == module.calls[1] and module.calls[0]
    (effect,) = (await journal(store)).effects.values()
    assert effect.intent.effect_class.value == "idempotent"
