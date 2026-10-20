"""Audit 3 platform regressions: reference scoping, nested approval policy, retry, child order."""

from __future__ import annotations

import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from _local_env import local_headers
from nexura_agents.errors import WorkflowExecutionError

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.triggers.models import DurableTriggerJob
from nexura_runtime.service.triggers.worker import TriggerWorker
from nexura_runtime.service.workspaces.repository import WorkspaceRepository
from nexura_runtime.studio.store import StudioStore
from nexura_runtime.workflow import (
    MemoryWorkflowStore,
    ModuleRegistry,
    SQLiteWorkflowStore,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
    interrupt,
)

BASE = "/api/v1"


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
    WorkflowNode(id="ask", handler="ask"),
    WorkflowNode(id="finish", handler="finish"),
)
GATE_SPEC = _chain(
    "GateFlow",
    WorkflowNode(id="pre", handler="finish"),
    WorkflowNode(id="gate", handler="finish", requires_approval=True),
    WorkflowNode(id="end", handler="finish"),
)


@pytest_asyncio.fixture
async def env() -> AsyncIterator[tuple[httpx.AsyncClient, dict[str, str], Any]]:
    db_path = Path(tempfile.mkdtemp(prefix="nexura_a3_")) / "store.sqlite3"
    store = StudioStore(db_path)
    ws_repo = WorkspaceRepository(db_path=db_path)
    ws_repo.create_workspace("ws_alpha", "Alpha", owner_id="local-owner")
    ws_repo.create_workspace("ws_beta", "Beta", owner_id="local-owner")
    ws_repo.record_resource_ownership("credential", "cred-beta", "ws_beta", "user_beta")

    registry = ModuleRegistry()

    async def ask(state: dict[str, Any]) -> dict[str, Any]:
        return {"answer": interrupt({"question": "ok?"})}

    async def finish(state: dict[str, Any]) -> dict[str, Any]:
        return {"total": 1}

    registry.register("ask", ask)
    registry.register("finish", finish)
    app = create_api_app(
        registry=registry,
        store=store,
        workspace_repo=ws_repo,
        profile="local-owner",
    )
    workspaces = {who: f"ws_{who}" for who in ("alpha",)}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client, local_headers(workspaces["alpha"]), app
    await store.close()


async def _start(client: httpx.AsyncClient, headers: dict[str, str], spec: WorkflowSpec) -> Any:
    res = await client.post(
        f"{BASE}/runs", headers=headers, json={"spec": spec.to_dict(), "initial_state": {}}
    )
    assert res.status_code == 201, res.text
    return res.json()


async def test_fork_state_update_rejects_foreign_credential(env: Any) -> None:
    client, headers, _ = env
    run = await _start(client, headers, ASK_SPEC)
    res = await client.post(
        f"{BASE}/runs",
        headers=headers,
        json={"spec": ASK_SPEC.to_dict(), "initial_state": {"credential_id": "cred-beta"}},
    )
    assert res.status_code == 403
    res = await client.post(
        f"{BASE}/runs/{run['run_id']}/fork",
        headers=headers,
        json={"checkpoint_index": 0, "state_update": {"credential_id": "cred-beta"}},
    )
    assert res.status_code == 403, res.text


async def test_input_value_rejects_foreign_credential(env: Any) -> None:
    client, headers, _ = env
    run = await _start(client, headers, ASK_SPEC)
    assert run["status"] == "waiting_for_input"
    res = await client.post(
        f"{BASE}/runs/{run['run_id']}/input",
        headers=headers,
        json={
            "interrupt_id": run["pending_input"]["interrupt_id"],
            "value": {"credential_id": "cred-beta"},
        },
    )
    assert res.status_code == 403, res.text
    after = (await client.get(f"{BASE}/runs/{run['run_id']}", headers=headers)).json()
    assert after["status"] == "waiting_for_input"


async def test_decide_state_update_rejects_foreign_credential(env: Any) -> None:
    client, headers, _ = env
    run = await _start(client, headers, GATE_SPEC)
    assert run["status"] == "pending_approval", run
    res = await client.post(
        f"{BASE}/runs/{run['run_id']}/decide",
        headers=headers,
        json={
            "approval_id": run["pending_approval"]["approval_id"],
            "decision": "approve",
            "state_update": {"credential_id": "cred-beta"},
        },
    )
    assert res.status_code == 403, res.text
    after = (await client.get(f"{BASE}/runs/{run['run_id']}", headers=headers)).json()
    assert after["status"] == "pending_approval"


# ---- platform_5 -------------------------------------------------------------------------------

_HIDDEN_SUBGRAPH = {
    "name": "P",
    "entry_node": "s",
    "terminal_nodes": ["s"],
    "nodes": {
        "s": {
            "id": "s",
            "handler": "",
            "subgraph": {
                "name": "c",
                "entry_node": "g",
                "terminal_nodes": ["g"],
                "nodes": {"g": {"id": "g", "handler": "gated", "requires_approval": False}},
            },
        }
    },
}
_HIDDEN_PARALLEL = {
    "name": "Q",
    "entry_node": "s",
    "terminal_nodes": ["s"],
    "nodes": {"s": {"id": "s", "handler": "", "parallel": ["gated", "echo"]}},
}


