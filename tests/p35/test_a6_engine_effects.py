"""Offline S3/S4 journal integration; all effects are explicitly local fake operations."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from nexura_agents.contracts.errors import IndeterminateEffectError
from nexura_agents.contracts.execution import (
    DefinitionMismatchError,
    EffectResolution,
    EffectStatus,
    ExecutionId,
)
from nexura_agents.errors import SessionConflictError
from nexura_agents.execution_store import (
    EffectAlreadyResolvedError,
    ExecutionConflictError,
    MemoryExecutionStore,
)

from nexura_runtime.runtime.effects import EffectReconciler
from nexura_runtime.runtime.execution import (
    DurableExecutionEngine,
    DurableNodeSpec,
    DurableWorkflowSpec,
)
from nexura_runtime.storage import SQLiteRepository


class LocalFakeCrash(BaseException):
    """Stop after an actual intent commit, before invoking the local fake handler."""


class CrashStore(MemoryExecutionStore):
    crash = True

    async def commit(self, *args: Any, **kwargs: Any) -> Any:
        result = await super().commit(*args, **kwargs)
        if self.crash and any(e.status == EffectStatus.PENDING for e in kwargs.get("effects", [])):
            self.crash = False
            raise LocalFakeCrash()
        return result


def spec(policy: str = "non_idempotent_write") -> DurableWorkflowSpec:
    return DurableWorkflowSpec(
        workflow_id="fake-payment",
        entry_node="pay",
        terminal_nodes=["pay"],
        nodes={"pay": DurableNodeSpec(node_id="pay", handler="fake-pay", effect_policy=policy)},
    )


async def paused_engine(store: CrashStore) -> tuple[DurableExecutionEngine, str]:
    engine = DurableExecutionEngine(store, {"fake-pay": lambda _: {"fake": True}})
    await engine.init_run("ws", "run", spec())
    await engine.claim_run("ws", "run", "original", lease_duration=0.025)
    with pytest.raises(LocalFakeCrash):
        await engine.execute_step("ws", "run", "original", spec())
    await asyncio.sleep(0.05)
    await engine.claim_run("ws", "run", "takeover")
    with pytest.raises(IndeterminateEffectError):
        await engine.execute_step("ws", "run", "takeover", spec())
    state = await engine.get_state("ws", "run")
    assert state.status == "paused" and state.pause_reason == "effect_unknown"
    snapshot = await store.get(ExecutionId("ws", "run"))
    assert snapshot.run.lease_owner is None
    effect = next(iter(snapshot.effects.values()))
    assert effect.status == EffectStatus.INDETERMINATE
    return engine, effect.effect_id


@pytest.mark.parametrize("resolution", list(EffectResolution))
async def test_unknown_pauses_and_resolution_continues_once(resolution: EffectResolution) -> None:
    store = CrashStore()
    engine, effect_id = await paused_engine(store)
    calls = 0

    async def local_fake(_: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"fake": True}

    engine.register_handler("fake-pay", local_fake)
    resolved = await engine.resolve_effect(
        "ws", "run", effect_id, resolution, actor="operator", result={"confirmed_fake": True}
    )
    assert resolved.resolved_by == "operator"
    with pytest.raises(EffectAlreadyResolvedError):
        await engine.resolve_effect("ws", "run", effect_id, resolution, actor="second")
    state = await engine.run_until_idle("ws", "run", "resumer", spec())
    assert state.status == ("failed" if resolution == EffectResolution.FAILED else "completed")
    assert calls == (1 if resolution == EffectResolution.NOT_APPLIED else 0)
    if resolution == EffectResolution.CONFIRMED:
        assert state.state["confirmed_fake"] is True
    snapshot = await store.get(ExecutionId("ws", "run"))
    assert snapshot.effects[effect_id].resolution == resolution
    assert snapshot.events[-1].sequence == len(snapshot.events)
    assert any(event.payload.get("actor") == "operator" for event in snapshot.events)


async def test_context_carries_stable_key_and_journal_fencing_token() -> None:
    """The legacy engine does not heartbeat: lease_duration bounds a step, as before S3."""
    store = MemoryExecutionStore()
    from nexura_agents.contracts.context import ExecutionContext

    keys = []

    async def local_fake(ctx: ExecutionContext, _: dict[str, Any]) -> dict[str, Any]:
        keys.append(ctx.attributes["idempotency_key"])
        digest = (await store.get(ExecutionId("ws", "run"))).run.definition_digest
        # The owner's lease is live while the handler runs, so a competitor is refused.
        assert (
            await store.claim(ExecutionId("ws", "run"), "competitor", 1, definition_digest=digest)
            is None
        )
        return {"local": True}

    engine = DurableExecutionEngine(store, {"fake-pay": local_fake})
    await engine.init_run("ws", "run", spec("idempotent_write"))
    await engine.claim_run("ws", "run", "owner")
    ctx = ExecutionContext(workspace_id="ws", run_id="run", node_id="pay")
    state = await engine.execute_step("ws", "run", "owner", spec("idempotent_write"), ctx)
    assert state.status == "completed" and keys[0].startswith("nx-")
    snapshot = await store.get(ExecutionId("ws", "run"))
    effect = next(iter(snapshot.effects.values()))
    assert keys == [effect.intent.idempotency_key]
    assert effect.status == EffectStatus.CONFIRMED and effect.result == {"local": True}
    assert ctx.attributes["fencing_token"] == snapshot.run.fencing_token


async def test_expired_lease_refuses_completion_and_lets_another_worker_take_over() -> None:
    from nexura_runtime.runtime.execution import LeaseExpiredError

    store = MemoryExecutionStore()

    async def slow(_: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(0.08)
        return {"late": True}

    engine = DurableExecutionEngine(store, {"fake-pay": slow})
    await engine.init_run("ws", "run", spec("idempotent_write"))
    await engine.claim_run("ws", "run", "owner", lease_duration=0.03)
    with pytest.raises(LeaseExpiredError):
        await engine.execute_step("ws", "run", "owner", spec("idempotent_write"))
    snapshot = await store.get(ExecutionId("ws", "run"))
    assert snapshot.run.status == "running" and "late" not in snapshot.run.state["state"]
    effect = next(iter(snapshot.effects.values()))
    assert effect.status == EffectStatus.PENDING
    engine.register_handler("fake-pay", lambda _: {"retried": True})
    done = await engine.run_until_idle("ws", "run", "taker", spec("idempotent_write"))
    assert done.status == "completed" and done.state["retried"] is True
    after = await store.get(ExecutionId("ws", "run"))
    # Same visit, same key: the idempotent retry reused the persisted intent.
    assert list(after.effects) == [effect.effect_id]
    assert after.effects[effect.effect_id].status == EffectStatus.CONFIRMED


async def test_failed_handler_pauses_effect_unknown_and_stays_resolvable() -> None:
    """Review M3a: a raising non-pure handler may have applied its effect, so the run
    pauses effect_unknown (founder decision 2) instead of failing unresolvably."""
    store = MemoryExecutionStore()
    calls: list[str] = []

    def broken(_: dict[str, Any]) -> dict[str, Any]:
        calls.append("sent")
        raise ConnectionError("local fake connection reset")

    engine = DurableExecutionEngine(store, {"fake-pay": broken})
    await engine.init_run("ws", "run", spec())
    await engine.claim_run("ws", "run", "owner")
    with pytest.raises(ConnectionError):
        await engine.execute_step("ws", "run", "owner", spec())
    snapshot = await store.get(ExecutionId("ws", "run"))
    assert snapshot.run.status == "paused" and snapshot.run.lease_owner is None
    assert [e.status for e in snapshot.effects.values()] == [EffectStatus.INDETERMINATE]
    step = next(iter(snapshot.steps.values()))
    assert step.intent is not None and step.outcome is None
    state = await engine.get_state("ws", "run")
    assert state.status == "paused" and state.pause_reason == "effect_unknown"
    (effect_id,) = snapshot.effects
    assert state.pending_effect is not None
    assert state.pending_effect["intent"]["idempotency_key"] == (
        snapshot.effects[effect_id].intent.idempotency_key
    )
    assert "local fake connection reset" in (state.error or "")

    await engine.resolve_effect(
        "ws", "run", effect_id, EffectResolution.CONFIRMED, actor="person", result={"ok": 1}
    )
    done = await engine.run_until_idle("ws", "run", "resumer", spec())
    assert done.status == "completed" and done.state["ok"] == 1
    assert calls == ["sent"]  # the confirmed effect was not sent again


async def test_failed_pure_handler_still_fails_the_run() -> None:
    store = MemoryExecutionStore()

    def broken(_: dict[str, Any]) -> dict[str, Any]:
        raise ValueError("local fake bad input")

    engine = DurableExecutionEngine(store, {"fake-pay": broken})
    await engine.init_run("ws", "run", spec("pure"))
    await engine.claim_run("ws", "run", "owner")
    with pytest.raises(ValueError):
        await engine.execute_step("ws", "run", "owner", spec("pure"))
    snapshot = await store.get(ExecutionId("ws", "run"))
    assert snapshot.run.status == "failed" and not snapshot.effects
    assert (await engine.get_state("ws", "run")).status == "failed"


async def test_cancelled_worker_leaves_a_recoverable_run_that_pauses_on_takeover() -> None:
    """Review M3b: task cancellation is crash semantics (no commit); after the lease
    expires the next worker pauses effect_unknown instead of re-sending."""
    store = MemoryExecutionStore()
    started = asyncio.Event()
    calls: list[str] = []

    async def slow(_: dict[str, Any]) -> dict[str, Any]:
        calls.append("sent")
        started.set()
        await asyncio.sleep(30)
        return {}

    engine = DurableExecutionEngine(store, {"fake-pay": slow})
    await engine.init_run("ws", "run", spec())
    await engine.claim_run("ws", "run", "owner", lease_duration=0.05)
    task = asyncio.create_task(engine.execute_step("ws", "run", "owner", spec()))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    snapshot = await store.get(ExecutionId("ws", "run"))
    assert snapshot.run.status == "running"
    assert [e.status for e in snapshot.effects.values()] == [EffectStatus.PENDING]
    await asyncio.sleep(0.1)  # the cancelled worker's lease expires
    await engine.claim_run("ws", "run", "taker")
    with pytest.raises(IndeterminateEffectError):
        await engine.execute_step("ws", "run", "taker", spec())
    state = await engine.get_state("ws", "run")
    assert state.status == "paused" and state.pause_reason == "effect_unknown"
    assert calls == ["sent"]


async def test_cancelling_a_dispatched_run_marks_the_effect_indeterminate() -> None:
    """Review L5: cancellation ends the run; the in-flight effect becomes indeterminate
    (not pending), and the handler's late completion is refused."""
    store = MemoryExecutionStore()
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(_: dict[str, Any]) -> dict[str, Any]:
        started.set()
        await release.wait()
        return {"late": True}

    engine = DurableExecutionEngine(store, {"fake-pay": slow})
    await engine.init_run("ws", "run", spec())
    await engine.claim_run("ws", "run", "owner")
    task = asyncio.create_task(engine.execute_step("ws", "run", "owner", spec()))
    await asyncio.wait_for(started.wait(), 5)
    cancelled = await engine.request_cancellation("ws", "run")
    assert cancelled.status == "cancelled"
    release.set()
    with pytest.raises(SessionConflictError):
        await task
    snapshot = await store.get(ExecutionId("ws", "run"))
    (effect,) = snapshot.effects.values()
    assert effect.status == EffectStatus.INDETERMINATE
    assert "late" not in snapshot.run.state["state"]
    assert (await engine.get_state("ws", "run")).status == "cancelled"


