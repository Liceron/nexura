"""One ExecutionStore contract suite, exercised against all three backends.

Postgres requires NEXURA_TEST_POSTGRES_URI pointing at a disposable database.
The two-process SQLite cases use spawn and ordinary files (no shared semaphores).
"""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import sqlite3
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
from nexura_agents.contracts.execution import (
    DefinitionMismatchError,
    EffectRecord,
    EffectResolution,
    EffectStatus,
    ExecutionId,
    LeaseLostError,
    StepIntent,
    StepKey,
    StepOutcome,
    default_idempotency_key,
)
from nexura_agents.execution_store import (
    EffectAlreadyResolvedError,
    ExecutionConflictError,
    ExecutionLease,
    ExecutionRun,
    ExecutionStore,
    ExecutionVersionError,
    MemoryExecutionStore,
    SQLiteExecutionStore,
)
from nexura_agents.execution_store.base import _Store
from nexura_agents.execution_store.sql import _SQLStore


@dataclass
class Harness:
    store: ExecutionStore
    backend: str
    workspace: str
    path: Path

    def identity(self, run: str = "run") -> ExecutionId:
        return ExecutionId(self.workspace, run)


@pytest.fixture(params=["memory", "sqlite", "postgres"])
async def journal(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Harness]:
    backend = request.param
    store: ExecutionStore
    path = tmp_path / "execution.sqlite3"
    if backend == "memory":
        store = MemoryExecutionStore()
    elif backend == "sqlite":
        store = SQLiteExecutionStore(path)
    else:
        dsn = os.environ.get("NEXURA_TEST_POSTGRES_URI")
        if not dsn:
            pytest.skip("NEXURA_TEST_POSTGRES_URI is not configured")
        from nexura_postgres.execution_store import PostgresExecutionStore

        store = PostgresExecutionStore(dsn)
    await store.init_db()
    try:
        yield Harness(store, backend, "s2-" + uuid.uuid4().hex, path)
    finally:
        await store.close()


async def _create(journal: Harness, name: str = "run", **kwargs: Any) -> ExecutionRun:
    return await journal.store.create(
        journal.identity(name), owner="original", definition_digest="definition", **kwargs
    )


def _effect(identity: ExecutionId, step: int = 1, key: str | None = None) -> EffectRecord:
    step_key = StepKey.workflow("charge", step)
    return EffectRecord(
        identity,
        StepIntent(
            step_key,
            "payload",
            key or default_idempotency_key(identity, step_key),
            approval_id="approval",
            reservation_ids=["reservation"],
        ),
    )


async def test_creation_is_leased_isolated_versioned_and_unique(journal: Harness) -> None:
    store = journal.store
    assert isinstance(store, ExecutionStore)
    state = {"nested": {"value": 1}}
    run = await _create(
        journal,
        state=state,
        kind="workflow",
        parent_run_id="parent",
        origin={"trigger": {"job_id": "job", "attempt": 1}},
    )
    assert run.fencing_token == 1 and run.lease_owner == "original"
    assert run.lease_expires_at is not None and run.lease_expires_at > run.created_at
    assert (run.journal_schema_version, run.state_version) == (1, 2)
    state["nested"]["value"] = 99
    run.state["nested"]["value"] = 88
    snapshot = await store.get(run.execution_id)
    assert snapshot.run.state == {"nested": {"value": 1}}
    assert snapshot.run.kind == "workflow" and snapshot.run.parent_run_id == "parent"
    assert snapshot.run.origin["trigger"]["job_id"] == "job"
    snapshot.run.state["nested"]["value"] = 77
    assert (await store.get(run.execution_id)).run.state["nested"]["value"] == 1
    with pytest.raises(ExecutionConflictError):
        await _create(journal)
    other = await store.create(
        ExecutionId(journal.workspace + "-other", "run"),
        owner="other",
        definition_digest="definition",
    )
    assert other.execution_id != run.execution_id
    assert await store.list_recoverable(journal.workspace) == []
    with pytest.raises(KeyError):
        await store.get(journal.identity("missing"))


