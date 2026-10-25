"""Round-two independent cases: local fake payments; no TCP/provider calls."""

import asyncio
import time

import httpx
import pytest
from _local_env import local_headers
from nexura_agents.contracts import BaseModule, ModuleManifest, ModuleResult
from nexura_agents.contracts.execution import ExecutionId, NotApplied, Unknown

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.workflow import (
    MemoryWorkflowStore,
    ModuleRegistry,
    SQLiteWorkflowStore,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
)


def one(shape, *, gated=False):
    node = WorkflowNode(id="pay", handler="pay", requires_approval=gated)
    if shape == "mapped":
        node.map_over = "items"
    elif shape == "parallel":
        node.handler, node.parallel = "", ["pay"]
    return WorkflowSpec(name="review", entry_node="pay", nodes={"pay": node})


@pytest.mark.parametrize("shape", ["plain", "mapped", "parallel"])
async def test_gated_reconciliation_not_applied_requires_fresh_approval(shape):
    calls, probes = [], []

    async def pay(state):
        calls.append("sent")
        if len(calls) == 1:
            raise ConnectionError("local fake recipient proves it did not apply")
        return {"paid": True}

    def reconcile(state, intent):
        probes.append(intent.approval_id)
        return NotApplied()

    registry = ModuleRegistry()
    registry.register("pay", pay, effect="reconcilable", reconcile=reconcile)
    store = MemoryWorkflowStore()
    engine = WorkflowEngine(one(shape, gated=True), registry, store=store)
    waiting = await engine.run({"items": [1]}, workflow_run_id="gated")
    first_id = waiting.pending_approval.approval_id
    result = await engine.resume("gated", first_id)
    assert result.status == "paused", (shape, result.status, calls)
    assert result.pending_approval is not None
    assert result.pending_approval.approval_id != first_id
    assert calls == ["sent"]


async def test_reconciler_not_applied_does_not_reset_workflow_deadline():
    calls = []

    async def pay(state):
        calls.append(time.monotonic())
        if len(calls) < 3:
            await asyncio.sleep(0.2)
        return {"paid": True}

    registry = ModuleRegistry()
    registry.register("pay", pay, effect="reconcilable", reconcile=lambda s, i: NotApplied())
    store = MemoryWorkflowStore()
    engine = WorkflowEngine(one("plain"), registry, store=store, timeout=0.04)
    start = time.monotonic()
    result = await engine.run({}, workflow_run_id="deadline")
    elapsed = time.monotonic() - start
    assert len(calls) <= 1, (result.status, len(calls), elapsed)


@pytest.mark.parametrize("shape", ["mapped", "parallel"])
async def test_reconcilable_timeout_unknown_resolution_not_applied(shape):
    calls = []

    async def pay(state):
        calls.append("sent")
        if len(calls) == 1:
            await asyncio.sleep(1)
        return {"paid": True}

    registry = ModuleRegistry()
    registry.register("pay", pay, effect="reconcilable", reconcile=lambda s, i: Unknown())
    store = MemoryWorkflowStore()
    engine = WorkflowEngine(one(shape), registry, store=store, timeout=0.04)
    paused = await engine.run({"items": [1]}, workflow_run_id="timeout")
    assert paused.status == "paused" and paused.pause_reason == "effect_unknown"
    result = await engine.resolve_effect("timeout", "not_applied", actor="reviewer", result={})
    assert result.status == "completed" and calls == ["sent", "sent"]
    snapshot = await (await store.open_execution_store()).get(ExecutionId("default", "timeout"))
    assert len(snapshot.effects) >= 2