def _gated_app(tmp_path: Path) -> Any:
    reg = ModuleRegistry()

    async def gated(state: dict[str, Any]) -> dict[str, Any]:
        return {"done": True}

    async def echo(state: dict[str, Any]) -> dict[str, Any]:
        return {"e": 1}

    reg.register("gated", gated)
    reg.register("echo", echo)
    return create_api_app(
        registry=reg,
        db_path=str(tmp_path / "a.db"),
        profile="local-owner",
        approval_required_handlers=frozenset({"gated"}),
    )


@pytest.mark.parametrize("spec", [_HIDDEN_SUBGRAPH, _HIDDEN_PARALLEL])
async def test_post_runs_rejects_nested_gated_handler_without_orphan(
    tmp_path: Path, spec: dict[str, Any]
) -> None:
    app = _gated_app(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://t",
        headers={"X-Workspace-ID": "default"},
    ) as c:
        res = await c.post(f"{BASE}/runs", json={"spec": spec, "initial_state": {}})
        assert res.status_code in (400, 422), (res.status_code, res.text[:200])
        assert (await c.get(f"{BASE}/runs")).json()["items"] == []


async def test_worker_nested_gated_handler_fails_job_without_running_run(tmp_path: Path) -> None:
    app = _gated_app(tmp_path)
    wf = await app.state.store.create_workflow(WorkflowSpec.from_dict(_HIDDEN_SUBGRAPH))
    app.state.trigger_store.enqueue_trigger_job(
        DurableTriggerJob(
            job_id="job_nested",
            workspace_id="default",
            trigger_type="schedule",
            trigger_source_id="s1",
            workflow_id=wf["workflow_id"],
            release_id="current",
            payload={},
            created_at=1.0,
        )
    )
    outcomes = await TriggerWorker(app.state).run_once(now=1.0)
    assert [o.status for o in outcomes] == ["failed"]
    runs = await app.state.store.list_runs()
    assert [r for r in runs if r["status"] == "running"] == []
    assert runs == []


# ---- platform_6 -------------------------------------------------------------------------------


async def test_retry_on_matches_error_inside_subworkflow() -> None:
    calls: list[int] = []
    reg = ModuleRegistry()

    def flaky(state: dict[str, Any]) -> dict[str, Any]:
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionError("transient")
        return {"ok": True}

    reg.register("flaky", flaky)
    child = WorkflowSpec(
        name="c",
        entry_node="a",
        nodes={"a": WorkflowNode(id="a", handler="flaky")},
        terminal_nodes=["a"],
    )
    parent = WorkflowSpec(
        name="p",
        entry_node="s",
        nodes={
            "s": WorkflowNode(
                id="s",
                handler="",
                subgraph=child,
                retry={"max_attempts": 3, "initial_delay": 0.01, "retry_on": ["ConnectionError"]},
            )
        },
        terminal_nodes=["s"],
    )
    res = await WorkflowEngine(parent, reg, store=MemoryWorkflowStore()).run({})
    assert res.status == "completed" and len(calls) == 3


async def test_retry_on_does_not_match_unrelated_subworkflow_error() -> None:
    calls: list[int] = []
    reg = ModuleRegistry()

    def bad(state: dict[str, Any]) -> dict[str, Any]:
        calls.append(1)
        raise ValueError("permanent")

    reg.register("bad", bad)
    child = WorkflowSpec(
        name="c",
        entry_node="a",
        nodes={"a": WorkflowNode(id="a", handler="bad")},
        terminal_nodes=["a"],
    )
    parent = WorkflowSpec(
        name="p",
        entry_node="s",
        nodes={
            "s": WorkflowNode(
                id="s",
                handler="",
                subgraph=child,
                retry={"max_attempts": 3, "initial_delay": 0.01, "retry_on": ["ConnectionError"]},
            )
        },
        terminal_nodes=["s"],
    )
    with pytest.raises(WorkflowExecutionError):
        await WorkflowEngine(parent, reg, store=MemoryWorkflowStore()).run({})
    assert len(calls) == 1


# ---- platform_4 -------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["sqlite", "memory"])
async def test_children_in_item_order(tmp_path: Path, kind: str) -> None:
    reg = ModuleRegistry()

    async def f(state: dict[str, Any]) -> dict[str, Any]:
        return {"v": state["item"]}

    reg.register("f", f)
    child = WorkflowSpec(
        name="c",
        entry_node="a",
        nodes={"a": WorkflowNode(id="a", handler="f")},
        terminal_nodes=["a"],
    )
    parent = WorkflowSpec(
        name="p",
        entry_node="m",
        nodes={
            "m": WorkflowNode(
                id="m",
                handler="",
                subgraph=child,
                map_over="items",
                map_as="item",
                reducers={"v": "append"},
            )
        },
        terminal_nodes=["m"],
    )
    store: Any = (
        SQLiteWorkflowStore(tmp_path / "s.db") if kind == "sqlite" else MemoryWorkflowStore()
    )
    await WorkflowEngine(parent, reg, store=store).run(
        {"items": list(range(12))}, workflow_run_id="r"
    )
    ids = [c.workflow_run_id for c in await store.list_children("r")]
    assert ids == [f"r/m#1[{i}]" for i in range(12)]
