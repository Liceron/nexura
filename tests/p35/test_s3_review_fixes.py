"""Fixes for the S3 review findings M3a, M3b and L5 (REVIEW_S345.md).

The three tests are copied from .autopilot/p35/review/test_s345_review.py with their
assertions unchanged (return annotations added, ruff-formatted). Local fake effects only.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest
from nexura_agents.contracts.execution import EffectResolution, EffectStatus, ExecutionId
from nexura_agents.execution_store import MemoryExecutionStore

from nexura_runtime.runtime.execution import (
    DurableExecutionEngine,
    DurableNodeSpec,
    DurableWorkflowSpec,
)


def pay_spec() -> DurableWorkflowSpec:
    # effect_policy defaults to non_idempotent_write since S3.
    return DurableWorkflowSpec(
        workflow_id="review",
        entry_node="pay",
        terminal_nodes=["pay"],
        nodes={"pay": DurableNodeSpec(node_id="pay", handler="debit")},
    )


async def test_m3a_failed_handler_effect_remains_resolvable() -> None:
    """M3a: a raising non_idempotent handler fails the run AND marks the effect
    indeterminate. A failed run cannot be claimed, so engine.resolve_effect and
    POST .../resolve (409 RUN_FINISHED) can never resolve it. Correct: pause with
    effect_unknown, or allow recording a resolution on a terminal run."""
    store = MemoryExecutionStore()
    effects: list[str] = []

    async def debit(_: Any) -> Any:
        effects.append("debit")
        raise OSError("local fake: remote accepted, response lost")

    spec = pay_spec()
    engine = DurableExecutionEngine(store, {"debit": debit})
    await engine.init_run("ws", "run", spec)
    await engine.claim_run("ws", "run", "worker")
    with pytest.raises(OSError):
        await engine.execute_step("ws", "run", "worker", spec)
    snapshot = await store.get(ExecutionId("ws", "run"))
    (effect,) = snapshot.effects.values()
    assert effect.status == EffectStatus.INDETERMINATE and effects == ["debit"]
    resolved = await engine.resolve_effect(
        "ws",
        "run",
        effect.effect_id,
        EffectResolution.CONFIRMED,
        actor="reviewer",
        result={"paid": True},
    )
    assert resolved.resolved_by == "reviewer"


async def test_m3b_cancelled_worker_does_not_finish_the_run() -> None:
    """M3b: execute_step catches BaseException, so cancelling the worker task (graceful
    shutdown, SIGTERM -> task.cancel) commits status failed with the effect indeterminate:
    the run is terminal and unresolvable. Correct: cancellation = crash semantics; the run
    stays recoverable and the next worker pauses it effect_unknown."""
    store = MemoryExecutionStore()
    started = asyncio.Event()

    async def debit(_: Any) -> Any:
        started.set()
        await asyncio.sleep(30)

    spec = pay_spec()
    engine = DurableExecutionEngine(store, {"debit": debit})
    await engine.init_run("ws", "run", spec)
    await engine.claim_run("ws", "run", "worker")
    task = asyncio.create_task(engine.execute_step("ws", "run", "worker", spec))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    snapshot = await store.get(ExecutionId("ws", "run"))
    assert snapshot.run.status not in ("completed", "failed"), (
        f"worker shutdown finished the run as {snapshot.run.status!r}"
    )


async def test_l5_cancelling_a_dispatched_node_does_not_leave_a_pending_effect() -> None:
    """L5: request_cancellation while a non_idempotent node is in flight commits the run
    terminal (journal 'failed') but leaves its effect 'pending' forever: not indeterminate,
    so not resolvable, and the handler's completion is refused. Correct: indeterminate."""
    store = MemoryExecutionStore()
    started, release = asyncio.Event(), asyncio.Event()

    async def debit(_: Any) -> Any:
        started.set()
        await release.wait()
        return {"paid": True}

    spec = pay_spec()
    engine = DurableExecutionEngine(store, {"debit": debit})
    await engine.init_run("ws", "run", spec)
    await engine.claim_run("ws", "run", "worker")
    task = asyncio.create_task(engine.execute_step("ws", "run", "worker", spec))
    await asyncio.wait_for(started.wait(), 5)
    await engine.request_cancellation("ws", "run")
    release.set()
    with contextlib.suppress(Exception):
        await task
    snapshot = await store.get(ExecutionId("ws", "run"))
    (effect,) = snapshot.effects.values()
    assert snapshot.run.status == "failed"
    assert effect.status != EffectStatus.PENDING, "terminal run keeps a pending effect"