async def test_claim_renew_release_and_definition_fencing(journal: Harness) -> None:
    store = journal.store
    run = await _create(journal)
    lease = run.lease
    assert (
        await store.claim(run.execution_id, "competitor", 30, definition_digest="definition")
        is None
    )
    own = await store.claim(run.execution_id, "original", 30, definition_digest="definition")
    assert own is not None and own.fencing_token == lease.fencing_token
    renewed = await store.renew(own, 60)
    assert renewed.expires_at > own.expires_at
    before = await store.get(run.execution_id)
    with pytest.raises(DefinitionMismatchError):
        await store.claim(run.execution_id, "original", 30, definition_digest="changed")
    assert await store.get(run.execution_id) == before
    await store.release(renewed)
    assert [r.execution_id for r in await store.list_recoverable(journal.workspace)] == [
        run.execution_id
    ]
    replacement = await store.claim(run.execution_id, "new", 30, definition_digest="definition")
    assert replacement is not None and replacement.fencing_token == lease.fencing_token + 1
    before = await store.get(run.execution_id)
    effect = _effect(run.execution_id)
    for stale in (
        lease,
        replace(replacement, owner="wrong"),
        replace(replacement, fencing_token=replacement.fencing_token + 1),
    ):
        with pytest.raises(LeaseLostError):
            await store.commit(
                stale,
                state={"stale": True},
                steps=[effect.intent],
                effects=[effect],
                events=[{"type": "stale"}],
            )
        with pytest.raises(LeaseLostError):
            await store.renew(stale, 30)
        with pytest.raises(LeaseLostError):
            await store.release(stale)
    assert await store.get(run.execution_id) == before


async def test_expiry_uses_store_time_and_same_owner_gets_new_token(journal: Harness) -> None:
    store = journal.store
    run = await _create(journal, ttl=0.025)
    await asyncio.sleep(0.05)
    assert [r.execution_id for r in await store.list_recoverable(journal.workspace)] == [
        run.execution_id
    ]
    with pytest.raises(LeaseLostError):
        await store.renew(run.lease, 30)
    with pytest.raises(LeaseLostError):
        await store.commit(run.lease, state={"late": True})
    new = await store.claim(run.execution_id, "original", 30, definition_digest="definition")
    assert new is not None and new.fencing_token == run.fencing_token + 1
    with pytest.raises(LeaseLostError):
        await store.release(run.lease)


async def test_atomic_intent_outcome_effect_and_event_journal(journal: Harness) -> None:
    store = journal.store
    run = await _create(journal)
    effect = _effect(run.execution_id)
    await store.commit(
        run.lease,
        state={"phase": "dispatched"},
        cursor="charge#1",
        steps=[effect.intent],
        effects=[effect],
        events=[{"type": "dispatch"}],
    )
    snapshot = await store.get(run.execution_id)
    assert snapshot.steps["charge#1"].intent == effect.intent
    assert snapshot.effects[effect.effect_id] == effect
    assert snapshot.run.state == {"phase": "dispatched"}
    assert snapshot.run.cursor == "charge#1"
    outcome = StepOutcome(StepKey("charge#1"), "completed", {"receipt": 42})
    pure = StepOutcome(StepKey.agent_model(0), "completed", "model-output")
    confirmed = replace(effect, status=EffectStatus.CONFIRMED, result=outcome.output)
    await store.commit(
        run.lease,
        state={"phase": "next"},
        cursor="next#2",
        steps=[outcome, pure],
        effects=[confirmed],
        events=[{"type": "outcome"}, {"detail": "custom"}],
    )
    snapshot = await store.get(run.execution_id)
    assert snapshot.steps["charge#1"].outcome == outcome
    assert snapshot.steps["turn:0/model"].intent is None
    assert snapshot.effects[effect.effect_id] == confirmed
    assert [event.sequence for event in snapshot.events] == [1, 2, 3]
    assert [event.event_type for event in snapshot.events] == ["dispatch", "outcome", "custom"]
    before = snapshot
    with pytest.raises(ExecutionConflictError):
        await store.commit(
            run.lease,
            state={"must": "rollback"},
            steps=[StepOutcome(StepKey("charge#1"), "failed", "different")],
            events=[{"type": "must-rollback"}],
        )
    assert await store.get(run.execution_id) == before
    # Repeating the exact same facts never creates a second step outcome.
    await store.commit(
        run.lease,
        state=before.run.state,
        cursor=before.run.cursor,
        steps=[outcome],
        effects=[confirmed],
    )
    assert len((await store.get(run.execution_id)).steps) == 2


