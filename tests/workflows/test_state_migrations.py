"""P38 C6: local fake handlers, fenced state migration and immutable approvals."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from nexura_agents.contracts.execution import ExecutionId
from nexura_agents.errors import WorkflowConflictError, WorkflowValidationError

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.workflow import (
    MemoryWorkflowStore,
    ModuleRegistry,
    SQLiteWorkflowStore,
    WorkflowCheckpoint,
    WorkflowEngine,
    WorkflowMigrationError,
    WorkflowNode,
    WorkflowSpec,
    checkpoint_to_dict,
    interrupt,
)


class Crash(BaseException):
    """A stopped worker, bypassing the engine's ordinary failure handling."""


@pytest.fixture(params=["memory", "sqlite", "postgres"])
def migration_store(request: pytest.FixtureRequest, tmp_path: Path) -> Any:
    if request.param == "postgres":
        return request.getfixturevalue("postgres_workflow_store")
    if request.param == "memory":
        return MemoryWorkflowStore()
    store = SQLiteWorkflowStore(tmp_path / "migrations.db")
    request.addfinalizer(store.close_sync)
    return store


def spec(version: str | int | None = 1, *, gated: bool = True) -> WorkflowSpec:
    return WorkflowSpec(
        name="migration-example",
        version=version,
        entry_node="action",
        nodes={"action": WorkflowNode(id="action", handler="fake", requires_approval=gated)},
    )


def registry() -> ModuleRegistry:
    reg = ModuleRegistry()
    reg.register("fake", lambda state: {"seen": state["value"]}, effect="pure")
    return reg


async def test_identity_and_approval_regating(migration_store: Any) -> None:
    old = spec()
    engine = WorkflowEngine(old, registry(), store=migration_store)
    paused = await engine.run({"value": 2}, workflow_run_id="r")
    pending = paused.pending_approval
    assert pending is not None
    assert paused.workflow_version == 1
    assert paused.definition_digest == old.canonical_hash()
    new = spec(2)
    calls: list[tuple[str | int, str | int]] = []

    def migrate(start: str | int, end: str | int, state: dict[str, Any]) -> dict[str, Any]:
        calls.append((start, end))
        return {**state, "value": state["value"] + 10}

    new.register_migration(1, 2, migrate)
    engine = WorkflowEngine(new, registry(), store=migration_store)
    cp = await engine.migrate("r")
    assert cp.workflow_version == 2 and cp.state["value"] == 12
    assert cp.pending_approval == pending  # no silent digest rebinding
    assert checkpoint_to_dict(cp)["definition_digest"] == new.canonical_hash()
    again = await engine.resume("r", pending.approval_id)
    fresh = again.pending_approval
    assert fresh is not None and fresh.approval_id != pending.approval_id
    assert fresh.spec_hash == new.canonical_hash()
    assert fresh.payload_digest != pending.payload_digest
    assert fresh.regate_reason == "definition_migrated"
    assert fresh.supersedes_approval_id == pending.approval_id
    assert "seen" not in again.state
    done = await engine.resume("r", fresh.approval_id)
    assert done.status == "completed" and done.state["seen"] == 12
    assert calls == [(1, 2)]
    journal = await migration_store.open_execution_store()
    snapshot = await journal.get(ExecutionId("default", "r"))
    assert snapshot.run.definition_digest == old.canonical_hash()  # immutable origin
    assert snapshot.run.state["definition_digest"] == new.canonical_hash()
    assert sum(e.event_type == "state_migrated" for e in snapshot.events) == 1


async def test_removed_gate_still_requires_fresh_approval(migration_store: Any) -> None:
    paused = await WorkflowEngine(spec(), registry(), store=migration_store).run(
        {"value": 1}, workflow_run_id="r"
    )
    new = spec(2, gated=False)
    new.register_migration(1, 2, lambda a, b, s: s)
    assert paused.pending_approval is not None
    engine = WorkflowEngine(new, registry(), store=migration_store)
    fresh = await engine.resume("r", paused.pending_approval.approval_id)
    assert fresh.status == "paused" and "seen" not in fresh.state
    assert fresh.pending_approval is not None
    done = await engine.resume("r", fresh.pending_approval.approval_id)
    assert done.status == "completed"


