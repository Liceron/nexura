"""C6 review regressions; all actions and payments are explicitly local fakes."""

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
from nexura_agents.errors import WorkflowSpecMismatchError

from nexura_runtime._workflow_journal import WorkflowJournal
from nexura_runtime.service.api import runs
from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.workflow import (
    MemoryWorkflowStore,
    ModuleRegistry,
    SQLiteWorkflowStore,
    WorkflowEngine,
    WorkflowMigrationError,
    WorkflowNode,
    WorkflowSpec,
    interrupt,
    workflow_run_identities,
)


class Crash(BaseException):
    """A stopped process, bypassing ordinary workflow exception handling."""


def definition(version: int = 1, *, gated: bool = True) -> WorkflowSpec:
    return WorkflowSpec(
        name="review",
        version=version,
        entry_node="pay",
        nodes={
            "pay": WorkflowNode(id="pay", handler="fake", requires_approval=gated),
        },
    )


async def test_approval_claim_then_migration_never_dispatches_old_definition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryWorkflowStore()
    sends: list[int] = []
    hooks: list[int] = []
    reg = ModuleRegistry()

    def fake(state: dict[str, Any]) -> dict[str, Any]:
        sends.append(state["amount"])
        return {}

    reg.register("fake", fake, effect="pure")
    old = WorkflowEngine(definition(), reg, store=store)
    paused = await old.run({"amount": 5}, workflow_run_id="r")
    assert paused.pending_approval is not None
    target = definition(2)

    def migrate(a: Any, b: Any, state: dict[str, Any]) -> dict[str, Any]:
        hooks.append(1)
        return {**state, "amount": 500}

    target.register_migration(1, 2, migrate)
    new = WorkflowEngine(target, reg, store=store)
    take = WorkflowJournal.take
    injected = False

    async def race(journal: WorkflowJournal, owner: str) -> bool:
        nonlocal injected
        if not injected:
            injected = True
            await new.migrate("r")
        return await take(journal, owner)

    monkeypatch.setattr(WorkflowJournal, "take", race)
    with pytest.raises(WorkflowSpecMismatchError):
        await old.resume("r", paused.pending_approval.approval_id)
    monkeypatch.setattr(WorkflowJournal, "take", take)
    assert sends == [] and hooks == [1]
    fresh = await new.recover("r")
    assert fresh.pending_approval is not None
    assert fresh.pending_approval.approval_id != paused.pending_approval.approval_id
    assert fresh.pending_approval.input_state["amount"] == 500
    assert fresh.pending_approval.regate_reason == "definition_migrated"
    assert (await new.resume("r", fresh.pending_approval.approval_id)).status == "completed"
    assert sends == [500] and hooks == [1]


async def test_inconsistent_checkpoint_cannot_repeat_committed_hook() -> None:
    store = MemoryWorkflowStore()
    reg = ModuleRegistry()
    reg.register("fake", lambda s: {}, effect="pure")
    await WorkflowEngine(definition(), reg, store=store).run({"amount": 5}, workflow_run_id="r")
    original = await store.get("r")
    assert original is not None
    calls: list[int] = []
    target = definition(2)

    def migrate(a: Any, b: Any, state: dict[str, Any]) -> dict[str, Any]:
        calls.append(1)
        return {**state, "amount": 500}

    target.register_migration(1, 2, migrate)
    engine = WorkflowEngine(target, reg, store=store)
    migrated = await engine.migrate("r")
    # Reproduce a stale worker overwrite with a later revision than the receipt source.
    original.revision = migrated.revision
    await store.save(original)
    with pytest.raises(WorkflowMigrationError, match="refusing to repeat migration hooks"):
        await engine.migrate("r")
    assert calls == [1]