async def test_constraint_failure_rolls_back_every_table(journal: Harness) -> None:
    store = journal.store
    first = await _create(journal, "first")
    effect = _effect(first.execution_id, key="workspace-key")
    await store.commit(first.lease, state={}, steps=[effect.intent], effects=[effect])
    second = await _create(journal, "second")
    before = await store.get(second.execution_id)
    duplicate = _effect(second.execution_id, key="workspace-key")
    with pytest.raises(ExecutionConflictError):
        await store.commit(
            second.lease,
            state={"bad": True},
            cursor="charge#1",
            steps=[duplicate.intent],
            effects=[duplicate],
            events=[{"type": "rollback"}],
        )
    assert await store.get(second.execution_id) == before
    # A second effect in the same run cannot reuse the key either.
    duplicate = _effect(first.execution_id, step=2, key="workspace-key")
    before_first = await store.get(first.execution_id)
    with pytest.raises(ExecutionConflictError):
        await store.commit(first.lease, state={}, steps=[duplicate.intent], effects=[duplicate])
    assert await store.get(first.execution_id) == before_first
    other = await store.create(
        ExecutionId(journal.workspace + "-other", "run"),
        owner="other",
        definition_digest="definition",
    )
    allowed = _effect(other.execution_id, key="workspace-key")
    await store.commit(other.lease, state={}, steps=[allowed.intent], effects=[allowed])


@pytest.mark.parametrize("resolution", list(EffectResolution))
async def test_effect_resolution_is_once_fenced_and_audited(
    journal: Harness, resolution: EffectResolution
) -> None:
    store = journal.store
    run = await _create(journal)
    effect = _effect(run.execution_id)
    await store.commit(run.lease, state={}, steps=[effect.intent], effects=[effect])
    with pytest.raises(ExecutionConflictError):
        await store.resolve_effect(run.lease, effect.effect_id, resolution, actor="operator")
    unknown = replace(effect, status=EffectStatus.INDETERMINATE)
    await store.commit(
        run.lease, state={"pause_reason": "effect_unknown"}, status="paused", effects=[unknown]
    )
    assert (await store.get(run.execution_id)).run.lease_owner is None
    assert await store.list_recoverable(journal.workspace) == []
    with pytest.raises(LeaseLostError):
        await store.resolve_effect(run.lease, effect.effect_id, resolution, actor="stale")
    lease = await store.claim(run.execution_id, "operator", 30, definition_digest="definition")
    assert lease is not None
    with pytest.raises(ValueError):
        await store.resolve_effect(lease, effect.effect_id, resolution, actor="")
    resolved = await store.resolve_effect(
        lease, effect.effect_id, resolution, actor="operator", result={"receipt": 42}
    )
    assert resolved.status is EffectStatus.RESOLVED and resolved.resolution is resolution
    assert resolved.resolved_by == "operator" and resolved.result == {"receipt": 42}
    snapshot = await store.get(run.execution_id)
    assert snapshot.events[-1].event_type == "effect_resolved"
    assert snapshot.events[-1].payload["actor"] == "operator"
    again = await store.claim(run.execution_id, "operator", 30, definition_digest="definition")
    assert again is not None
    before = await store.get(run.execution_id)
    with pytest.raises(EffectAlreadyResolvedError):
        await store.resolve_effect(again, effect.effect_id, resolution, actor="operator")
    assert await store.get(run.execution_id) == before


async def test_invalid_effect_changes_are_atomic(journal: Harness) -> None:
    store = journal.store
    run = await _create(journal)
    effect = _effect(run.execution_id)
    before = await store.get(run.execution_id)
    with pytest.raises(ExecutionConflictError):
        await store.commit(run.lease, state={}, effects=[effect])
    with pytest.raises(ExecutionConflictError):
        await store.commit(
            run.lease,
            state={},
            steps=[effect.intent],
            effects=[replace(effect, status=EffectStatus.CONFIRMED)],
        )
    with pytest.raises(ExecutionConflictError):
        await store.commit(
            run.lease,
            state={},
            steps=[effect.intent],
            effects=[replace(effect, execution_id=journal.identity("other"))],
        )
    assert await store.get(run.execution_id) == before
    await store.commit(run.lease, state={}, steps=[effect.intent], effects=[effect])
    before = await store.get(run.execution_id)
    with pytest.raises(ExecutionConflictError):
        await store.commit(
            run.lease, state={}, steps=[replace(effect.intent, payload_digest="changed")]
        )
    with pytest.raises(ExecutionConflictError):
        await store.commit(
            run.lease,
            state={},
            effects=[
                replace(effect, status=EffectStatus.RESOLVED, resolution=EffectResolution.CONFIRMED)
            ],
        )
    assert await store.get(run.execution_id) == before


