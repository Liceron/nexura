"""A6 S12: the legacy durable-execution paths are retired, with errors that name the replacement.

- Importing a removed name (``DurableExecutionEngine``, ``DurableWorkflowSpec``,
  ``EffectReconciler``...) raises ``RemovedAPIError``, an ``ImportError`` saying what to use.
- Runs the removed engine left in the execution journal (``kind="durable_graph"``) stay
  readable through the storage repository; updating one is refused with a message.
- Assertions moved here from the deleted engine tests where the behaviour still exists on
  the replacement path (noted per test).

All effects are local fakes; no network or provider is involved.
"""

from __future__ import annotations

import importlib
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from _legacy_journal import seed_legacy
from nexura_agents.contracts.execution import (
    EffectRecord,
    EffectResolution,
    EffectStatus,
    ExecutionId,
    StepIntent,
    StepKey,
    default_idempotency_key,
)
from nexura_agents.errors import StorageError, WorkflowSpecMismatchError
from nexura_agents.execution_store import SQLiteExecutionStore

from nexura_runtime._removed import RemovedAPIError
from nexura_runtime.storage import SQLiteRepository
from nexura_runtime.workflow import (
    ModuleRegistry,
    SQLiteWorkflowStore,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
)

ENGINE_NAMES = (
    "DurableExecutionEngine",
    "DurableExecutionModule",
    "DurableRunState",
    "DurableWorkflowSpec",
    "DurableNodeSpec",
    "FencingConflictError",
    "LeaseExpiredError",
)
REMOVED = [
    *(("nexura_runtime", name) for name in ENGINE_NAMES),
    *(("nexura_runtime.runtime", name) for name in (*ENGINE_NAMES, "DurableExecutionError")),
    *(
        ("nexura_runtime.runtime.execution", name)
        for name in (
            *ENGINE_NAMES,
            "DurableExecutionError",
            "NodeIntent",
            "WorkerLease",
            "EffectPolicy",
            "DurableDefinitionMismatchError",
        )
    ),
    ("nexura_runtime.runtime.execution.engine", "DurableExecutionEngine"),
    ("nexura_runtime.runtime.execution.models", "DurableWorkflowSpec"),
    ("nexura_runtime.runtime.execution.models", "NodeIntent"),
    ("nexura_runtime.runtime.execution.module", "DurableExecutionModule"),
    ("nexura_runtime.runtime.execution.errors", "FencingConflictError"),
    *(
        ("nexura_runtime.runtime.effects", name)
        for name in (
            "EffectReconciler",
            "EffectRecord",
            "EffectStatus",
            "EffectError",
            "EffectIndeterminateError",
            "ReconciliationError",
        )
    ),
    ("nexura_runtime.runtime.effects.reconciler", "EffectReconciler"),
    ("nexura_runtime.runtime.effects.models", "EffectRecord"),
    ("nexura_runtime.runtime.effects.errors", "ReconciliationError"),
]


@pytest.mark.parametrize(("module", "name"), REMOVED, ids=[f"{m}.{n}" for m, n in REMOVED])
def test_a_removed_name_raises_an_import_error_naming_the_replacement(
    module: str, name: str
) -> None:
    namespace: dict[str, Any] = {}
    with pytest.raises(RemovedAPIError) as raised:
        exec(f"from {module} import {name}", namespace)  # noqa: S102 - fixed test strings
    message = str(raised.value)
    assert isinstance(raised.value, ImportError)
    assert f"{module}.{name} was removed" in message
    assert "docs/durable-execution.md" in message
    # Every message points at something that exists today.
    assert any(
        target in message
        for target in ("WorkflowEngine", "execution_store", "contracts.execution", "ExecutionLease")
    ), message


def test_removed_names_are_not_listed_and_other_names_stay_attribute_errors() -> None:
    import nexura_runtime
    import nexura_runtime.runtime as runtime_pkg

    for module in (nexura_runtime, runtime_pkg):
        assert "DurableExecutionEngine" not in getattr(module, "__all__", [])
        assert "DurableExecutionEngine" not in dir(module)
    with pytest.raises(AttributeError):
        _ = nexura_runtime.NoSuchThing  # type: ignore[attr-defined]
    execution = importlib.import_module("nexura_runtime.runtime.execution")
    assert execution.__all__ == []
    with pytest.raises(AttributeError):
        _ = execution.NoSuchThing
    # The rest of nexura_runtime.runtime still imports.
    assert runtime_pkg.AgentCoordinator is not None


async def _durable_graph_row(db: Path) -> str:
    """A run the removed engine left in a database: journal kind ``durable_graph``."""
    store = SQLiteExecutionStore(db)
    await store.init_db()
    try:
        snapshot = await seed_legacy(store)
        return next(iter(snapshot.effects))
    finally:
        await store.close()


