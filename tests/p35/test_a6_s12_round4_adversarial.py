"""Independent round-three boundary probes. All payments/models are local fakes."""

import asyncio
import time

import pytest
from nexura_agents.contracts.execution import Applied, ExecutionId, NotApplied
from nexura_agents.errors import WorkflowMaxStepsError

from nexura_runtime.workflow import (
    MemoryWorkflowStore,
    ModuleRegistry,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
)


class Crash(BaseException):
    """Simulated abrupt process stop after a persisted intent and fake send."""


async def test_recover_probe_obeys_its_invocation_deadline():
    calls = []

    async def pay(state):
        calls.append("charged")
        raise Crash

    async def probe(state, intent):
        await asyncio.sleep(0.12)
        return Applied({"paid": True})

    registry = ModuleRegistry()
    registry.register("pay", pay, effect="reconcilable", reconcile=probe)
    spec = WorkflowSpec(
        name="recovery-deadline",
        entry_node="pay",
        nodes={
            "pay": WorkflowNode(id="pay", handler="pay"),
        },
    )
    store = MemoryWorkflowStore()
    with pytest.raises(Crash):
        await WorkflowEngine(spec, registry, store=store).run({}, workflow_run_id="r")
    started = time.monotonic()
    result = await WorkflowEngine(spec, registry, store=store, timeout=0.03).recover("r")
    elapsed = time.monotonic() - started
    assert result.status == "paused" and result.pause_reason == "effect_unknown", (
        result.status,
        elapsed,
        calls,
    )
    assert elapsed < 0.10
    assert calls == ["charged"]


async def test_recover_reconciliation_resend_count_has_no_free_first_resend():
    calls = []
    crashing = [True]

    async def pay(state):
        calls.append("sent")
        if crashing[0]:
            raise Crash
        raise ConnectionError("local fake definitely not applied")

    registry = ModuleRegistry()
    registry.register("pay", pay, effect="reconcilable", reconcile=lambda s, i: NotApplied())
    spec = WorkflowSpec(
        name="resend-count",
        entry_node="pay",
        nodes={
            "pay": WorkflowNode(id="pay", handler="pay"),
        },
    )
    store = MemoryWorkflowStore()
    with pytest.raises(Crash):
        await WorkflowEngine(spec, registry, store=store).run({}, workflow_run_id="r")
    crashing[0] = False
    calls.clear()
    result = await WorkflowEngine(spec, registry, store=store).recover("r")
    assert result.pause_reason == "effect_unknown"
    assert len(calls) <= 3, len(calls)


@pytest.mark.parametrize("shape", ["parallel", "mapped", "nested"])
async def test_recovery_step_limit_cannot_make_unknown_composite_terminal(shape):
    calls = []

    async def pay(state):
        calls.append("charged")
        raise Crash

    registry = ModuleRegistry()
    registry.register("prepare", lambda s: {}, effect="pure")
    registry.register("pay", pay)
    action = WorkflowNode(id="action", handler="pay")
    if shape == "parallel":
        action.handler, action.parallel = "", ["pay"]
    elif shape == "mapped":
        action.map_over = "items"
    else:
        action.handler = ""
        action.subgraph = WorkflowSpec(
            name="child",
            entry_node="pay",
            nodes={
                "pay": WorkflowNode(id="pay", handler="pay"),
            },
        )
    spec = WorkflowSpec(
        name="steps",
        entry_node="prepare",
        nodes={
            "prepare": WorkflowNode(id="prepare", handler="prepare", default_transition="action"),
            "action": action,
        },
    )
    store = MemoryWorkflowStore()
    with pytest.raises(Crash):
        await WorkflowEngine(spec, registry, store=store, max_steps=5).run(
            {"items": [1]}, workflow_run_id="r"
        )
    try:  # noqa: SIM105 - keep the independent repro's original control flow
        await WorkflowEngine(spec, registry, store=store, max_steps=1).recover("r")
    except WorkflowMaxStepsError:
        pass
    snapshot = await (await store.open_execution_store()).get(ExecutionId("default", "r"))
    assert calls == ["charged"]
    assert snapshot.run.status not in ("completed", "failed"), snapshot.run.status