async def test_chained_migrations_and_resume_after_crash(migration_store: Any) -> None:
    reg = registry()
    reg.register("fake", lambda state: (_ for _ in ()).throw(Crash()), effect="pure")
    old = spec("v1", gated=False)
    with pytest.raises(Crash):
        await WorkflowEngine(old, reg, store=migration_store).run({"value": 3}, workflow_run_id="r")
    new = spec("v3", gated=False)
    calls: list[str | int] = []

    def migrate(start: str | int, end: str | int, state: dict[str, Any]) -> dict[str, Any]:
        calls.append(end)
        return {**state, "value": state["value"] + 1}

    new.register_migration("v1", "v2", migrate)
    new.register_migration("v2", "v3", migrate)
    engine = WorkflowEngine(new, reg, store=migration_store)
    with pytest.raises(Crash):  # migration committed; crash in the next step
        await engine.recover("r")
    done = await WorkflowEngine(new, registry(), store=migration_store).recover("r")
    assert done.status == "completed" and done.state["seen"] == 5
    assert calls == ["v2", "v3"]


async def test_lost_checkpoint_projection_does_not_repeat_hook(
    migration_store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    await WorkflowEngine(spec(), registry(), store=migration_store).run(
        {"value": 1}, workflow_run_id="r"
    )
    new = spec(2)
    calls: list[int] = []

    def migrate(start: str | int, end: str | int, state: dict[str, Any]) -> dict[str, Any]:
        calls.append(1)
        return {**state, "value": 99}

    new.register_migration(1, 2, migrate)
    engine = WorkflowEngine(new, registry(), store=migration_store)
    save = migration_store.save

    async def die_before_save(cp: WorkflowCheckpoint) -> None:
        raise Crash

    monkeypatch.setattr(migration_store, "save", die_before_save)
    with pytest.raises(Crash):
        await engine.migrate("r")
    monkeypatch.setattr(migration_store, "save", save)
    restored = await engine.migrate("r")
    assert restored.state["value"] == 99 and restored.workflow_version == 2
    assert calls == [1]


async def test_new_deploy_projects_previous_migration_before_next_hook(
    migration_store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    await WorkflowEngine(spec(), registry(), store=migration_store).run(
        {"value": 1}, workflow_run_id="r"
    )
    calls: list[str | int] = []

    def migrate(a: str | int, b: str | int, s: dict[str, Any]) -> dict[str, Any]:
        calls.append(b)
        return {**s, "value": s["value"] + 1}

    v2 = spec(2)
    v2.register_migration(1, 2, migrate)
    save = migration_store.save

    async def die(cp: WorkflowCheckpoint) -> None:
        raise Crash

    monkeypatch.setattr(migration_store, "save", die)
    with pytest.raises(Crash):
        await WorkflowEngine(v2, registry(), store=migration_store).migrate("r")
    monkeypatch.setattr(migration_store, "save", save)
    v3 = spec(3)
    v3.register_migration(1, 2, migrate)
    v3.register_migration(2, 3, migrate)
    cp = await WorkflowEngine(v3, registry(), store=migration_store).migrate("r")
    assert cp.state["value"] == 3 and calls == [2, 3]


async def test_approval_claim_racing_migration_commit_projects_safely(
    migration_store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    paused = await WorkflowEngine(spec(), registry(), store=migration_store).run(
        {"value": 1}, workflow_run_id="r"
    )
    assert paused.pending_approval is not None
    approval_id = paused.pending_approval.approval_id
    new = spec(2)
    calls: list[int] = []

    def migrate(a: str | int, b: str | int, s: dict[str, Any]) -> dict[str, Any]:
        calls.append(1)
        return {**s, "value": 2}

    new.register_migration(1, 2, migrate)
    save = migration_store.save

    async def claim_before_save(cp: WorkflowCheckpoint) -> None:
        assert await migration_store.claim_approval("r", approval_id)
        await save(cp)

    monkeypatch.setattr(migration_store, "save", claim_before_save)
    engine = WorkflowEngine(new, registry(), store=migration_store)
    with pytest.raises(WorkflowConflictError):
        await engine.migrate("r")
    monkeypatch.setattr(migration_store, "save", save)
    restored = await engine.migrate("r")
    assert restored.state["value"] == 2 and calls == [1]
    fresh = await engine.resume("r", approval_id)
    assert fresh.status == "paused" and "seen" not in fresh.state


async def test_in_flight_idempotent_payload_cannot_change(migration_store: Any) -> None:
    reg = ModuleRegistry()
    reg.register("fake", lambda s: (_ for _ in ()).throw(Crash()), effect="idempotent")
    with pytest.raises(Crash):
        await WorkflowEngine(spec(gated=False), reg, store=migration_store).run(
            {"value": 1}, workflow_run_id="r"
        )
    new = spec(2, gated=False)
    new.register_migration(1, 2, lambda a, b, s: {**s, "value": 2})
    with pytest.raises(WorkflowMigrationError, match="in-flight intent 'action#1'"):
        await WorkflowEngine(new, reg, store=migration_store).recover("r")
    cp = await migration_store.get("r")
    assert cp.state["value"] == 1 and cp.workflow_version == 1


async def test_journaled_intent_node_must_exist(migration_store: Any) -> None:
    old = spec()
    old.entry_node = "earlier"
    old.nodes["earlier"] = WorkflowNode(id="earlier", handler="fake", default_transition="action")
    reg = ModuleRegistry()
    reg.register("fake", lambda s: (_ for _ in ()).throw(Crash()), effect="non_idempotent")
    with pytest.raises(Crash):
        await WorkflowEngine(old, reg, store=migration_store).run({"value": 1}, workflow_run_id="r")
    new = spec(2)
    new.register_migration(1, 2, lambda a, b, s: s)
    with pytest.raises(WorkflowMigrationError, match="missing node ids: earlier"):
        await WorkflowEngine(new, reg, store=migration_store).migrate("r")


async def test_child_journaled_intent_node_must_exist(migration_store: Any) -> None:
    child = WorkflowSpec(
        name="child",
        version=1,
        entry_node="earlier",
        nodes={
            "earlier": WorkflowNode(id="earlier", handler="fake"),
        },
    )
    old = spec()
    old.entry_node = "sub"
    old.nodes["sub"] = WorkflowNode(
        id="sub", handler="", subgraph=child, default_transition="action"
    )
    reg = ModuleRegistry()
    reg.register("fake", lambda s: (_ for _ in ()).throw(Crash()), effect="non_idempotent")
    with pytest.raises(Crash):
        await WorkflowEngine(old, reg, store=migration_store).run({"value": 1}, workflow_run_id="r")
    new = WorkflowSpec.from_dict({**old.to_dict(), "workflow_version": 2})
    new.nodes["sub"].subgraph = WorkflowSpec(
        name="child",
        version=2,
        entry_node="later",
        nodes={
            "later": WorkflowNode(id="later", handler="fake"),
        },
    )
    new.register_migration(1, 2, lambda a, b, s: s)
    with pytest.raises(WorkflowMigrationError, match="missing node ids: earlier"):
        await WorkflowEngine(new, reg, store=migration_store).migrate("r")


async def test_effect_unknown_keeps_intent_and_resolution(migration_store: Any) -> None:
    reg = ModuleRegistry()
    reg.register("fake", lambda s: (_ for _ in ()).throw(Crash()), effect="non_idempotent")
    old = spec(gated=False)
    engine = WorkflowEngine(old, reg, store=migration_store)
    with pytest.raises(Crash):
        await engine.run({"value": 1}, workflow_run_id="r")
    unknown = await engine.recover("r")
    assert unknown.pause_reason == "effect_unknown"
    new = spec(2, gated=False)
    new.register_migration(1, 2, lambda a, b, s: {**s, "value": 2})
    engine = WorkflowEngine(new, reg, store=migration_store)
    migrated = await engine.recover("r")
    assert migrated.pending_effect == unknown.pending_effect
    assert migrated.pending_effect is not None
    journal = await migration_store.open_execution_store()
    snapshot = await journal.get(ExecutionId("default", "r"))
    assert snapshot.run.state["pending_effect"] == migrated.pending_effect["effect_id"]
    done = await engine.resolve_effect("r", "confirmed", actor="local-test", result={"seen": 1})
    assert done.status == "completed" and done.state == {"value": 2, "seen": 1}


@pytest.mark.parametrize("target_version", [2, 1, None])
async def test_missing_path_refuses_without_writes(
    migration_store: Any, target_version: int | None
) -> None:
    await WorkflowEngine(spec(), registry(), store=migration_store).run(
        {"value": 1}, workflow_run_id="r"
    )
    before = await migration_store.get("r")
    new = spec(target_version)
    new.metadata["changed"] = True
    engine = WorkflowEngine(new, registry(), store=migration_store)
    with pytest.raises(WorkflowMigrationError, match="no migration path"):
        await engine.recover("r")
    assert await migration_store.get("r") == before


async def test_missing_nodes_and_explicit_old_definition(migration_store: Any) -> None:
    old = spec()
    paused = await WorkflowEngine(old, registry(), store=migration_store).run(
        {"value": 7}, workflow_run_id="r"
    )
    assert paused.pending_approval is not None
    compatible = spec(2)
    done = await WorkflowEngine(compatible, registry(), store=migration_store).resume(
        "r", paused.pending_approval.approval_id, continue_on_old_definition=True
    )
    assert done.status == "completed" and done.workflow_version == 1
    paused = await WorkflowEngine(old, registry(), store=migration_store).run(
        {"value": 7}, workflow_run_id="s"
    )
    incompatible = WorkflowSpec(
        name=old.name,
        version=2,
        entry_node="replacement",
        nodes={"replacement": WorkflowNode(id="replacement", handler="fake")},
    )
    incompatible.register_migration(1, 2, lambda a, b, s: s)
    engine = WorkflowEngine(incompatible, registry(), store=migration_store)
    with pytest.raises(WorkflowMigrationError, match="missing node ids: action"):
        await engine.migrate("s")
    assert paused.pending_approval is not None
    with pytest.raises(WorkflowMigrationError, match="action"):
        await engine.resume(
            "s", paused.pending_approval.approval_id, continue_on_old_definition=True
        )


async def test_interrupt_migrates_before_answer(migration_store: Any) -> None:
    reg = ModuleRegistry()

    def ask(state: dict[str, Any]) -> dict[str, Any]:
        return {"answer": interrupt("answer?"), "seen": state["value"]}

    reg.register("fake", ask, effect="pure")
    paused = await WorkflowEngine(spec(gated=False), reg, store=migration_store).run(
        {"value": 1}, workflow_run_id="r"
    )
    assert paused.interrupt is not None
    new = spec(2, gated=False)
    new.register_migration(1, 2, lambda a, b, s: {**s, "value": 2})
    done = await WorkflowEngine(new, reg, store=migration_store).resume_interrupt(
        "r", paused.interrupt["interrupt_id"], "yes"
    )
    assert done.status == "completed" and done.state["seen"] == 2


async def test_bad_migration_keeps_original_state(migration_store: Any) -> None:
    await WorkflowEngine(spec(), registry(), store=migration_store).run(
        {"value": 1}, workflow_run_id="r"
    )
    new = spec(2)
    new.register_migration(1, 2, lambda a, b, s: [])  # type: ignore[arg-type,return-value]
    with pytest.raises(WorkflowValidationError, match="JSON object"):
        await WorkflowEngine(new, registry(), store=migration_store).migrate("r")
    cp = await migration_store.get("r")
    assert cp.state == {"value": 1} and cp.workflow_version == 1


def test_service_migrate_then_existing_decide(tmp_path: Path) -> None:
    app = create_api_app(registry=registry(), db_path=tmp_path / "api.db", profile="local-owner")
    app.state.workflow_migrations = {
        "migration-example": {(1, 2): lambda a, b, s: {**s, "value": 10}}
    }
    with TestClient(app) as client:
        saved = client.post(
            "/api/v1/workflows",
            json={
                "name": "migration-example",
                "spec": spec().to_dict(),
            },
        )
        assert saved.status_code == 201, saved.text
        workflow_id = saved.json()["workflow_id"]
        started = client.post(
            "/api/v1/runs", json={"workflow_id": workflow_id, "initial_state": {"value": 1}}
        )
        assert started.status_code in (200, 201), started.text
        run_id = started.json()["run_id"]
        original = client.get(f"/api/v1/runs/{run_id}").json()
        assert original["workflow_version"] == 1
        assert started.json()["workflow_version"] == 1
        saved = client.put(
            f"/api/v1/workflows/{workflow_id}",
            json={
                "spec": spec(99).to_dict(),
                "expected_revision": saved.json()["revision"],
            },
        )
        assert saved.status_code == 200, saved.text
        refused = client.post(f"/api/v1/runs/{run_id}/migrate", json={})
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"]["code"] == "WORKFLOW_MIGRATION_REFUSED"
        saved = client.put(
            f"/api/v1/workflows/{workflow_id}",
            json={
                "spec": spec(2).to_dict(),
                "expected_revision": saved.json()["revision"],
            },
        )
        assert saved.status_code == 200, saved.text
        migrated = client.post(f"/api/v1/runs/{run_id}/migrate", json={})
        assert migrated.status_code == 200, migrated.text
        body = migrated.json()
        assert body["workflow_version"] == 2
        assert body["started_workflow_version"] == 1
        assert body["origin_definition_digest"] == original["definition_digest"]
        assert body["definition_digest"] == spec(2).canonical_hash()
        assert body["pending_approval"]["spec_hash"] == original["definition_digest"]
        again = client.post(
            f"/api/v1/runs/{run_id}/decide",
            json={
                "approval_id": body["pending_approval"]["approval_id"],
                "decision": "approve",
            },
        )
        assert again.status_code == 200, again.text
        fresh = again.json()["pending_approval"]
        assert fresh["spec_hash"] == spec(2).canonical_hash()
        done = client.post(
            f"/api/v1/runs/{run_id}/decide",
            json={
                "approval_id": fresh["approval_id"],
                "decision": "approve",
            },
        )
        assert done.json()["status"] == "completed", done.text
        assert done.json()["workflow_version"] == 2
        listed = client.get("/api/v1/runs").json()["items"]
        assert listed[0]["workflow_version"] == 2


def test_sqlite_reopen_preserves_version(tmp_path: Path) -> None:
    path = tmp_path / "reopen.db"
    store = SQLiteWorkflowStore(path)
    asyncio.run(WorkflowEngine(spec("release-1"), registry(), store=store).run({"value": 1}))
    run_id = store._conn.execute(  # type: ignore[union-attr]
        "SELECT workflow_run_id FROM nexura_workflow_checkpoints"
    ).fetchone()[0]
    store.close_sync()
    reopened = SQLiteWorkflowStore(path)
    try:
        cp = asyncio.run(reopened.get(run_id))
        assert cp is not None and cp.workflow_version == "release-1"
    finally:
        reopened.close_sync()


@pytest.mark.parametrize("point", ["before_checkpoint", "next_step"])
def test_sigkill_after_migration_commit(tmp_path: Path, point: str) -> None:
    """Store/handler fault injection, as in the durability harness; no external service."""
    path = tmp_path / "killed.db"
    store = SQLiteWorkflowStore(path)
    reg = registry()
    reg.register("fake", lambda s: (_ for _ in ()).throw(Crash()), effect="pure")
    with pytest.raises(Crash):
        asyncio.run(
            WorkflowEngine(spec(gated=False), reg, store=store).run(
                {"value": 1}, workflow_run_id="r"
            )
        )
    store.close_sync()
    marker = tmp_path / "committed"
    script = """
import asyncio, sys, time
from pathlib import Path
from nexura_runtime.workflow import ModuleRegistry, SQLiteWorkflowStore, WorkflowEngine
from nexura_runtime.workflow import WorkflowNode, WorkflowSpec
database, marker, point = sys.argv[1:]
def block():
    Path(marker).write_text("committed")
    while True:
        time.sleep(1)
class Store(SQLiteWorkflowStore):
    async def save(self, checkpoint):
        if point == "before_checkpoint" and checkpoint.workflow_version == 2:
            block()
        await super().save(checkpoint)
async def main():
    store = Store(database)
    registry = ModuleRegistry()
    async def fake(state):
        block()
    registry.register("fake", fake, effect="pure")
    spec = WorkflowSpec(name="migration-example", version=2, entry_node="action",
        nodes={"action": WorkflowNode(id="action", handler="fake")})
    spec.register_migration(1, 2, lambda a,b,s: {**s,"value":s["value"]+1})
    await WorkflowEngine(spec, registry, store=store).recover("r")
asyncio.run(main())
"""
    with subprocess.Popen(
        [sys.executable, "-c", script, str(path), str(marker), point],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as process:
        deadline = time.monotonic() + 15
        while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        try:
            assert marker.exists(), "child did not reach committed migration boundary"
        finally:
            if process.poll() is None:
                process.kill()
            _, stderr = process.communicate(timeout=10)
        assert process.returncode == -9, stderr.decode()
    reopened = SQLiteWorkflowStore(path)
    new = spec(2, gated=False)

    def never_recompute(a: str | int, b: str | int, s: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("a committed migration was applied again")

    new.register_migration(1, 2, never_recompute)
    try:
        done = asyncio.run(WorkflowEngine(new, registry(), store=reopened).recover("r"))
        assert done.status == "completed" and done.state["seen"] == 2
    finally:
        reopened.close_sync()