async def test_a_legacy_durable_graph_run_stays_readable_and_is_refused_for_update(
    tmp_path: Path,
) -> None:
    """No data loss for a run the removed engine left behind: the repository still shows it
    and its events, its effect can still be resolved in the journal, and a repository update
    (what only the removed engine did) is refused with the replacement named.

    Moved from ``test_a6_engine_effects.py::
    test_a_second_repository_sees_engine_runs_and_refuses_finished_updates``."""
    db = tmp_path / "legacy.sqlite3"
    effect_id = await _durable_graph_row(db)
    repo = SQLiteRepository(db)
    try:
        record = await repo.get_run("ws", "legacy-run")
        assert record is not None and record.status == "paused"
        assert record.input_data == {"amount": 5}
        events = [e.event_type for e in await repo.get_run_events("ws", "legacy-run")]
        assert events[-1] == "run_paused"
        with pytest.raises(StorageError, match="removed DurableExecutionEngine") as refused:
            await repo.update_run("ws", "legacy-run", record.revision, "running")
        assert "WorkflowEngine" in str(refused.value)
        assert "resolve_effect" in str(refused.value)
        after = await repo.get_run("ws", "legacy-run")
        assert after is not None and after.revision == record.revision
    finally:
        await repo.close()

    store = SQLiteExecutionStore(db)
    try:
        identity = ExecutionId("ws", "legacy-run")
        snapshot = await store.get(identity)
        lease = await store.claim(identity, "ops", 30, definition_digest="legacy-digest")
        assert lease is not None
        resolved = await store.resolve_effect(
            lease, effect_id, EffectResolution.FAILED, actor="ops@example.test"
        )
        assert (
            resolved.status is EffectStatus.RESOLVED and resolved.resolved_by == "ops@example.test"
        )
        assert snapshot.run.kind == "durable_graph"
    finally:
        await store.close()


async def test_plain_repository_runs_neither_open_nor_migrate_the_journal(tmp_path: Path) -> None:
    """Moved unchanged from ``test_a6_engine_effects.py`` (the read-only bridge stays)."""
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


async def test_completed_legacy_row_reopens_with_its_final_event_and_refuses_updates(
    tmp_path: Path,
) -> None:
    db = tmp_path / "completed-legacy.sqlite3"
    store = SQLiteExecutionStore(db)
    await seed_legacy(store, "completed")
    before = await store.get(ExecutionId("ws", "legacy-run"))
    await store.close()
    repo = SQLiteRepository(db)
    try:
        record = await repo.get_run("ws", "legacy-run")
        assert record is not None and record.status == "completed"
        assert record.state["status"] == "completed"
        events = await repo.get_run_events("ws", "legacy-run")
        assert events[-1].event_type == "run_completed"
        with pytest.raises(StorageError, match="read-only"):
            await repo.update_run("ws", "legacy-run", record.revision, "running", {"bad": True})
    finally:
        await repo.close()
    reopened = SQLiteExecutionStore(db)
    try:
        assert await reopened.get(ExecutionId("ws", "legacy-run")) == before
    finally:
        await reopened.close()


class Crash(BaseException):
    """Stands in for the worker process dying."""


async def test_a_changed_handler_is_refused_before_dispatch_on_recover(tmp_path: Path) -> None:
    """Moved from ``tests/backend/b32/test_expired_completion.py::
    test_changed_workflow_handler_rejected_before_dispatch``: a run is continued only with
    the definition it started with; the changed handler never runs."""
    effects: list[str] = []
    registry = ModuleRegistry()

    def original(state: dict[str, Any]) -> dict[str, Any]:
        raise Crash

    registry.register("original", original, effect="pure")
    registry.register("changed", lambda state: effects.append("changed") or {}, effect="pure")
    spec = WorkflowSpec(
        name="fixed",
        entry_node="action",
        nodes={"action": WorkflowNode(id="action", handler="original")},
        terminal_nodes=["action"],
    )
    store = SQLiteWorkflowStore(tmp_path / "fixed.sqlite3")
    try:
        with pytest.raises(Crash):
            await WorkflowEngine(spec, registry, store=store).run({}, workflow_run_id="fixed")
        changed = WorkflowSpec.from_dict(spec.to_dict())
        changed.nodes["action"].handler = "changed"
        with pytest.raises(WorkflowSpecMismatchError):
            await WorkflowEngine(changed, registry, store=store).recover("fixed")
        assert effects == []
    finally:
        store.close_sync()