@pytest.mark.parametrize("operation", ["recover", "resume", "interrupt"])
async def test_checkpoint_digest_is_checked_again_under_lease(
    operation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryWorkflowStore()
    reg = ModuleRegistry()
    calls: list[int] = []

    def fake(state: dict[str, Any]) -> dict[str, Any]:
        if operation == "recover":
            raise Crash
        if operation == "interrupt":
            interrupt("local question")
        calls.append(1)
        return {}

    reg.register("fake", fake, effect="pure")
    engine = WorkflowEngine(definition(gated=operation == "resume"), reg, store=store)
    if operation == "recover":
        with pytest.raises(Crash):
            await engine.run({}, workflow_run_id="r")
        paused = None
    else:
        paused = await engine.run({}, workflow_run_id="r")
    take = engine._take_journal

    async def change_after_take(run_id: str) -> WorkflowJournal | None:
        journal = await take(run_id)
        cp = await store.get(run_id)
        assert cp is not None
        cp.spec_hash = "another-definition"
        await store.save(cp)
        return journal

    monkeypatch.setattr(engine, "_take_journal", change_after_take)
    with pytest.raises(WorkflowSpecMismatchError):
        if operation == "recover":
            await engine.recover("r")
        elif operation == "resume":
            assert paused is not None and paused.pending_approval is not None
            await engine.resume("r", paused.pending_approval.approval_id)
        else:
            assert paused is not None and paused.interrupt is not None
            await engine.resume_interrupt("r", paused.interrupt["interrupt_id"], "local answer")
    assert calls == []


@pytest.mark.parametrize("kind", ["approval", "interrupt"])
async def test_child_compatibility_refusal_precedes_parent_claim(kind: str) -> None:
    store = MemoryWorkflowStore()
    reg = ModuleRegistry()

    def action(state: dict[str, Any]) -> dict[str, Any]:
        return {"answer": interrupt("local question")} if kind == "interrupt" else {}

    reg.register("fake", action, effect="pure")
    child = definition(gated=kind == "approval")
    parent = WorkflowSpec(
        name="parent",
        version=1,
        entry_node="sub",
        nodes={
            "sub": WorkflowNode(id="sub", handler="", subgraph=child),
        },
    )
    engine = WorkflowEngine(parent, reg, store=store)
    paused = await engine.run({"amount": 5}, workflow_run_id="r")
    children = await store.list_children("r")
    children[0].spec_hash = "changed-child-definition"
    await store.save(children[0])
    if kind == "approval":
        assert paused.pending_approval is not None
        pending_id = paused.pending_approval.approval_id
        with pytest.raises(WorkflowSpecMismatchError):
            await engine.resume("r", pending_id)
    else:
        assert paused.interrupt is not None
        pending_id = paused.interrupt["interrupt_id"]
        with pytest.raises(WorkflowSpecMismatchError):
            await engine.resume_interrupt("r", pending_id, "local answer")
    checkpoint = await store.get("r")
    assert checkpoint is not None and checkpoint.status == "paused"
    assert await store.claim_approval("r", pending_id)


@pytest.mark.parametrize("child", [False, True])
async def test_completed_historical_nodes_do_not_block_migration(child: bool) -> None:
    store = MemoryWorkflowStore()
    reg = ModuleRegistry()
    reg.register("fake", lambda s: {}, effect="non_idempotent")
    target = definition(2)
    old = definition()
    if child:
        old.nodes["earlier"] = WorkflowNode(
            id="earlier", handler="", subgraph=definition(gated=False), default_transition="pay"
        )
    else:
        old.nodes["earlier"] = WorkflowNode(id="earlier", handler="fake", default_transition="pay")
    old.entry_node = "earlier"
    await WorkflowEngine(old, reg, store=store).run({"amount": 5}, workflow_run_id="r")
    target.register_migration(1, 2, lambda a, b, s: s)
    migrated = await WorkflowEngine(target, reg, store=store).migrate("r")
    assert migrated.workflow_version == 2 and migrated.current_node_id == "pay"


async def test_non_idempotent_resolved_before_migration_still_regates() -> None:
    store = MemoryWorkflowStore()
    reg = ModuleRegistry()
    sends: list[int] = []
    crash = True

    def fake(state: dict[str, Any]) -> dict[str, Any]:
        nonlocal crash
        if crash:
            crash = False
            raise Crash
        sends.append(state["amount"])
        return {}

    reg.register("fake", fake, effect="non_idempotent")
    old = WorkflowEngine(definition(), reg, store=store)
    paused = await old.run({"amount": 5}, workflow_run_id="r")
    assert paused.pending_approval is not None
    with pytest.raises(Crash):
        await old.resume("r", paused.pending_approval.approval_id)
    unknown = await old.recover("r")
    journal = await store.open_execution_store()
    view = await old._take_journal("r")
    assert view is not None and unknown.pending_effect is not None
    from nexura_agents.contracts.execution import EffectResolution

    await view.resolve(
        unknown.pending_effect["effect_id"],
        EffectResolution.NOT_APPLIED,
        actor="local-reviewer",
        result=None,
    )
    await view.close()
    target = definition(2, gated=False)
    target.register_migration(1, 2, lambda a, b, s: {**s, "amount": 500})
    new = WorkflowEngine(target, reg, store=store)
    await new.migrate("r")
    fresh = await new.recover("r")
    assert fresh.pending_approval is not None and sends == []
    assert fresh.pending_approval.regate_reason == "definition_migrated"
    assert (await journal.get(ExecutionId("default", "r"))).run.status == "paused"


def start_saved(client: TestClient) -> tuple[dict[str, Any], str]:
    saved = client.post(
        "/api/v1/workflows", json={"name": "review", "spec": definition().to_dict()}
    )
    assert saved.status_code == 201, saved.text
    workflow = saved.json()
    started = client.post(
        "/api/v1/runs",
        json={"workflow_id": workflow["workflow_id"], "initial_state": {"amount": 5}},
    )
    assert started.status_code == 201, started.text
    return workflow, started.json()["run_id"]


def update_saved(client: TestClient, workflow: dict[str, Any], target: WorkflowSpec) -> None:
    response = client.put(
        f"/api/v1/workflows/{workflow['workflow_id']}",
        json={
            "spec": target.to_dict(),
            "expected_revision": workflow["revision"],
        },
    )
    assert response.status_code == 200, response.text


def test_service_saved_target_audit_fork_history_and_batched_listing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reg = ModuleRegistry()
    reg.register("fake", lambda s: {}, effect="pure")
    app = create_api_app(registry=reg, db_path=tmp_path / "api.db", profile="local-owner")
    app.state.workflow_migrations = {"review": {(1, 2): lambda a, b, s: {**s, "amount": 500}}}
    with TestClient(app) as client:
        workflow, run_id = start_saved(client)
        target = definition(2)
        update_saved(client, workflow, target)
        refused = client.post(
            f"/api/v1/runs/{run_id}/migrate", json={"expected_definition_digest": "stale"}
        )
        assert refused.status_code == 409
        migrated = client.post(f"/api/v1/runs/{run_id}/migrate", json={})
        assert migrated.status_code == 200, migrated.text
        events = [e for e in migrated.json()["events"] if e["type"] == "RUN_DEFINITION_MIGRATED"]
        assert len(events) == 1
        assert events[0]["data"]["actor"] == "local-owner"
        assert events[0]["data"]["from_version"] == 1
        assert events[0]["data"]["to_version"] == 2
        assert events[0]["data"]["from_digest"] == definition().canonical_hash()
        assert events[0]["data"]["to_digest"] == target.canonical_hash()
        history = client.get(f"/api/v1/runs/{run_id}/history")
        assert history.status_code == 200
        assert history.json()["items"][-1]["spec_hash"] == target.canonical_hash()
        fork = client.post(f"/api/v1/runs/{run_id}/fork", json={"checkpoint_index": -1})
        assert fork.status_code in (200, 201), fork.text
        assert fork.json()["definition_digest"] == target.canonical_hash()

        async def forbid_get(run_id: str) -> Any:
            raise AssertionError("Listing must batch identity reads, not read every checkpoint")

        monkeypatch.setattr(app.state.store.checkpoint_store, "get", forbid_get)
        listed = client.get("/api/v1/runs")
        assert listed.status_code == 200, listed.text
        source = next(r for r in listed.json()["items"] if r["run_id"] == run_id)
        assert source["workflow_version"] == 2 and source["started_workflow_version"] == 1


@pytest.mark.parametrize("bad", ["name", "handler", "version", "hook", "nan"])
def test_service_migration_validation_is_clean(tmp_path: Path, bad: str) -> None:
    reg = ModuleRegistry()
    reg.register("fake", lambda s: {}, effect="pure")
    app = create_api_app(registry=reg, db_path=tmp_path / "api.db", profile="local-owner")
    with TestClient(app) as client:
        workflow, run_id = start_saved(client)
        target = definition(2)
        update_saved(client, workflow, target)
        if bad in ("name", "handler", "version"):
            # Simulate a definition persisted by an earlier importer, without API validation.
            if bad == "name":
                target.name = "some-other-workflow"
            elif bad == "handler":
                target.nodes["pay"].handler = "missing"
            else:
                target.version = 2.5  # type: ignore[assignment]
            assert client.portal is not None
            client.portal.call(app.state.store.update_workflow, workflow["workflow_id"], target, 2)
        elif bad == "hook":

            def broken(a: Any, b: Any, s: dict[str, Any]) -> dict[str, Any]:
                raise KeyError("local missing key")

            app.state.workflow_migrations = {"review": {(1, 2): broken}}
        else:
            app.state.workflow_migrations = {
                "review": {(1, 2): lambda a, b, s: {"nan": float("nan")}}
            }
        response = client.post(f"/api/v1/runs/{run_id}/migrate", json={})
        assert response.status_code == (409 if bad == "hook" else 422), response.text
        assert response.json()["detail"]["code"] == (
            "WORKFLOW_MIGRATION_REFUSED" if bad == "hook" else "VALIDATION_ERROR"
        )
        assert client.get(f"/api/v1/runs/{run_id}").json()["workflow_version"] == 1


def test_service_not_journaled_and_old_option_refusals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reg = ModuleRegistry()
    reg.register("fake", lambda s: {}, effect="pure")
    app = create_api_app(registry=reg, db_path=tmp_path / "api.db", profile="local-owner")
    with TestClient(app) as client:
        workflow, run_id = start_saved(client)
        for route in ("migrate", "recover"):
            response = client.post(
                f"/api/v1/runs/{run_id}/{route}", json={"continue_on_old_definition": True}
            )
            assert response.status_code == 422
        update_saved(client, workflow, definition(2))

        async def no_journal(state: Any) -> None:
            return None

        monkeypatch.setattr(runs, "run_journal", no_journal)
        response = client.post(f"/api/v1/runs/{run_id}/migrate", json={})
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "RUN_NOT_JOURNALED"


@pytest.mark.parametrize("backend", ["memory", "sqlite", "postgres"])
def test_batched_identities_preserve_starting_version(
    tmp_path: Path,
    request: pytest.FixtureRequest,
    backend: str,
) -> None:
    # A sync test: the Postgres fixture creates its schema with asyncio.run, which cannot
    # be called from a running event loop, so the async work runs in its own asyncio.run.
    store: Any
    if backend == "postgres":
        store = request.getfixturevalue("postgres_workflow_store")
    elif backend == "memory":
        store = MemoryWorkflowStore()
    else:
        store = SQLiteWorkflowStore(tmp_path / "state.db")
    reg = ModuleRegistry()
    reg.register("fake", lambda s: {}, effect="pure")

    async def check() -> None:
        try:
            await WorkflowEngine(definition(), reg, store=store).run({}, workflow_run_id="r")
            target = definition(2)
            target.register_migration(1, 2, lambda a, b, s: s)
            await WorkflowEngine(target, reg, store=store).migrate("r")
            assert getattr(store, "run_identities", None) is not None  # the batched path
            assert await workflow_run_identities(store, ["r", "missing"]) == {
                "r": {
                    "definition_digest": target.canonical_hash(),
                    "workflow_version": 2,
                    "started_workflow_version": 1,
                }
            }
        finally:
            await store.close()

    asyncio.run(check())


def test_migration_requires_edit_even_for_own_saved_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexura_runtime.secrets.keys import generate_master_key
    from nexura_runtime.service.auth.models import ActionPermission, Principal
    from nexura_runtime.service.extensions import Identity

    monkeypatch.setenv("NEXURA_MASTER_KEY", generate_master_key())
    scopes: list[ActionPermission] = ["run", "edit"]

    class LocalIdentity:
        name = "local-review-fake-identity"
        is_authenticating = True

        def authenticate(self, request: object, target_workspace_id: str | None) -> Identity:
            return Identity(
                Principal(
                    principal_id="editor",
                    profile="authenticated-shared-service",
                    scopes=list(scopes),
                ),
                "review-workspace",
                "editor",
            )

        def is_operator(self, request: object) -> bool:
            return False

    reg = ModuleRegistry()
    reg.register("fake", lambda s: {}, effect="pure")
    app = create_api_app(
        registry=reg,
        db_path=tmp_path / "api.db",
        profile="authenticated-shared-service",
        auth_provider=LocalIdentity(),
    )
    app.state.workflow_migrations = {"review": {(1, 2): lambda a, b, s: s}}
    with TestClient(app) as client:
        workflow, run_id = start_saved(client)
        update_saved(client, workflow, definition(2))
        scopes[:] = ["run"]
        response = client.post(f"/api/v1/runs/{run_id}/migrate", json={})
        assert response.status_code == 403, response.text
        assert client.get(f"/api/v1/runs/{run_id}").json()["workflow_version"] == 1


def test_migration_recovery_events_failure_recording_and_live_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reg = ModuleRegistry()
    reg.register("fake", lambda s: {}, effect="pure")
    app = create_api_app(registry=reg, db_path=tmp_path / "api.db", profile="local-owner")
    app.state.workflow_migrations = {"review": {(1, 2): lambda a, b, s: s}}
    with TestClient(app) as client:
        workflow, run_id = start_saved(client)
        update_saved(client, workflow, definition(2))

        async def make_interrupted() -> None:
            cp = await app.state.store.checkpoint_store.get(run_id)
            cp.status, cp.pending_approval = "interrupted", None
            await app.state.store.checkpoint_store.save(cp)
            await app.state.store.update_run(
                run_id, "interrupted", cp.current_node_id, cp.state, cp.step_count
            )

        assert client.portal is not None
        client.portal.call(make_interrupted)
        original_migrate = WorkflowEngine.migrate

        async def live_migrate(engine: WorkflowEngine, run_id: str) -> Any:
            from nexura_runtime.workflow import _LeaseBusyError

            raise _LeaseBusyError("local live lease")

        monkeypatch.setattr(WorkflowEngine, "migrate", live_migrate)
        live = client.post(f"/api/v1/runs/{run_id}/migrate", json={})
        assert live.status_code == 409 and live.json()["detail"]["code"] == "RUN_LEASE_HELD"
        monkeypatch.setattr(WorkflowEngine, "migrate", original_migrate)

        async def fail_recovery(engine: WorkflowEngine, run_id: str, **kwargs: Any) -> Any:
            assert (await app.state.store.get_run(run_id))["status"] == "running"
            raise RuntimeError("local recovery failure")

        monkeypatch.setattr(WorkflowEngine, "recover", fail_recovery)
        response = client.post(f"/api/v1/runs/{run_id}/migrate", json={})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["status"] == "failed" and result["workflow_version"] == 2
        types = [e["type"] for e in result["events"]]
        assert "RUN_DEFINITION_MIGRATED" in types
        assert "RUN_RECOVERY_STARTED" in types
        assert "RUN_FAILED" in types


def test_stale_process_blocked_between_snapshot_and_claim(tmp_path: Path) -> None:
    """Two real SQLite workers: migrate while the old worker waits before its claim."""
    database = tmp_path / "state.db"

    async def setup() -> None:
        store = SQLiteWorkflowStore(database)
        reg = ModuleRegistry()

        async def fake(state: dict[str, Any]) -> dict[str, Any]:
            raise Crash

        reg.register("fake", fake, effect="pure")
        try:
            with pytest.raises(Crash):
                await WorkflowEngine(definition(gated=False), reg, store=store).run(
                    {"amount": 5}, workflow_run_id="r"
                )
        finally:
            await store.close()

    asyncio.run(setup())
    worker = r"""
import asyncio, sys
from pathlib import Path
from nexura_agents.errors import WorkflowSpecMismatchError
from nexura_runtime._workflow_journal import WorkflowJournal
from nexura_runtime.workflow import (
    SQLiteWorkflowStore, ModuleRegistry, WorkflowEngine, WorkflowNode, WorkflowSpec,
)
root = Path(sys.argv[1])
async def main():
    store = SQLiteWorkflowStore(root / "state.db")
    reg = ModuleRegistry()
    async def fake(state):
        (root / "unapproved-send").write_text(str(state))
        return {}
    reg.register("fake", fake, effect="pure")
    spec = WorkflowSpec(name="review", version=1, entry_node="pay", nodes={
        "pay": WorkflowNode(id="pay", handler="fake")})
    take = WorkflowJournal.take
    async def block(journal, owner):
        (root / "before-claim").touch()
        while not (root / "release-claim").exists():
            await asyncio.sleep(0.01)
        return await take(journal, owner)
    WorkflowJournal.take = block
    try:
        try:
            await WorkflowEngine(spec, reg, store=store).recover("r")
        except WorkflowSpecMismatchError:
            (root / "refused").touch()
        else:
            raise AssertionError("stale definition was not refused")
    finally:
        await store.close()
asyncio.run(main())
"""
    process = subprocess.Popen(
        [sys.executable, "-c", worker, str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15
        while not (tmp_path / "before-claim").exists():
            assert process.poll() is None, process.communicate()
            assert time.monotonic() < deadline, "worker never reached snapshot-to-claim boundary"
            time.sleep(0.01)

        async def migrate() -> None:
            store = SQLiteWorkflowStore(database)
            reg = ModuleRegistry()
            reg.register("fake", lambda s: {}, effect="pure")
            target = definition(2, gated=False)
            target.register_migration(1, 2, lambda a, b, s: {**s, "amount": 500})
            try:
                cp = await WorkflowEngine(target, reg, store=store).migrate("r")
                assert cp.state["amount"] == 500
            finally:
                await store.close()

        asyncio.run(migrate())
        (tmp_path / "release-claim").touch()
        output = process.communicate(timeout=15)
        assert process.returncode == 0, output
        assert (tmp_path / "refused").exists()
        assert not (tmp_path / "unapproved-send").exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
