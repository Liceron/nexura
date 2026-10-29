"""A6 S8: PostgresWorkflowStore specifics and the service on Postgres workflow state.

Needs ``NEXURA_TEST_POSTGRES_URI`` (a disposable database); each test works in its own
schema, dropped afterwards. The store contract shared with memory and SQLite is in
``tests/workflows/test_workflow_store_contract.py``; this file covers what only Postgres
has: workspace scoping, versioned migrations, races between separate connection pools
(as between hosts), the journal on the same pool, event-loop changes, and
``create_api_app(workflow_state_url=...)`` end to end. Every handler is a local fake.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from _local_env import local_headers
from fastapi.testclient import TestClient
from nexura_agents.contracts.execution import ExecutionId
from nexura_agents.errors import WorkflowConflictError, WorkflowError
from nexura_postgres import PostgresWorkflowStore
from nexura_postgres.workflow_store import MIGRATIONS

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.workspaces.repository import WorkspaceRepository
from nexura_runtime.workflow import (
    ModuleRegistry,
    PendingApproval,
    WorkflowCheckpoint,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
)


async def _admin(uri: str, statement: str, *args: Any) -> Any:
    import asyncpg  # type: ignore[import-untyped]

    conn = await asyncpg.connect(uri)
    try:
        return await conn.fetch(statement, *args)
    finally:
        await conn.close()


class Schema:
    def __init__(self, uri: str) -> None:
        self.uri = uri
        self.name = f"wf_pg_{uuid.uuid4().hex[:12]}"
        self.dsn = f"{uri}{'&' if '?' in uri else '?'}search_path={self.name}"

    def fetch(self, statement: str, *args: Any) -> Any:
        return asyncio.run(_admin(self.dsn, statement, *args))


@pytest.fixture
def schema() -> Iterator[Schema]:
    uri = os.environ.get("NEXURA_TEST_POSTGRES_URI")
    if not uri:
        pytest.skip("NEXURA_TEST_POSTGRES_URI is not configured")
    made = Schema(uri)
    asyncio.run(_admin(uri, f'CREATE SCHEMA "{made.name}"'))
    try:
        yield made
    finally:
        asyncio.run(_admin(uri, f'DROP SCHEMA IF EXISTS "{made.name}" CASCADE'))


def cp(run_id: str, workspace_id: str | None = "ws", **overrides: Any) -> WorkflowCheckpoint:
    values: dict[str, Any] = {
        "workflow_run_id": run_id,
        "spec_version": 1,
        "spec_hash": "h",
        "current_node_id": "a",
        "state": {},
        "step_count": 1,
        "status": "running",
        "workspace_id": workspace_id,
    }
    values.update(overrides)
    return WorkflowCheckpoint(**values)


def test_constructor_validation() -> None:
    with pytest.raises(ValueError, match="DSN or pool"):
        PostgresWorkflowStore()
    with pytest.raises(ValueError, match="workspace_id"):
        PostgresWorkflowStore("postgresql://h/db", workspace_id="")
    with pytest.raises(ValueError, match="max_pool_size"):
        PostgresWorkflowStore("postgresql://h/db", max_pool_size=1)


# ------------------------------------------------------------------ workspace scoping


async def test_a_scoped_store_sees_and_writes_only_its_workspace(schema: Schema) -> None:
    shared = PostgresWorkflowStore(schema.dsn)
    alpha = PostgresWorkflowStore(schema.dsn, workspace_id="alpha")
    beta = PostgresWorkflowStore(schema.dsn, workspace_id="beta")
    try:
        assert await alpha.create(cp("a1", workspace_id=None))  # filled in from the scope
        got = await shared.get("a1")
        assert got is not None and got.workspace_id == "alpha"
        assert await beta.get("a1") is None
        assert await beta.history("a1") == []
        assert not await beta.create(cp("a1", workspace_id=None))  # id taken elsewhere
        with pytest.raises(WorkflowError, match="another workspace"):
            await beta.save(cp("a1", workspace_id=None, state={"stolen": True}))
        with pytest.raises(WorkflowError, match="belongs to workspace 'alpha'"):
            await beta.save(cp("b9", workspace_id="alpha"))
        got = await alpha.get("a1")
        assert got is not None and got.state == {}

        await alpha.save(cp("a1/n#1", workspace_id=None))
        assert [c.workflow_run_id for c in await alpha.list_children("a1")] == ["a1/n#1"]
        assert await beta.list_children("a1") == []
        assert await alpha.list_incomplete() == ["a1"]
        assert await beta.list_incomplete() == []
        assert await beta.list_incomplete(workspace_id="alpha") == []
        assert await shared.list_incomplete(workspace_id="alpha") == ["a1"]

        waiting = cp(
            "a2",
            workspace_id=None,
            status="paused",
            pending_approval=PendingApproval(
                approval_id="ap", workflow_run_id="a2", node_id="b", input_state={}
            ),
        )
        assert await alpha.create(waiting)
        assert not await beta.claim_approval("a2", "ap")
        assert await alpha.claim_approval("a2", "ap")
    finally:
        for store in (shared, alpha, beta):
            await store.close()


async def test_run_identities_read_a_page_within_the_workspace(schema: Schema) -> None:
    """P38 C6: active and starting identities in one query, scoped like every other read."""
    shared = PostgresWorkflowStore(schema.dsn)
    alpha = PostgresWorkflowStore(schema.dsn, workspace_id="alpha")
    beta = PostgresWorkflowStore(schema.dsn, workspace_id="beta")
    try:
        assert await alpha.run_identities([]) == {}
        assert await alpha.create(cp("m", workspace_id=None, spec_hash="h1", workflow_version=1))
        moved = cp("m", workspace_id=None, spec_hash="h2", workflow_version=2)
        await alpha.save(moved)
        assert await alpha.create(cp("u", workspace_id=None, spec_hash="h1"))
        expected = {
            "m": {
                "definition_digest": "h2",
                "workflow_version": 2,
                "started_workflow_version": 1,
            },
            "u": {
                "definition_digest": "h1",
                "workflow_version": None,
                "started_workflow_version": None,
            },
        }
        assert await alpha.run_identities(["m", "u", "missing"]) == expected
        assert await shared.run_identities(["m", "u"]) == expected
        assert await beta.run_identities(["m", "u"]) == {}
    finally:
        for store in (shared, alpha, beta):
            await store.close()


# ------------------------------------------------------------------ migrations


async def test_migrations_are_versioned_and_reapplying_is_a_no_op(schema: Schema) -> None:
    stores = [PostgresWorkflowStore(schema.dsn) for _ in range(3)]
    try:
        # Concurrent first use: one session migrates, the others wait on the lock.
        await asyncio.gather(*(store.init_db() for store in stores))
        rows = await _admin(schema.dsn, "SELECT version, name FROM nexura_workflow_migrations")
        assert [(r["version"], r["name"]) for r in rows] == [(v, n) for v, n, _ in MIGRATIONS]
        journal = await _admin(schema.dsn, "SELECT version FROM nexura_exec_migrations")
        assert journal  # the execution journal's migrations ran in the same database
        await stores[0].close()
        await stores[0].init_db()  # reopened after close: nothing applied twice
        again = await _admin(schema.dsn, "SELECT count(*) AS n FROM nexura_workflow_migrations")
        assert again[0]["n"] == len(MIGRATIONS)
    finally:
        for store in stores:
            await store.close()


async def test_a_newer_schema_version_is_refused(schema: Schema) -> None:
    store = PostgresWorkflowStore(schema.dsn)
    await store.init_db()
    await store.close()
    await _admin(
        schema.dsn,
        "INSERT INTO nexura_workflow_migrations (version, name, applied_at) "
        "VALUES (999, 'from the future', 0)",
    )
    newer = PostgresWorkflowStore(schema.dsn)
    try:
        with pytest.raises(WorkflowError, match="version 999"):
            await newer.get("x")
    finally:
        await newer.close()


# ------------------------------------------------------------------ races across pools


async def test_two_hosts_race_for_one_approval_and_one_revision(schema: Schema) -> None:
    host_a = PostgresWorkflowStore(schema.dsn)
    host_b = PostgresWorkflowStore(schema.dsn)
    try:
        waiting = cp(
            "r",
            status="paused",
            pending_approval=PendingApproval(
                approval_id="ap",
                workflow_run_id="r",
                node_id="b",
                input_state={},
                expires_at=time.time() + 600,
            ),
        )
        assert await host_a.create(waiting)
        claims = await asyncio.gather(
            *(store.claim_approval("r", "ap") for store in (host_a, host_b) for _ in range(4))
        )
        assert sorted(claims) == [False] * 7 + [True]
        current = await host_a.get("r")
        assert current is not None and current.revision == 2

        async def save(store: PostgresWorkflowStore, i: int) -> str:
            try:
                await store.save(cp("r", state={"writer": i}, revision=2))
            except WorkflowConflictError:
                return "conflict"
            return "saved"

        outcomes = await asyncio.gather(
            *(save(store, i) for i, store in enumerate([host_a, host_b] * 3))
        )
        assert sorted(outcomes) == ["conflict"] * 5 + ["saved"]
        creates = await asyncio.gather(
            *(store.create(cp("new", step_count=0)) for store in [host_a, host_b] * 3)
        )
        assert sorted(creates) == [False] * 5 + [True]
    finally:
        await host_a.close()
        await host_b.close()


# ------------------------------------------------------------------ journal and pools


def _spec() -> WorkflowSpec:
    return WorkflowSpec(
        name="pg",
        entry_node="a",
        nodes={
            "a": WorkflowNode(id="a", handler="note", default_transition="b"),
            "b": WorkflowNode(id="b", handler="pay", requires_approval=True),
        },
        terminal_nodes=["b"],
    )


def _registry(applied: list[str]) -> ModuleRegistry:
    reg = ModuleRegistry()

    def note(state: dict[str, Any]) -> dict[str, Any]:
        applied.append("note")
        return {"noted": True}

    def pay(state: dict[str, Any]) -> dict[str, Any]:
        applied.append("pay")
        return {"paid": True}

    reg.register("note", note, effect="pure")
    reg.register("pay", pay)
    return reg


async def test_the_engine_journals_on_the_stores_own_pool(schema: Schema) -> None:
    store = PostgresWorkflowStore(schema.dsn)
    try:
        journal = await store.open_execution_store()
        assert journal is store.execution_store
        assert await journal._get_pool() is await store._get_pool()
        applied: list[str] = []
        engine = WorkflowEngine(_spec(), _registry(applied), store=store, workspace_id="ws")
        paused = await engine.run({}, workflow_run_id="j1")
        assert paused.status == "paused" and paused.pending_approval is not None
        done = await engine.resume("j1", paused.pending_approval.approval_id)
        assert done.status == "completed" and applied == ["note", "pay"]
        snapshot = await journal.get(ExecutionId("ws", "j1"))
        assert snapshot.run.status == "completed"
        consumed = snapshot.run.state["consumed_approvals"]
        assert [c["approval_id"] for c in consumed] == [paused.pending_approval.approval_id]
        rows = await _admin(
            schema.dsn,
            "SELECT status FROM nexura_exec_runs WHERE workspace_id = 'ws' AND run_id = 'j1'",
        )
        assert [r["status"] for r in rows] == ["completed"]
        await journal.close()  # a no-op: the pool belongs to the workflow store
        assert await store.get("j1") is not None
    finally:
        await store.close()


def test_a_store_moves_to_a_new_event_loop(schema: Schema) -> None:
    store = PostgresWorkflowStore(schema.dsn)
    try:
        assert asyncio.run(store.create(cp("loop", step_count=0)))
        got = asyncio.run(store.get("loop"))  # the first loop is closed now
        assert got is not None and got.revision == 1
    finally:
        store.close_sync()


# ------------------------------------------------------------------ the service


def test_service_keeps_workflow_state_in_postgres(
    schema: Schema, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The A6 option (workflow state in Postgres, runs in SQLite): no service database_url,
    # under any --service-backend (with one, workflow state must be that database: MVP WP3).
    monkeypatch.delenv("NEXURA_DATABASE_URL", raising=False)
    db = tmp_path / "svc.sqlite3"
    repo = WorkspaceRepository()
    repo.create_workspace("wsA", "A", owner_id="local-owner")
    applied: list[str] = []
    app = create_api_app(
        registry=_registry(applied),
        db_path=db,
        workspace_repo=repo,
        profile="local-owner",
        workflow_state_url=schema.dsn,
    )
    headers = local_headers("wsA")
    with TestClient(app) as client:
        started = client.post(
            "/api/v1/runs",
            json={"spec": _spec().to_dict(), "initial_state": {}},
            headers=headers,
        )
        assert started.status_code == 201, started.text
        run = started.json()
        assert run["status"] == "pending_approval", run
        run_id = run["run_id"]
        approval_id = run["pending_approval"]["approval_id"]
        decided = client.post(
            f"/api/v1/runs/{run_id}/decide",
            json={"approval_id": approval_id, "decision": "approve"},
            headers=headers,
        )
        assert decided.status_code == 200, decided.text
        assert decided.json()["status"] == "completed"
        history = client.get(f"/api/v1/runs/{run_id}/history", headers=headers)
        assert history.status_code == 200, history.text
        effects = client.get(f"/api/v1/runs/{run_id}/effects", headers=headers)
        assert effects.status_code == 200, effects.text
        listed = effects.json()["effects"]  # read from the Postgres journal
        assert listed and all(e["status"] == "confirmed" for e in listed)
        gated = [e for e in listed if e["intent"]["approval_id"] is not None]
        assert [e["intent"]["approval_id"] for e in gated] == [approval_id]
        assert gated[0]["result"]["update"] == {"paid": True}
    assert applied == ["note", "pay"]

    rows = schema.fetch(
        "SELECT workspace_id, status FROM nexura_workflow_checkpoints WHERE workflow_run_id = $1",
        run_id,
    )
    assert [(r["workspace_id"], r["status"]) for r in rows] == [("wsA", "completed")]
    journal = schema.fetch("SELECT status FROM nexura_exec_runs WHERE run_id = $1", run_id)
    assert [r["status"] for r in journal] == ["completed"]
    with sqlite3.connect(db) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        status = conn.execute(
            "SELECT status FROM nexura_studio_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
    assert "nexura_workflow_checkpoints" not in tables  # nothing written to SQLite
    assert status == ("completed",)