async def test_missing_handler_records_no_dispatch() -> None:
    from nexura_runtime.runtime.execution import DurableExecutionError

    store = MemoryExecutionStore()
    engine = DurableExecutionEngine(store)
    await engine.init_run("ws", "run", spec())
    await engine.claim_run("ws", "run", "owner")
    with pytest.raises(DurableExecutionError, match="not registered"):
        await engine.execute_step("ws", "run", "owner", spec())
    snapshot = await store.get(ExecutionId("ws", "run"))
    assert snapshot.run.status == "failed" and not snapshot.effects and not snapshot.steps


async def test_resolve_effect_errors_for_unknown_run_and_effect() -> None:
    from nexura_agents.errors import StorageError

    from nexura_runtime.runtime.execution import DurableExecutionError

    store = CrashStore()
    engine, _ = await paused_engine(store)
    with pytest.raises(StorageError):
        await engine.resolve_effect("ws", "missing", "x", EffectResolution.FAILED, actor="op")
    with pytest.raises(DurableExecutionError, match="not found"):
        await engine.resolve_effect("ws", "run", "missing", EffectResolution.FAILED, actor="op")


async def test_definition_changes_fail_before_dispatch(tmp_path: Path) -> None:
    repo = SQLiteRepository(tmp_path / "digest.sqlite3")
    try:
        engine = DurableExecutionEngine(repo)
        await engine.init_run("ws", "run", spec())
        await engine.claim_run("ws", "run", "owner")
        changed = spec()
        changed.nodes["pay"].handler = "different"
        with pytest.raises(DefinitionMismatchError):
            await engine.execute_step("ws", "run", "owner", changed)
        assert not (
            await (await engine.get_execution_store()).get(ExecutionId("ws", "run"))
        ).effects
    finally:
        await repo.close()


