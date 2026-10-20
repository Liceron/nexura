"""REST coverage for interrupt() answers, checkpoint history, forks and sub-runs."""

from __future__ import annotations

import re
import sqlite3
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from _local_env import local_headers

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.workspaces.repository import WorkspaceRepository
from nexura_runtime.studio.store import StudioStore
from nexura_runtime.workflow import (
    MemoryWorkflowStore,
    ModuleRegistry,
    SQLiteWorkflowStore,
    WorkflowCheckpoint,
    WorkflowNode,
    WorkflowSpec,
    interrupt,
)


def _chain(name: str, *nodes: WorkflowNode) -> WorkflowSpec:
    linked: dict[str, WorkflowNode] = {}
    for index, node in enumerate(nodes):
        if index + 1 < len(nodes):
            node.default_transition = nodes[index + 1].id
        linked[node.id] = node
    return WorkflowSpec(
        name=name, entry_node=nodes[0].id, nodes=linked, terminal_nodes=[nodes[-1].id]
    )


ASK_SPEC = _chain(
    "AskFlow",
    WorkflowNode(id="prep", handler="prep"),
    WorkflowNode(id="ask", handler="ask"),
    WorkflowNode(id="finish", handler="finish"),
)
CHILD_SPEC = _chain(
    "Child",
    WorkflowNode(id="c1", handler="prep"),
    WorkflowNode(id="c2", handler="finish"),
)
PARENT_SPEC = _chain(
    "Parent",
    WorkflowNode(id="sub", handler="", subgraph=CHILD_SPEC),
    WorkflowNode(id="after", handler="finish"),
)