async def test_two_workers_contend_for_one_lease_and_only_the_holder_commits(
    tmp_path: Path,
) -> None:
    """Moved from ``tests/backend/b32/test_release_gate.py::test_two_worker_contention_and_fencing``
    onto ``SQLiteExecutionStore``: while alpha holds the lease beta's claim is refused, and
    alpha's fenced commit is the one that lands."""
    store = SQLiteExecutionStore(tmp_path / "contention.sqlite3")
    await store.init_db()
    try:
        identity = ExecutionId("ws_prod", "run_contention_001")
        created = await store.create(identity, owner="initializer", definition_digest="d", state={})
        await store.release(created.lease)
        alpha = await store.claim(identity, "worker_alpha", 30, definition_digest="d")
        assert alpha is not None and alpha.fencing_token > created.lease.fencing_token
        assert await store.claim(identity, "worker_beta", 30, definition_digest="d") is None
        await store.commit(alpha, state={"processed_by": "worker_alpha"}, status="completed")
        snapshot = await store.get(identity)
        assert snapshot.run.status == "completed"
        assert snapshot.run.state == {"processed_by": "worker_alpha"}
    finally:
        await store.close()


async def test_a_lost_response_stays_indeterminate_until_resolved_once(tmp_path: Path) -> None:
    """Moved from ``tests/backend/b07/test_approvals_and_effects.py::
    test_lost_response_stays_indeterminate`` (``EffectReconciler``) onto the journal:
    the intent is recorded before dispatch, a lost response leaves the effect
    ``indeterminate`` across a reopen, a person resolves it once with an audited actor,
    and a second resolution is refused."""
    from nexura_agents.execution_store import EffectAlreadyResolvedError

    db = tmp_path / "effects.sqlite3"
    identity = ExecutionId("ws_brokerage", "run_trade_88")
    key = StepKey.workflow("place_order", 0)
    intent = StepIntent(key, "payload-digest", "idem_key_881")
    effect = EffectRecord(identity, intent)
    store = SQLiteExecutionStore(db)
    await store.init_db()
    try:
        run = await store.create(identity, owner="worker", definition_digest="d", state={})
        await store.commit(run.lease, state={}, steps=[intent], effects=[effect])
        # The gateway reset the connection after the POST: the outcome is unknown.
        await store.commit(
            run.lease,
            state={"pause_reason": "effect_unknown"},
            status="paused",
            effects=[EffectRecord(identity, intent, status=EffectStatus.INDETERMINATE)],
        )
    finally:
        await store.close()

    reopened = SQLiteExecutionStore(db)  # a fresh process
    try:
        snapshot = await reopened.get(identity)
        assert snapshot.effects[effect.effect_id].status == EffectStatus.INDETERMINATE
        assert snapshot.effects[effect.effect_id].intent.idempotency_key == "idem_key_881"
        lease = await reopened.claim(identity, "ops", 30, definition_digest="d")
        assert lease is not None
        resolved = await reopened.resolve_effect(
            lease,
            effect.effect_id,
            EffectResolution.CONFIRMED,
            actor="ops@example.test",
            result={"order": "ORD-772"},
        )
        assert resolved.status is EffectStatus.RESOLVED
        assert resolved.resolution is EffectResolution.CONFIRMED
        audit = (await reopened.get(identity)).events[-1]
        assert (
            audit.event_type == "effect_resolved" and audit.payload["actor"] == "ops@example.test"
        )
        again = await reopened.claim(identity, "ops", 30, definition_digest="d")
        assert again is not None
        with pytest.raises(EffectAlreadyResolvedError):
            await reopened.resolve_effect(
                again, effect.effect_id, EffectResolution.FAILED, actor="ops@example.test"
            )
    finally:
        await reopened.close()


async def test_a_borrowed_autocommit_connection_is_committed_and_left_open(tmp_path: Path) -> None:
    """Moved from ``test_a6_engine_effects.py::test_wrapper_commits_on_a_borrowed_autocommit_
    connection`` (the removed ``EffectReconciler`` borrowed the caller's connection): a store
    on a borrowed autocommit connection commits its writes, ``close()`` leaves the connection
    open and idle, and a connection that is not idle is refused."""
    path = tmp_path / "borrowed.sqlite3"
    conn = sqlite3.connect(path, autocommit=True)
    try:
        store = SQLiteExecutionStore.from_connection(conn)
        await store.init_db()
        identity = ExecutionId("ws", "borrowed")
        run = await store.create(identity, owner="worker", definition_digest="d")
        key = StepKey.workflow("pay", 0)
        intent = StepIntent(key, "digest", default_idempotency_key(identity, key))
        await store.commit(
            run.lease, state={}, steps=[intent], effects=[EffectRecord(identity, intent)]
        )
        await store.close()
        assert not conn.in_transaction
        conn.execute("SELECT 1")  # still open for its owner
        with sqlite3.connect(path) as other:
            assert other.execute("SELECT COUNT(*) FROM nexura_exec_effects").fetchone() == (1,)
    finally:
        conn.close()
    legacy = sqlite3.connect(path, autocommit=False)
    try:
        with pytest.raises(ValueError, match="idle"):
            SQLiteExecutionStore.from_connection(legacy)
    finally:
        legacy.close()