@pytest.mark.parametrize("status", ["paused", "completed", "failed"])
async def test_terminal_and_paused_commits_release_the_lease(journal: Harness, status: str) -> None:
    store = journal.store
    run = await _create(journal)
    result = await store.commit(run.lease, state={}, status=status)
    assert result.lease_owner is None and result.lease_expires_at is None
    assert await store.list_recoverable(journal.workspace) == []
    with pytest.raises(LeaseLostError):
        await store.commit(run.lease, state={"late": True})
    if status != "paused":
        with pytest.raises(ExecutionConflictError):
            await store.claim(run.execution_id, "new", 30, definition_digest="definition")


async def test_json_and_version_write_failures_preserve_snapshot(journal: Harness) -> None:
    run = await _create(journal)
    store = journal.store
    before = await store.get(run.execution_id)
    invalid_states: list[Any] = [{"bad": float("nan")}, {"bad": object()}, {1: "coerced"}]
    for state in invalid_states:
        with pytest.raises((ValueError, TypeError)):
            await store.commit(run.lease, state=state, events=[{"type": "rollback"}])
    for version in (1, 3):
        with pytest.raises(ExecutionVersionError):
            await store.commit(run.lease, state={}, state_version=version)
    assert await store.get(run.execution_id) == before


@pytest.mark.parametrize("ttl", [0, -1, float("inf"), float("nan"), True])
async def test_invalid_ttl_cannot_change_a_lease(journal: Harness, ttl: float) -> None:
    run = await _create(journal)
    before = await journal.store.get(run.execution_id)
    with pytest.raises(ValueError):
        await journal.store.claim(run.execution_id, "other", ttl, definition_digest="definition")
    with pytest.raises(ValueError):
        await journal.store.renew(run.lease, ttl)
    assert await journal.store.get(run.execution_id) == before


async def test_database_migrations_reopen_and_version_compatibility(journal: Harness) -> None:
    if not isinstance(journal.store, _SQLStore):
        pytest.skip("SQL migration-specific portion of the shared suite")
    store = journal.store
    await store.init_db()
    run = await _create(journal)
    async with store._connection() as db:
        versions = await db.fetch("SELECT version FROM nexura_exec_migrations", {})
        assert [row["version"] for row in versions] == [1]
        await db.execute(
            "UPDATE nexura_exec_runs SET state_version=1, journal_schema_version=0 "
            "WHERE workspace_id=:workspace AND run_id=:run",
            {"workspace": journal.workspace, "run": "run"},
        )
    assert (await store.get(run.execution_id)).run.state_version == 1
    await store.commit(run.lease, state={"upgraded": True})
    assert (await store.get(run.execution_id)).run.state_version == 2
    async with store._connection() as db:
        await db.execute(
            "UPDATE nexura_exec_runs SET state_version=99 "
            "WHERE workspace_id=:workspace AND run_id=:run",
            {"workspace": journal.workspace, "run": "run"},
        )
    with pytest.raises(ExecutionVersionError):
        await store.get(run.execution_id)
    async with store._connection() as db:
        await db.execute(
            "UPDATE nexura_exec_runs SET state_version=2 "
            "WHERE workspace_id=:workspace AND run_id=:run",
            {"workspace": journal.workspace, "run": "run"},
        )
    if journal.backend == "sqlite":
        reopened = SQLiteExecutionStore(journal.path)
        try:
            await reopened.init_db()
            assert await reopened.get(run.execution_id) == await store.get(run.execution_id)
        finally:
            await reopened.close()


async def test_expected_revision_is_checked_inside_fenced_commit(journal: Harness) -> None:
    """Moved from the deleted ``test_a6_engine_effects.py`` (A6 S12), now on every backend."""
    store = journal.store
    run = await _create(journal)
    await store.commit(run.lease, state={"winner": True}, expected_revision=run.revision)
    before = await store.get(run.execution_id)
    with pytest.raises(ExecutionConflictError):
        await store.commit(run.lease, state={"stale": True}, expected_revision=run.revision)
    assert await store.get(run.execution_id) == before