class Env:
    def __init__(self, client: httpx.AsyncClient, workspaces: dict[str, str], store: StudioStore):
        self.client = client
        self.workspaces = workspaces
        self.store = store

    def headers(self, who: str = "alpha") -> dict[str, str]:
        return local_headers(self.workspaces[who])

    async def start(
        self, spec: WorkflowSpec, state: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        res = await self.client.post(
            "/api/v1/runs",
            headers=self.headers(),
            json={"spec": spec.to_dict(), "initial_state": state or {}},
        )
        assert res.status_code == 201, res.text
        body: dict[str, Any] = res.json()
        return body


@pytest_asyncio.fixture
async def env() -> AsyncIterator[Env]:
    db_path = Path(tempfile.mkdtemp(prefix="nexura_rih_")) / "store.sqlite3"
    store = StudioStore(db_path)
    ws_repo = WorkspaceRepository(db_path=db_path)
    ws_repo.create_workspace("ws_alpha", "Alpha", owner_id="local-owner")
    ws_repo.create_workspace("ws_beta", "Beta", owner_id="local-owner")

    registry = ModuleRegistry()

    async def prep(state: dict[str, Any]) -> dict[str, Any]:
        return {"base": 1}

    async def ask(state: dict[str, Any]) -> dict[str, Any]:
        return {"answer": interrupt({"question": "Approve amount?", "base": state.get("base")})}

    async def finish(state: dict[str, Any]) -> dict[str, Any]:
        return {"total": state.get("base", 0) + state.get("bonus", 0)}

    registry.register("prep", prep)
    registry.register("ask", ask)
    registry.register("finish", finish)

    app = create_api_app(
        registry=registry,
        store=store,
        workspace_repo=ws_repo,
        profile="local-owner",
    )
    workspaces = {who: f"ws_{who}" for who in ("alpha", "beta")}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield Env(client, workspaces, store)
    await store.close()


async def _paused_run(env: Env) -> dict[str, Any]:
    run = await env.start(ASK_SPEC)
    assert run["status"] == "waiting_for_input", run
    return run


@pytest.mark.asyncio
async def test_interrupt_pauses_as_waiting_for_input_and_can_be_answered(env: Env) -> None:
    run = await _paused_run(env)
    pending = run["pending_input"]
    assert pending["node_id"] == "ask"
    assert pending["value"] == {"question": "Approve amount?", "base": 1}
    assert run["pending_approval"] is None
    assert any(e["type"] == "RUN_WAITING_FOR_INPUT" for e in run["events"])

    res = await env.client.post(
        f"/api/v1/runs/{run['run_id']}/input",
        headers=env.headers(),
        json={"interrupt_id": pending["interrupt_id"], "value": {"ok": True, "api_key": "sk-1"}},
    )
    assert res.status_code == 200, res.text
    done = res.json()
    assert done["status"] == "completed"
    assert done["pending_input"] is None
    assert done["current_state"]["answer"]["ok"] is True
    assert done["current_state"]["total"] == 1
    provided = [e for e in done["events"] if e["type"] == "INPUT_PROVIDED"]
    assert len(provided) == 1
    assert provided[0]["data"]["value"]["api_key"] == "[REDACTED]"
    assert any(e["type"] == "RUN_COMPLETED" for e in done["events"])

    listed = await env.client.get("/api/v1/runs?status=completed", headers=env.headers())
    assert run["run_id"] in [r["run_id"] for r in listed.json()["items"]]


@pytest.mark.asyncio
async def test_answering_twice_is_a_conflict(env: Env) -> None:
    run = await _paused_run(env)
    url = f"/api/v1/runs/{run['run_id']}/input"
    body = {"interrupt_id": run["pending_input"]["interrupt_id"], "value": "yes"}
    assert (await env.client.post(url, headers=env.headers(), json=body)).status_code == 200
    second = await env.client.post(url, headers=env.headers(), json=body)
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "NOT_WAITING_FOR_INPUT"


@pytest.mark.asyncio
async def test_lost_claim_is_already_answered(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    run = await _paused_run(env)

    async def lost(workflow_run_id: str, approval_id: str) -> bool:
        return False

    monkeypatch.setattr(env.store.checkpoint_store, "claim_approval", lost)
    res = await env.client.post(
        f"/api/v1/runs/{run['run_id']}/input",
        headers=env.headers(),
        json={"interrupt_id": run["pending_input"]["interrupt_id"], "value": 1},
    )
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "ALREADY_ANSWERED"


@pytest.mark.asyncio
async def test_wrong_interrupt_id_and_unknown_run_are_404(env: Env) -> None:
    run = await _paused_run(env)
    res = await env.client.post(
        f"/api/v1/runs/{run['run_id']}/input",
        headers=env.headers(),
        json={"interrupt_id": "nope", "value": 1},
    )
    assert res.status_code == 404
    assert res.json()["detail"]["code"] == "INTERRUPT_NOT_FOUND"
    still = (await env.client.get(f"/api/v1/runs/{run['run_id']}", headers=env.headers())).json()
    assert still["status"] == "waiting_for_input"

    for method, suffix in (("post", "input"), ("get", "history"), ("get", "subruns")):
        kwargs: dict[str, Any] = (
            {"json": {"interrupt_id": "x", "value": 1}} if method == "post" else {}
        )
        res = await getattr(env.client, method)(
            f"/api/v1/runs/missing/{suffix}", headers=env.headers(), **kwargs
        )
        assert res.status_code == 404, suffix
    res = await env.client.post(
        "/api/v1/runs/missing/fork", headers=env.headers(), json={"checkpoint_index": 0}
    )
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_other_workspace_is_denied_everywhere(env: Env) -> None:
    run = await _paused_run(env)
    rid = run["run_id"]
    other = env.headers("beta")
    answer = await env.client.post(
        f"/api/v1/runs/{rid}/input",
        headers=other,
        json={"interrupt_id": run["pending_input"]["interrupt_id"], "value": 1},
    )
    history = await env.client.get(f"/api/v1/runs/{rid}/history", headers=other)
    subruns = await env.client.get(f"/api/v1/runs/{rid}/subruns", headers=other)
    fork = await env.client.post(
        f"/api/v1/runs/{rid}/fork", headers=other, json={"checkpoint_index": 0}
    )
    for res in (answer, history, subruns, fork):
        assert res.status_code == 403, res.text
        assert res.json()["detail"]["code"] == "CROSS_WORKSPACE_ACCESS_DENIED"
        assert f"'{env.workspaces['alpha']}'" in res.json()["detail"]["message"]
        assert "items" not in res.json() and "run_id" not in res.json()
    still = (await env.client.get(f"/api/v1/runs/{rid}", headers=env.headers())).json()
    assert still["status"] == "waiting_for_input"
    beta_runs = (await env.client.get("/api/v1/runs", headers=other)).json()["items"]
    assert beta_runs == []


@pytest.mark.asyncio
async def test_history_lists_checkpoints(env: Env) -> None:
    run = await _paused_run(env)
    res = await env.client.get(f"/api/v1/runs/{run['run_id']}/history", headers=env.headers())
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) >= 2
    assert items[0]["workflow_run_id"] == run["run_id"]
    assert [i["step_count"] for i in items] == sorted(i["step_count"] for i in items)
    assert items[-1]["status"] == "paused"
    assert items[-1]["interrupt"]["node_id"] == "ask"


@pytest.mark.asyncio
async def test_fork_with_state_update_creates_a_distinct_run(env: Env) -> None:
    run = await _paused_run(env)
    rid = run["run_id"]
    iid = run["pending_input"]["interrupt_id"]
    done = (
        await env.client.post(
            f"/api/v1/runs/{rid}/input",
            headers=env.headers(),
            json={"interrupt_id": iid, "value": 1},
        )
    ).json()
    assert done["current_state"]["total"] == 1

    history = (await env.client.get(f"/api/v1/runs/{rid}/history", headers=env.headers())).json()
    index = next(i for i, cp in enumerate(history["items"]) if cp["current_node_id"] == "finish")

    res = await env.client.post(
        f"/api/v1/runs/{rid}/fork",
        headers=env.headers(),
        json={"checkpoint_index": index, "state_update": {"bonus": 10}},
    )
    assert res.status_code == 201, res.text
    forked = res.json()
    assert forked["run_id"] != rid
    assert forked["status"] == "completed"
    assert forked["current_state"]["total"] == 11
    assert forked["forked_from"]["workflow_run_id"] == rid
    assert forked["forked_from"]["checkpoint_index"] == index
    assert forked["forked_from"]["state_changed"] is True
    assert any(e["type"] == "RUN_FORKED" for e in forked["events"])

    source = (await env.client.get(f"/api/v1/runs/{rid}", headers=env.headers())).json()
    assert source["current_state"]["total"] == 1
    assert source["status"] == "completed"
    fetched = (
        await env.client.get(f"/api/v1/runs/{forked['run_id']}", headers=env.headers())
    ).json()
    assert fetched["forked_from"]["workflow_run_id"] == rid
    # The caller's workspace owns the fork, so the other workspace is denied.
    denied = await env.client.get(f"/api/v1/runs/{forked['run_id']}", headers=env.headers("beta"))
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "CROSS_WORKSPACE_ACCESS_DENIED"
    assert f"'{env.workspaces['alpha']}'" in denied.json()["detail"]["message"]


@pytest.mark.asyncio
async def test_fork_of_an_interrupt_replays_to_waiting_for_input(env: Env) -> None:
    run = await _paused_run(env)
    res = await env.client.post(
        f"/api/v1/runs/{run['run_id']}/fork", headers=env.headers(), json={"checkpoint_index": 0}
    )
    assert res.status_code == 201, res.text
    assert res.json()["status"] == "waiting_for_input"
    assert res.json()["pending_input"]["node_id"] == "ask"


@pytest.mark.asyncio
async def test_fork_rejects_bad_indexes_and_leaves_no_run_behind(env: Env) -> None:
    run = await _paused_run(env)
    iid = run["pending_input"]["interrupt_id"]
    await env.client.post(
        f"/api/v1/runs/{run['run_id']}/input",
        headers=env.headers(),
        json={"interrupt_id": iid, "value": 1},
    )
    before = len((await env.client.get("/api/v1/runs", headers=env.headers())).json()["items"])
    for index in (999, -999, -1):  # out of range both ways, and the end of the run
        res = await env.client.post(
            f"/api/v1/runs/{run['run_id']}/fork",
            headers=env.headers(),
            json={"checkpoint_index": index},
        )
        assert res.status_code == 400, (index, res.text)
    after = len((await env.client.get("/api/v1/runs", headers=env.headers())).json()["items"])
    assert after == before


@pytest.mark.asyncio
async def test_subruns_lists_children_of_a_sub_workflow_node(env: Env) -> None:
    run = await env.start(PARENT_SPEC)
    assert run["status"] == "completed", run
    res = await env.client.get(f"/api/v1/runs/{run['run_id']}/subruns", headers=env.headers())
    assert res.status_code == 200
    items = res.json()["items"]
    ids = [i["workflow_run_id"] for i in items]
    # Natural order (Audit 13): numeric parts compare as numbers, so "#2" comes before "#10".
    natural = sorted(
        ids, key=lambda s: [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]
    )
    assert ids == natural
    assert items
    assert all(i["workflow_run_id"].startswith(f"{run['run_id']}/sub#") for i in items)
    assert all(i["status"] == "completed" for i in items)

    plain = await _paused_run(env)
    none = await env.client.get(f"/api/v1/runs/{plain['run_id']}/subruns", headers=env.headers())
    assert none.json()["items"] == []
    assert none.json()["has_more"] is False


def test_openapi_lists_new_endpoints_and_status(env: Env) -> None:
    app = env.client._transport.app  # type: ignore[attr-defined]
    schema = app.openapi()
    paths = schema["paths"]
    for suffix, method in (
        ("input", "post"),
        ("history", "get"),
        ("fork", "post"),
        ("subruns", "get"),
    ):
        assert method in paths[f"/api/v1/runs/{{run_id}}/{suffix}"]
    props = schema["components"]["schemas"]["RunResponse"]["properties"]
    assert "pending_input" in props
    assert "waiting_for_input" in props["status"]["description"]


@pytest.mark.asyncio
async def test_existing_database_without_the_new_columns_still_loads(tmp_path: Path) -> None:
    db = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE nexura_studio_runs (
            run_id TEXT PRIMARY KEY, workflow_id TEXT, spec_hash TEXT NOT NULL,
            spec_json TEXT NOT NULL, status TEXT NOT NULL, current_node_id TEXT,
            input_state_json TEXT NOT NULL, current_state_json TEXT NOT NULL,
            step_count INTEGER NOT NULL DEFAULT 0, approval_id TEXT, approval_json TEXT,
            error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL)"""
    )
    conn.execute(
        "INSERT INTO nexura_studio_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "run-old", None, ASK_SPEC.canonical_hash(), ASK_SPEC.to_json(), "completed",
            None, "{}", '{"x": 1}', 2, None, None, None, 1.0, 2.0,
        ),
    )  # fmt: skip
    conn.commit()
    conn.close()

    store = StudioStore(db)
    try:
        run = await store.get_run("run-old")
        assert run is not None
        assert run["status"] == "completed"
        assert run["pending_input"] is None
        assert run["forked_from"] is None
        assert [r["run_id"] for r in await store.list_runs()] == ["run-old"]
        await store.update_run(
            "run-old", "waiting_for_input", "ask", {"x": 1}, 2,
            pending_input={"interrupt_id": "i1", "node_id": "ask", "value": 5},
        )  # fmt: skip
        again = await store.get_run("run-old")
        assert again is not None
        assert again["pending_input"]["interrupt_id"] == "i1"
    finally:
        await store.close()
    StudioStore(db)._close_sync()  # reopening an already migrated database is fine


def _cp(run_id: str) -> WorkflowCheckpoint:
    return WorkflowCheckpoint(
        workflow_run_id=run_id,
        spec_version=1,
        spec_hash="h",
        current_node_id=None,
        state={},
        step_count=1,
        status="completed",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_list_children_matches_only_real_children_sorted(
    backend: str, tmp_path: Path
) -> None:
    store: Any = (
        MemoryWorkflowStore() if backend == "memory" else SQLiteWorkflowStore(tmp_path / "c.db")
    )
    ids = [
        "a_b/n#2",
        "a_b/n#1",
        "a_b/n#1[0]",
        "aXb/n#1",  # '_' must not act as a wildcard
        "a_b",  # the parent itself is not a child
        "a_bc/n#1",  # a different run that merely shares the prefix
        "A_B/n#1",  # LIKE is case-insensitive in SQLite; ids are not
        "100%/n#1",
        "100x/n#1",  # '%' must not act as a wildcard
        "back\\slash/n#1",
        "back\\\\slash/n#1",
    ]
    for run_id in ids:
        await store.save(_cp(run_id))
    try:
        assert [c.workflow_run_id for c in await store.list_children("a_b")] == [
            "a_b/n#1",
            "a_b/n#1[0]",
            "a_b/n#2",
        ]
        assert [c.workflow_run_id for c in await store.list_children("100%")] == ["100%/n#1"]
        assert [c.workflow_run_id for c in await store.list_children("back\\slash")] == [
            "back\\slash/n#1"
        ]
        assert await store.list_children("nothing") == []
    finally:
        await store.close()


async def _always_false() -> bool:
    return False


@pytest.mark.asyncio
async def test_lost_claim_does_not_record_input_provided(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = await _paused_run(env)
    monkeypatch.setattr(env.store.checkpoint_store, "claim_approval", lambda *a: _always_false())
    res = await env.client.post(
        f"/api/v1/runs/{run['run_id']}/input",
        headers=env.headers(),
        json={"interrupt_id": run["pending_input"]["interrupt_id"], "value": "LOSER"},
    )
    assert res.status_code == 409
    got = (await env.client.get(f"/api/v1/runs/{run['run_id']}", headers=env.headers())).json()
    assert [e for e in got["events"] if e["type"] == "INPUT_PROVIDED"] == []


@pytest.mark.asyncio
async def test_successful_answer_records_input_provided_once(env: Env) -> None:
    run = await _paused_run(env)
    res = await env.client.post(
        f"/api/v1/runs/{run['run_id']}/input",
        headers=env.headers(),
        json={"interrupt_id": run["pending_input"]["interrupt_id"], "value": 5},
    )
    assert res.status_code == 200, res.text
    events = [e for e in res.json()["events"] if e["type"] == "INPUT_PROVIDED"]
    assert len(events) == 1