async def test_expected_revision_is_checked_inside_fenced_commit() -> None:
    store = MemoryExecutionStore()
    run = await store.create(ExecutionId("ws", "run"), owner="worker", definition_digest="v1")
    await store.commit(run.lease, state={"winner": True}, expected_revision=run.revision)
    before = await store.get(run.execution_id)
    with pytest.raises(ExecutionConflictError):
        await store.commit(run.lease, state={"stale": True}, expected_revision=run.revision)
    assert await store.get(run.execution_id) == before


def test_deprecated_wrapper_uses_journal_and_records_actor(tmp_path: Path) -> None:
    path = tmp_path / "wrapper.sqlite3"
    with pytest.warns(DeprecationWarning):
        wrapper = EffectReconciler(path)
    try:
        wrapper.record_intent("ws", "legacy-id", "run", "fake-pay", "key", {"fake": True})
        wrapper.record_indeterminate("ws", "legacy-id")
        result = wrapper.reconcile_effect(
            "ws", "legacy-id", "rolled_back", "not sent", actor="human"
        )
        assert result.status == "reconciled"
        with sqlite3.connect(path) as db:
            row = db.execute(
                "SELECT status, resolution, resolved_by FROM nexura_exec_effects"
            ).fetchone()
            assert row == ("resolved", "not_applied", "human")
            assert (
                db.execute("SELECT name FROM sqlite_master WHERE name='effect_records'").fetchone()
                is None
            )
    finally:
        wrapper.close()