async def test_parallel_claims_have_one_winner(journal: Harness) -> None:
    run = await _create(journal)
    await journal.store.release(run.lease)
    results = await asyncio.gather(
        *[
            journal.store.claim(run.execution_id, owner, 30, definition_digest="definition")
            for owner in ("one", "two")
        ]
    )
    assert sum(result is not None for result in results) == 1


async def test_previous_versions_are_readable_and_future_versions_are_refused(
    journal: Harness,
) -> None:
    run = await _create(journal)
    store = journal.store
    assert isinstance(store, _Store)
    # Seed a prior-release envelope through the backend transaction, never through
    # the current-version-only public writer.
    async with store._edit(run.execution_id) as tx:
        tx.snapshot = replace(
            tx.snapshot, run=replace(tx.snapshot.run, journal_schema_version=0, state_version=1)
        )
    assert (await store.get(run.execution_id)).run.state_version == 1
    await store.commit(run.lease, state={"v2": True})
    restored = await store.get(run.execution_id)
    assert (restored.run.journal_schema_version, restored.run.state_version) == (1, 2)
    for field in ("journal_schema_version", "state_version"):
        future_version: dict[str, Any] = {field: 999}
        async with store._edit(run.execution_id) as tx:
            tx.snapshot = replace(tx.snapshot, run=replace(tx.snapshot.run, **future_version))
        with pytest.raises(ExecutionVersionError):
            await store.get(run.execution_id)
        with pytest.raises(ExecutionVersionError):
            await store.claim(run.execution_id, "owner", 30, definition_digest="definition")
        # Restore seeded data directly; the unsupported envelope must reject _edit.
        if isinstance(store, MemoryExecutionStore):
            store._runs[run.execution_id] = restored
        else:
            assert isinstance(store, _SQLStore)
            async with store._connection() as db:
                await db.execute(
                    f"UPDATE nexura_exec_runs SET {field}=:version "
                    "WHERE workspace_id=:workspace AND run_id=:run",
                    {
                        "version": getattr(restored.run, field),
                        "workspace": journal.workspace,
                        "run": "run",
                    },
                )


def test_sqlite_future_migration_is_refused_without_changing_existing_data(tmp_path: Path) -> None:
    async def check() -> None:
        store = SQLiteExecutionStore(tmp_path / "future.sqlite3")
        try:
            await store.init_db()
            identity = ExecutionId("future", "run")
            await store.create(identity, owner="owner", definition_digest="definition")
            before = await store.get(identity)
            async with store._connection() as db:
                await db.execute("INSERT INTO nexura_exec_migrations VALUES (999, 'future', 0)", {})
            with pytest.raises(ExecutionVersionError):
                await store.init_db()
            assert await store.get(identity) == before
        finally:
            await store.close()

    asyncio.run(check())


def _spawn_worker(path: str, mode: str, owner: str, output: str, gate: str, token: int = 0) -> None:
    async def work() -> None:
        store = SQLiteExecutionStore(path)
        try:
            await store.init_db()
            Path(output + ".ready").write_text("ready")
            deadline = time.monotonic() + 15
            while not Path(gate).exists():
                if time.monotonic() > deadline:
                    raise TimeoutError("parent gate")
                await asyncio.sleep(0.005)
            identity = ExecutionId("process", "run")
            result: dict[str, Any]
            if mode == "create":
                try:
                    await store.create(identity, owner=owner, definition_digest="definition")
                except ExecutionConflictError:
                    result = {"created": False}
                else:
                    result = {"created": True}
            elif mode == "claim":
                lease = await store.claim(identity, owner, 30, definition_digest="definition")
                result = {"token": lease.fencing_token if lease else None}
            else:
                stale = ExecutionLease(identity, owner, token, 0)
                effect = _effect(identity)
                try:
                    await store.commit(
                        stale,
                        state={"stale": True},
                        steps=[effect.intent],
                        effects=[effect],
                        events=[{"type": "stale"}],
                    )
                except LeaseLostError:
                    result = {"stale": "refused"}
                else:
                    result = {"stale": "accepted"}
            # Write then rename, so the parent never reads a half-written file.
            partial = Path(output + ".partial")
            partial.write_text(json.dumps(result))
            partial.replace(output)
        finally:
            await store.close()

    asyncio.run(work())


def _wait_files(paths: list[Path], workers: list[Any]) -> None:
    deadline = time.monotonic() + 20
    while not all(path.exists() for path in paths):
        assert time.monotonic() < deadline, "spawn worker timed out"
        assert all(worker.exitcode in (None, 0) for worker in workers), "spawn worker failed"
        time.sleep(0.01)


