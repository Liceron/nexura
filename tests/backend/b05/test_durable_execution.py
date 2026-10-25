"""B05: durable execution and worker lifecycle, on ``WorkflowEngine`` and the execution journal.

These cases used to run on the legacy ``DurableExecutionEngine``, removed by P35 A6 S12.
Each assertion that still describes a behaviour of the product was moved here, onto its
replacement (``WorkflowEngine`` with ``SQLiteWorkflowStore``: a fenced run lease, a step
intent before each non-pure step, ``recover`` and ``resolve_effect``). Crashes are simulated
in-process with a ``BaseException`` the engine does not catch; the real-process kill rows are
in ``tests/durability``. Every effect is a local fake ledger.

Where the assertion already has an exact counterpart elsewhere, the case is not repeated here:
- pure / ``read`` and idempotent steps re-run after a crash:
  ``tests/workflows/test_s6_workflow_journal.py::test_pure_and_idempotent_nodes_rerun_with_the_same_key``;
- a stale worker cannot commit after a newer lease:
  ``tests/workflows/test_s6_workflow_journal.py::test_a_stale_owner_cannot_commit_after_takeover``;
- cancellation schedules no further work: the service cancel route (B40), and the
  in-process task cancellation below.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from nexura_agents.contracts.execution import EffectClass, EffectStatus, ExecutionId
from nexura_agents.errors import (
    WorkflowMaxStepsError,
    WorkflowValidationError,
)

from nexura_runtime.workflow import (
    ModuleRegistry,
    SQLiteWorkflowStore,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
    step_idempotency_key,
)


class Crash(BaseException):
    """Stands in for the worker process dying: nothing in the engine catches it."""


class FakeBank:
    """A separate fake service that keeps its own record of applied transfers."""

    def __init__(self) -> None:
        self.transfers: list[dict[str, Any]] = []
        self.keys: list[str | None] = []
        self.crash_after_apply = False

    def transfer(self, state: dict[str, Any]) -> dict[str, Any]:
        self.transfers.append({"transfer_id": state["transfer_id"], "amount": state["amount"]})
        self.keys.append(step_idempotency_key())
        if self.crash_after_apply:
            self.crash_after_apply = False
            raise Crash  # the remote applied it; the worker died before recording that
        return {"receipt": f"ok-{state['transfer_id']}"}


@pytest.fixture
def store(tmp_path: Path) -> Any:
    sqlite = SQLiteWorkflowStore(tmp_path / "b05.sqlite3")
    yield sqlite
    sqlite.close_sync()


async def _journal(store: Any, run_id: str) -> Any:
    journal = await store.open_execution_store()
    return await journal.get(ExecutionId("default", run_id))


def _transfer_spec() -> WorkflowSpec:
    return WorkflowSpec(
        name="money-transfer",
        entry_node="check",
        nodes={
            "check": WorkflowNode(id="check", handler="check", default_transition="transfer"),
            "transfer": WorkflowNode(id="transfer", handler="transfer"),
        },
        terminal_nodes=["transfer"],
    )


def _transfer_registry(bank: FakeBank, check: Any = None) -> ModuleRegistry:
    registry = ModuleRegistry()
    registry.register("check", check or (lambda state: {"checked": True}), effect="pure")
    registry.register("transfer", bank.transfer)  # undeclared: non_idempotent
    return registry


async def test_crash_after_remote_success_is_never_repeated_silently(store: Any) -> None:
    """Crash after the remote applied a non-idempotent transfer, before its outcome was
    recorded: recovery pauses ``effect_unknown`` and the bank is not called a second time.
    A person confirms it, and the run completes with that result without a new call."""
    bank = FakeBank()
    spec = _transfer_spec()
    bank.crash_after_apply = True
    with pytest.raises(Crash):
        await WorkflowEngine(spec, _transfer_registry(bank), store=store).run(
            {"transfer_id": "TX-2", "amount": 2000}, workflow_run_id="tx-2"
        )
    assert len(bank.transfers) == 1

    engine = WorkflowEngine(spec, _transfer_registry(bank), store=store)
    paused = await engine.recover("tx-2")
    assert paused.status == "paused" and paused.pause_reason == "effect_unknown"
    assert len(bank.transfers) == 1  # crucial: not dispatched again
    snapshot = await _journal(store, "tx-2")
    (effect,) = snapshot.effects.values()
    assert effect.status == EffectStatus.INDETERMINATE
    assert effect.intent.effect_class == EffectClass.NON_IDEMPOTENT

    done = await engine.resolve_effect(
        "tx-2", "confirmed", actor="ops@example.test", result={"receipt": "per bank ledger"}
    )
    assert done.status == "completed" and done.state["receipt"] == "per bank ledger"
    assert len(bank.transfers) == 1


async def test_crash_before_dispatch_runs_the_step_once(store: Any) -> None:
    """The worker died before the transfer's intent was written (inside the step before
    it): nothing was dispatched, so recovery runs the transfer, exactly once."""
    bank = FakeBank()
    spec = _transfer_spec()
    crash = {"armed": True}

    def check(state: dict[str, Any]) -> dict[str, Any]:
        if crash["armed"]:
            crash["armed"] = False
            raise Crash
        return {"checked": True}

    with pytest.raises(Crash):
        await WorkflowEngine(spec, _transfer_registry(bank, check), store=store).run(
            {"transfer_id": "TX-1", "amount": 1000}, workflow_run_id="tx-1"
        )
    assert bank.transfers == []
    assert not [s for s in (await _journal(store, "tx-1")).steps.values() if s.intent]

    result = await WorkflowEngine(spec, _transfer_registry(bank, check), store=store).recover(
        "tx-1"
    )
    assert result.status == "completed" and result.state["receipt"] == "ok-TX-1"
    assert bank.transfers == [{"transfer_id": "TX-1", "amount": 1000}]


async def test_a_failed_non_idempotent_step_leaves_its_effect_unknown(store: Any) -> None:
    """A handler that raises may still have applied its effect (a lost response looks like
    a refusal): the run pauses and the effect remains resolvable."""
    calls: list[int] = []

    def flaky_transfer(state: dict[str, Any]) -> dict[str, Any]:
        calls.append(1)
        raise ConnectionResetError("gateway reset after POST")

    registry = ModuleRegistry()
    registry.register("check", lambda state: {"checked": True}, effect="pure")
    registry.register("transfer", flaky_transfer)
    engine = WorkflowEngine(_transfer_spec(), registry, store=store)
    paused = await engine.run({"transfer_id": "TX-3", "amount": 1}, workflow_run_id="tx-3")
    assert paused.status == "paused" and paused.pause_reason == "effect_unknown"
    snapshot = await _journal(store, "tx-3")
    assert snapshot.run.status == "paused"
    assert [e.status for e in snapshot.effects.values()] == [EffectStatus.INDETERMINATE]
    assert calls == [1]
    done = await engine.resolve_effect(
        "tx-3", "confirmed", actor="ops", result={"receipt": "ledger"}, reason="Ledger checked"
    )
    assert done.status == "completed" and done.state["receipt"] == "ledger"
    assert calls == [1]


async def test_a_timed_out_step_pauses_the_run_and_leaves_its_effect_unknown(store: Any) -> None:
    """The deadline is enforced on the final node too. A timed-out non-pure step may have
    reached the remote side, so its effect is ``indeterminate`` and the run pauses."""

    async def slow_transfer(state: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(2)
        return {"late": True}

    registry = ModuleRegistry()
    registry.register("check", lambda state: {"checked": True}, effect="pure")
    registry.register("transfer", slow_transfer)
    engine = WorkflowEngine(_transfer_spec(), registry, store=store, timeout=0.2)
    paused = await engine.run({"transfer_id": "TX-4", "amount": 1}, workflow_run_id="tx-4")
    assert paused.status == "paused" and paused.pause_reason == "effect_unknown"
    checkpoint = await store.get("tx-4")
    assert checkpoint.status == "paused" and "timeout" in (checkpoint.error or "")
    snapshot = await _journal(store, "tx-4")
    assert snapshot.run.status == "paused"
    assert [e.status for e in snapshot.effects.values()] == [EffectStatus.INDETERMINATE]
    assert "late" not in checkpoint.state


async def test_a_cancelled_worker_leaves_a_recoverable_run_that_pauses_on_takeover(
    store: Any,
) -> None:
    """Cancelling the worker task mid-step (shutdown) is treated as a crash: the intent
    stays dispatched with its effect ``pending``, and the next owner pauses the run
    ``effect_unknown`` instead of sending the transfer again."""
    entered, calls = asyncio.Event(), []

    async def transfer(state: dict[str, Any]) -> dict[str, Any]:
        calls.append(1)
        entered.set()
        await asyncio.sleep(30)
        return {"receipt": "never"}

    registry = ModuleRegistry()
    registry.register("check", lambda state: {"checked": True}, effect="pure")
    registry.register("transfer", transfer)
    spec = _transfer_spec()
    task = asyncio.create_task(
        WorkflowEngine(spec, registry, store=store).run(
            {"transfer_id": "TX-5", "amount": 5}, workflow_run_id="tx-5"
        )
    )
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    snapshot = await _journal(store, "tx-5")
    assert snapshot.run.status == "running" and snapshot.run.lease_owner is None
    assert [e.status for e in snapshot.effects.values()] == [EffectStatus.PENDING]

    paused = await WorkflowEngine(spec, registry, store=store).recover("tx-5")
    assert paused.status == "paused" and paused.pause_reason == "effect_unknown"
    assert calls == [1]


async def test_the_step_limit_is_counted_from_storage_across_recovery(store: Any) -> None:
    """Spent steps survive a restart: a new engine continues the count from the
    checkpoint, so recovery cannot grant a fresh step budget. (The transfer is declared
    idempotent here: an in-doubt non-idempotent step pauses before any limit applies.)"""
    bank = FakeBank()
    spec = _transfer_spec()

    def registry() -> ModuleRegistry:
        reg = ModuleRegistry()
        reg.register("check", lambda state: {"checked": True}, effect="pure")
        reg.register("transfer", bank.transfer, effect="idempotent")
        return reg

    bank.crash_after_apply = True
    with pytest.raises(Crash):
        await WorkflowEngine(spec, registry(), store=store, max_steps=5).run(
            {"transfer_id": "TX-6", "amount": 6}, workflow_run_id="tx-6"
        )
    assert (await store.get("tx-6")).step_count == 1  # "check" is done

    restarted = WorkflowEngine(spec, registry(), store=store, max_steps=1)
    with pytest.raises(WorkflowMaxStepsError) as exceeded:
        await restarted.recover("tx-6")
    assert (exceeded.value.step_count, exceeded.value.max_steps) == (1, 1)
    assert len(bank.transfers) == 1  # refused before the step ran again
    assert (await store.get("tx-6")).status == "failed"


async def test_a_loop_node_is_a_new_visit_with_its_own_intent_and_key(store: Any) -> None:
    """A conditional cycle re-executes its node: each visit is a new step with its own
    intent and idempotency key, and a fresh engine continues the count after a restart."""
    keys: list[str | None] = []
    crash = {"on_visit": 2}

    def loop(state: dict[str, Any]) -> dict[str, Any]:
        count = state.get("count", 0) + 1
        keys.append(step_idempotency_key())
        if crash["on_visit"] == count:
            crash["on_visit"] = 0
            raise Crash
        return {"count": count, "_branch": "done" if count == 2 else "again"}

    registry = ModuleRegistry()
    registry.register("loop", loop, effect="idempotent")
    registry.register("end", lambda state: {}, effect="pure")
    spec = WorkflowSpec(
        name="cycle",
        entry_node="loop",
        nodes={
            "loop": WorkflowNode(
                id="loop", handler="loop", transitions={"again": "loop", "done": "end"}
            ),
            "end": WorkflowNode(id="end", handler="end"),
        },
        terminal_nodes=["end"],
    )
    with pytest.raises(Crash):
        await WorkflowEngine(spec, registry, store=store).run({}, workflow_run_id="cycle")
    result = await WorkflowEngine(spec, registry, store=store).recover("cycle")
    assert (result.status, result.state["count"]) == ("completed", 2)
    # Visit 1, visit 2 (crashed), visit 2 again after recovery with the same key.
    assert len(keys) == 3 and keys[0] != keys[1] and keys[1] == keys[2]
    intents = [s.intent for s in (await _journal(store, "cycle")).steps.values() if s.intent]
    assert len({i.idempotency_key for i in intents}) == 2


async def test_a_missing_handler_is_refused_before_anything_is_journaled(store: Any) -> None:
    registry = ModuleRegistry()
    registry.register("check", lambda state: {"checked": True}, effect="pure")
    with pytest.raises(WorkflowValidationError):
        WorkflowEngine(_transfer_spec(), registry, store=store)
    journal = await store.open_execution_store()
    with pytest.raises(KeyError):
        await journal.get(ExecutionId("default", "never"))