async def test_mapped_literal_read_keeps_safe_retries():
    class ReadOnlyInvocation(BaseModule):
        manifest = ModuleManifest(
            module_id="review.readwrite",
            version="1.0.0",
            kind="tool",
            title="Local fake",
            description="Local fake module with a known read and a declared write",
            write_operations=["pay"],
        )

        async def invoke(self, ctx, inputs, cfg):
            assert inputs == {"operation": "read"}
            return ModuleResult(status="success", outputs={"read": True})

    registry = ModuleRegistry()
    registry.register("pay", ReadOnlyInvocation())
    spec = one("mapped")
    spec.nodes["pay"].input_mapping = {"operation": {"$literal": "read"}}
    spec.nodes["pay"].retry = {"max_attempts": 2}
    engine = WorkflowEngine(spec, registry, store=MemoryWorkflowStore())
    done = await engine.run({"items": [1]})
    assert done.status == "completed"


@pytest.mark.parametrize("effect", ["non_idempotent", "reconcilable"])
@pytest.mark.parametrize("mode", ["error", "timeout"])
@pytest.mark.parametrize("resolution", ["confirmed", "not_applied", "failed"])
async def test_rest_all_uncertain_resolutions(tmp_path, effect, mode, resolution):
    calls = []

    async def pay(state):
        calls.append("sent")
        if len(calls) == 1:
            if mode == "timeout":
                await asyncio.sleep(5)
            raise ConnectionError("local fake lost response")
        return {"paid": "retried"}

    registry = ModuleRegistry()
    registry.register(
        "pay",
        pay,
        effect=effect,
        reconcile=(lambda s, i: Unknown()) if effect == "reconcilable" else None,
    )
    db = tmp_path / "rest.db"
    app = create_api_app(db_path=str(db), registry=registry, profile="local-owner")
    store = SQLiteWorkflowStore(db)
    try:
        engine = WorkflowEngine(
            # Generous enough for the first dispatch under suite load; short deadlines
            # have their own tests.
            one("plain"),
            registry,
            store=store,
            workspace_id="ws",
            timeout=0.5,
        )
        paused = await engine.run({}, workflow_run_id="r")
        assert paused.status == "paused" and calls == ["sent"]
        eid = paused.pending_effect["effect_id"]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.post(
                f"/api/v1/runs/r/effects/{eid}/resolve",
                headers=local_headers("ws"),
                json={
                    "resolution": resolution,
                    "result": {"paid": "ledger"},
                    "reason": "local ledger checked",
                },
            )
        assert response.status_code == 200, response.text
        done = await engine.recover("r")
        assert done.status == ("failed" if resolution == "failed" else "completed")
        assert len(calls) == (2 if resolution == "not_applied" else 1)
        snapshot = await (await store.open_execution_store()).get(ExecutionId("ws", "r"))
        audit = [e.payload for e in snapshot.events if e.event_type == "effect_resolved"]
        assert len(audit) == 1
        assert audit[0]["actor"] == "local-owner" and audit[0]["reason"] == "local ledger checked"
    finally:
        await store.close()


async def test_pure_sibling_error_cannot_strand_an_unknown_payment():
    calls = []
    sent = asyncio.Event()

    async def pay(state):
        calls.append("charged")
        sent.set()
        raise ConnectionError("local fake payment accepted but response lost")

    async def read(state):
        await sent.wait()
        raise ValueError("local read failed")

    registry = ModuleRegistry()
    registry.register("pay", pay)
    registry.register("read", read, effect="pure")
    spec = WorkflowSpec(
        name="mixed",
        entry_node="both",
        nodes={
            "both": WorkflowNode(id="both", handler="", parallel=["pay", "read"]),
        },
    )
    store = MemoryWorkflowStore()
    engine = WorkflowEngine(spec, registry, store=store)
    from nexura_agents.errors import WorkflowExecutionError

    try:  # noqa: SIM105 - retain the independent review's control flow and assertions
        await engine.run({}, workflow_run_id="mixed")
    except WorkflowExecutionError:
        pass
    snapshot = await (await store.open_execution_store()).get(ExecutionId("default", "mixed"))
    assert calls == ["charged"]
    assert snapshot.run.status == "paused", (
        snapshot.run.status,
        [(e.intent.effect_class.value, e.status.value) for e in snapshot.effects.values()],
    )
    result = await engine.get_result("mixed")
    assert result.pause_reason == "effect_unknown"