def test_wrapper_imports_pre_0_5_effect_records_once(tmp_path: Path) -> None:
    from nexura_runtime.storage.migrations import run_sqlite_migrations

    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as db:
        run_sqlite_migrations(db)
        rows = [
            ("pending-1", "pending", None, None),
            ("confirmed-1", "confirmed", '{"order": "local-fake-1"}', None),
            ("unknown-1", "indeterminate", None, "INDETERMINATE: local fake reset"),
            ("rolled-1", "reconciled", None, "RECONCILED [ROLLED_BACK]: not on the fake ledger"),
        ]
        for effect_id, status, response, notes in rows:
            db.execute(
                "INSERT INTO effect_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "ws",
                    effect_id,
                    "run",
                    "fake-pay",
                    "key-" + effect_id,
                    "digest",
                    status,
                    1.0,
                    response,
                    notes,
                    1.0,
                    1.0,
                ),
            )
    for _ in range(2):  # the second open finds everything imported already
        with pytest.warns(DeprecationWarning):
            wrapper = EffectReconciler(path)
        try:
            pending = wrapper.get_effect("ws", "pending-1")
            assert pending is not None and pending.status == "pending"
            assert pending.payload_digest == "digest" and pending.dispatched_at == 1.0
            confirmed = wrapper.get_effect("ws", "confirmed-1")
            assert confirmed is not None and confirmed.status == "confirmed"
            assert confirmed.response_payload == {"order": "local-fake-1"}
            unknown = wrapper.get_effect("ws", "unknown-1")
            assert unknown is not None and unknown.status == "indeterminate"
            assert unknown.reconciliation_notes == "INDETERMINATE: local fake reset"
            rolled = wrapper.get_effect("ws", "rolled-1")
            assert rolled is not None and rolled.status == "reconciled"
            assert rolled.reconciliation_notes == "RECONCILED [ROLLED_BACK]: not on the fake ledger"
        finally:
            wrapper.close()
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT resolution FROM nexura_exec_effects WHERE status = 'resolved'"
        ).fetchall() == [("not_applied",)]
        assert db.execute("SELECT COUNT(*) FROM effect_records").fetchone() == (4,)


