"""P18: cancellation ends a run for good, and stops sub-workflows mid-step.

A cancelled run must not be revived by a late approval, answer or resume, and cancelling a
workflow must stop the work its sub-workflow is doing (n8n-style "resume after cancel" class).
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.workflow import (
    MemoryWorkflowStore,
    ModuleRegistry,
    WorkflowEngine,
    WorkflowNode,
    WorkflowSpec,
    interrupt,
)

# The fixture workflows below were imported from test_cve_patterns_runs.py, whose cases need the
# shared profile and moved to the closed edition in EDITIONS_PLAN step 7. Same definitions.


def _registry() -> ModuleRegistry:
    reg = ModuleRegistry()

    async def prep(state: dict[str, Any]) -> dict[str, Any]:
        return {"base": 1}

    async def ask(state: dict[str, Any]) -> dict[str, Any]:
        return {"answer": interrupt({"question": "ok?"})}

    async def send(state: dict[str, Any]) -> dict[str, Any]:
        return {"sent": True}

    async def fin(state: dict[str, Any]) -> dict[str, Any]:
        return {"done": True}

    for n, f in (("prep", prep), ("ask", ask), ("send", send), ("fin", fin)):
        reg.register(n, f)
    return reg


def _chain(name: str, *nodes: WorkflowNode) -> dict[str, Any]:
    for i, n in enumerate(nodes[:-1]):
        n.default_transition = nodes[i + 1].id
    return WorkflowSpec(
        name=name,
        entry_node=nodes[0].id,
        nodes={n.id: n for n in nodes},
        terminal_nodes=[nodes[-1].id],
    ).to_dict()


ASK = _chain(
    "ask",
    WorkflowNode(id="prep", handler="prep"),
    WorkflowNode(id="ask", handler="ask"),
    WorkflowNode(id="fin", handler="fin"),
)


APPR = _chain(
    "appr",
    WorkflowNode(id="prep", handler="prep"),
    WorkflowNode(id="send", handler="send", requires_approval=True),
    WorkflowNode(id="fin", handler="fin"),
)


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_api_app(registry=_registry(), db_path=tmp_path / "s.db"))


def _start(c: TestClient, spec: dict[str, Any]) -> dict[str, Any]:
    r = c.post("/api/v1/runs", json={"spec": spec, "initial_state": {}})
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


def test_cancelled_approval_run_cannot_be_approved(tmp_path: Path) -> None:
    c = _client(tmp_path)
    run = _start(c, APPR)
    assert run["status"] == "pending_approval"
    approval_id = run["pending_approval"]["approval_id"]
    assert c.post(f"/api/v1/runs/{run['run_id']}/cancel", json={}).status_code == 200
    r = c.post(
        f"/api/v1/runs/{run['run_id']}/decide",
        json={"approval_id": approval_id, "decision": "approve"},
    )
    assert r.status_code in (400, 404, 409), r.text
    assert c.get(f"/api/v1/runs/{run['run_id']}").json()["status"] == "cancelled"


def test_cancelled_waiting_run_cannot_be_answered(tmp_path: Path) -> None:
    c = _client(tmp_path)
    run = _start(c, ASK)
    assert run["status"] == "waiting_for_input"
    assert c.post(f"/api/v1/runs/{run['run_id']}/cancel", json={}).status_code == 200
    interrupt_id = (run.get("pending_input") or {}).get("interrupt_id", "x")
    r = c.post(
        f"/api/v1/runs/{run['run_id']}/input",
        json={"interrupt_id": interrupt_id, "value": "yes"},
    )
    assert r.status_code in (400, 404, 409), r.text
    assert c.get(f"/api/v1/runs/{run['run_id']}").json()["status"] == "cancelled"


def test_cancelled_run_cannot_be_cancelled_twice(tmp_path: Path) -> None:
    c = _client(tmp_path)
    run = _start(c, APPR)
    assert c.post(f"/api/v1/runs/{run['run_id']}/cancel", json={}).status_code == 200
    assert c.post(f"/api/v1/runs/{run['run_id']}/cancel", json={}).status_code == 409


async def test_cancelling_a_workflow_stops_its_subworkflow_mid_step() -> None:
    started = asyncio.Event()
    finished: list[str] = []

    async def slow(state: dict[str, Any]) -> dict[str, Any]:
        started.set()
        await asyncio.sleep(30)
        finished.append("slow")
        return {"x": 1}

    async def after(state: dict[str, Any]) -> dict[str, Any]:
        finished.append("after")
        return {}

    reg = ModuleRegistry()
    reg.register("slow", slow)
    reg.register("after", after)
    inner = WorkflowSpec(
        name="inner", entry_node="s", nodes={"s": WorkflowNode(id="s", handler="slow")}
    )
    outer = WorkflowSpec(
        name="outer",
        entry_node="sub",
        nodes={
            "sub": WorkflowNode(id="sub", handler="", subgraph=inner, default_transition="a"),
            "a": WorkflowNode(id="a", handler="after"),
        },
    )
    engine = WorkflowEngine(outer, reg, store=MemoryWorkflowStore())
    task = asyncio.create_task(engine.run({}))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert finished == []