@pytest.mark.durability
def test_sqlite_spawned_create_and_migration_races(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    path = tmp_path / "new.sqlite3"
    gate = tmp_path / "go"
    outputs = [tmp_path / "one.json", tmp_path / "two.json"]
    workers = [
        context.Process(
            target=_spawn_worker, args=(str(path), "create", owner, str(output), str(gate))
        )
        for owner, output in zip(("one", "two"), outputs, strict=True)
    ]
    try:
        for worker in workers:
            worker.start()
        _wait_files([Path(str(output) + ".ready") for output in outputs], workers)
        gate.write_text("start")
        _wait_files(outputs, workers)
        assert sum(json.loads(output.read_text())["created"] for output in outputs) == 1
    finally:
        for worker in workers:
            if worker.pid is not None:
                worker.join(timeout=5)
                if worker.is_alive():
                    worker.kill()
                    worker.join(timeout=5)
                assert worker.exitcode == 0


@pytest.mark.durability
def test_sqlite_spawned_claim_race_and_stale_process_commit(tmp_path: Path) -> None:
    path = tmp_path / "process.sqlite3"
    identity = ExecutionId("process", "run")

    async def initialize() -> ExecutionLease:
        store = SQLiteExecutionStore(path)
        try:
            await store.init_db()
            run = await store.create(identity, owner="old", definition_digest="definition")
            await store.release(run.lease)
            return run.lease
        finally:
            await store.close()

    old = asyncio.run(initialize())
    context = multiprocessing.get_context("spawn")
    gate = tmp_path / "go"
    outputs = [tmp_path / "one.json", tmp_path / "two.json"]
    workers = [
        context.Process(
            target=_spawn_worker, args=(str(path), "claim", owner, str(output), str(gate))
        )
        for owner, output in zip(("one", "two"), outputs, strict=True)
    ]
    try:
        for worker in workers:
            worker.start()
        _wait_files([Path(str(output) + ".ready") for output in outputs], workers)
        gate.write_text("start")
        _wait_files(outputs, workers)
        results = [json.loads(output.read_text()) for output in outputs]
        assert sum(result["token"] is not None for result in results) == 1
        assert next(result["token"] for result in results if result["token"]) > old.fencing_token
        output = tmp_path / "stale.json"
        stale = context.Process(
            target=_spawn_worker,
            args=(str(path), "stale", "old", str(output), str(gate), old.fencing_token),
        )
        workers.append(stale)
        stale.start()
        _wait_files([output], workers)
        assert json.loads(output.read_text()) == {"stale": "refused"}

        async def verify() -> None:
            store = SQLiteExecutionStore(path)
            try:
                snapshot = await store.get(identity)
                assert snapshot.run.state == {}
                assert snapshot.steps == {}
                assert snapshot.effects == {}
                assert snapshot.events == []
            finally:
                await store.close()

        asyncio.run(verify())
    finally:
        for worker in workers:
            if worker.pid is not None:
                worker.join(timeout=5)
                if worker.is_alive():
                    worker.kill()
                    worker.join(timeout=5)
                assert worker.exitcode == 0


async def test_sqlite_wal_write_cost_is_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "benchmark.sqlite3"
    store = SQLiteExecutionStore(path)
    try:
        await store.init_db()
        run = await store.create(
            ExecutionId("benchmark", "run"), owner="bench", definition_digest="definition", ttl=120
        )
        samples = []
        for step in range(50):
            effect = _effect(run.execution_id, step)
            start = time.perf_counter()
            await store.commit(
                run.lease, state={"cursor": step}, steps=[effect.intent], effects=[effect]
            )
            await store.commit(
                run.lease,
                state={"cursor": step + 1},
                steps=[StepOutcome(effect.intent.step_key, "completed", step)],
                effects=[replace(effect, status=EffectStatus.CONFIRMED, result=step)],
            )
            samples.append((time.perf_counter() - start) * 1000)
        samples.sort()
        with capsys.disabled():
            print(
                f"\nSQLite WAL FULL intent+outcome (50 pairs): mean={sum(samples) / 50:.3f}ms "
                f"p50={samples[25]:.3f}ms p95={samples[47]:.3f}ms; target <5ms (not asserted)"
            )
        with sqlite3.connect(path) as db:
            assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        await store.close()