def test_wrapper_refuses_duplicates_and_second_resolution(tmp_path: Path) -> None:
    from nexura_runtime.runtime.effects import EffectError, ReconciliationError

    with pytest.warns(DeprecationWarning):
        wrapper = EffectReconciler(tmp_path / "dup.sqlite3")
    try:
        wrapper.record_intent("ws", "a", "run", "fake-pay", "same-key", {"n": 1})
        with pytest.raises(EffectError, match="already recorded"):
            wrapper.record_intent("ws", "a", "run", "fake-pay", "other-key", {"n": 1})
        with pytest.raises(EffectError, match="idempotency keys are unique"):
            wrapper.record_intent("ws", "b", "run", "fake-pay", "same-key", {"n": 2})
        assert wrapper.get_effect("ws", "b") is None
        # The refused alias can still be recorded with its own key.
        assert wrapper.record_intent("ws", "b", "run", "fake-pay", "b-key", {"n": 2}).status == (
            "pending"
        )
        wrapper.record_indeterminate("ws", "a")
        wrapper.reconcile_effect("ws", "a", "voided", "local fake says no", actor="person")
        with pytest.raises(ReconciliationError):
            wrapper.reconcile_effect("ws", "a", "confirmed", "again", actor="other")
        with pytest.raises(ReconciliationError):  # confirmed requires a pending effect
            wrapper.record_confirmed("ws", "a", {"late": True})
    finally:
        wrapper.close()


def test_wrapper_commits_on_a_borrowed_autocommit_connection(tmp_path: Path) -> None:
    path = tmp_path / "borrowed.sqlite3"
    conn = sqlite3.connect(path, autocommit=True)
    try:
        with pytest.warns(DeprecationWarning):
            wrapper = EffectReconciler(conn)
        wrapper.record_intent("ws", "e", "run", "fake-pay", "key", {"fake": True})
        wrapper.close()
        assert not conn.in_transaction  # close() leaves the borrowed connection open
        with sqlite3.connect(path) as other:
            assert other.execute("SELECT COUNT(*) FROM nexura_exec_effects").fetchone() == (1,)
    finally:
        conn.close()


async def test_plain_repository_runs_neither_open_nor_migrate_the_journal(tmp_path: Path) -> None:
    repo = SQLiteRepository(tmp_path / "plain.sqlite3")
    try:
        created = await repo.create_run("ws", "plain", initial_state={"step": 0})
        updated = await repo.update_run("ws", "plain", created.revision, "step1", {"step": 1})
        assert updated.status == "step1" and (await repo.get_run("ws", "plain")) is not None
        assert getattr(repo, "_execution_store", None) is None
        with sqlite3.connect(tmp_path / "plain.sqlite3") as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master")}
        assert "nexura_exec_runs" not in tables
    finally:
        await repo.close()


async def test_a_second_repository_sees_engine_runs_and_refuses_finished_updates(
    tmp_path: Path,
) -> None:
    from nexura_agents.errors import StorageError

    path = tmp_path / "shared.sqlite3"
    writer = SQLiteRepository(path)
    try:
        engine = DurableExecutionEngine(writer, {"fake-pay": lambda _: {"fake": True}})
        await engine.init_run("ws", "run", spec("pure"))
        done = await engine.run_until_idle("ws", "run", "worker", spec("pure"))
        assert done.status == "completed"
    finally:
        await writer.close()
    reader = SQLiteRepository(path)  # a fresh process would start like this
    try:
        record = await reader.get_run("ws", "run")
        assert record is not None and record.status == "completed"
        assert [e.event_type for e in await reader.get_run_events("ws", "run")][-1] == (
            "workflow_completed"
        )
        with pytest.raises(StorageError, match="finished"):
            await reader.update_run("ws", "run", record.revision, "running")
    finally:
        await reader.close()