def timeout_spec(policy: str = "non_idempotent_write") -> DurableWorkflowSpec:
    return DurableWorkflowSpec(
        workflow_id="review-timeout",
        entry_node="pay",
        terminal_nodes=["pay"],
        nodes={
            "pay": DurableNodeSpec(
                node_id="pay", handler="debit", effect_policy=policy, timeout_seconds=0.05
            )
        },
    )


@pytest.mark.parametrize("resolution", list(EffectResolution))
async def test_timed_out_non_pure_node_pauses_and_is_resolvable(
    resolution: EffectResolution,
) -> None:
    """Founder decision 2: a timed-out non-pure call has an unknown outcome, so the run
    pauses effect_unknown (lease released) instead of failing, and each resolution works."""
    from nexura_agents.contracts.errors import DeadlineExceededError

    store = MemoryExecutionStore()
    sends: list[str] = []

    async def slow(_: Any) -> Any:
        sends.append("debit")
        await asyncio.sleep(1)
        return {"paid": "late"}

    async def fast(_: Any) -> Any:
        sends.append("debit")
        return {"paid": "retried"}

    spec = timeout_spec()
    engine = DurableExecutionEngine(store, {"debit": slow})
    await engine.init_run("ws", "run", spec)
    await engine.claim_run("ws", "run", "worker")
    with pytest.raises(DeadlineExceededError, match="timed out"):
        await engine.execute_step("ws", "run", "worker", spec)
    snapshot = await store.get(ExecutionId("ws", "run"))
    (effect,) = snapshot.effects.values()
    assert snapshot.run.status == "paused" and snapshot.run.lease_owner is None
    assert effect.status == EffectStatus.INDETERMINATE
    state = await engine.get_state("ws", "run")
    assert state.status == "paused" and state.pause_reason == "effect_unknown"
    assert state.pending_effect is not None and "timed out" in (state.error or "")

    engine.register_handler("debit", fast)
    await engine.resolve_effect(
        "ws", "run", effect.effect_id, resolution, actor="person", result={"paid": "ledger"}
    )
    done = await engine.run_until_idle("ws", "run", "resumer", spec)
    if resolution == EffectResolution.CONFIRMED:
        assert done.status == "completed" and done.state["paid"] == "ledger"
        assert sends == ["debit"]
    elif resolution == EffectResolution.NOT_APPLIED:
        assert done.status == "completed" and done.state["paid"] == "retried"
        assert sends == ["debit", "debit"]
    else:
        assert done.status == "failed" and sends == ["debit"]
    final = await store.get(ExecutionId("ws", "run"))
    assert final.effects[effect.effect_id].resolved_by == "person"


async def test_timed_out_pure_node_still_fails_the_run() -> None:
    from nexura_agents.contracts.errors import DeadlineExceededError

    store = MemoryExecutionStore()

    async def slow(_: Any) -> Any:
        await asyncio.sleep(1)

    spec = timeout_spec("pure")
    engine = DurableExecutionEngine(store, {"debit": slow})
    await engine.init_run("ws", "run", spec)
    await engine.claim_run("ws", "run", "worker")
    with pytest.raises(DeadlineExceededError):
        await engine.execute_step("ws", "run", "worker", spec)
    snapshot = await store.get(ExecutionId("ws", "run"))
    assert snapshot.run.status == "failed" and not snapshot.effects
