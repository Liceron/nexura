"""B06 and REST final audit regressions; local fake model and in-process ASGI only."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from nexura_agents import Agent, FakeModel, Runner, ToolCall, tool
from nexura_agents.repro import ReproRecorder, export_repro_bundle, replay_repro_bundle
from nexura_agents.tools import StopAtTools

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.workflow import ModuleRegistry, WorkflowNode, WorkflowSpec


@pytest.mark.asyncio
async def test_recording_preserves_stop_at_tools() -> None:
    @tool
    async def answer() -> str:
        return "42"

    agent = Agent(
        "a",
        "",
        FakeModel(responses=[[ToolCall("c", "answer", {})]]),
        tools=[answer],
        tool_use_behavior=StopAtTools(["answer"]),
    )
    recorder = ReproRecorder()
    original = await recorder.run_agent(Runner(), agent, "go")
    assert original.status == "completed"
    assert original.output == "42"
    bundle = export_repro_bundle(recorder)
    assert bundle.data["spec"]["tool_use_behavior"] == {"stop_at_tools": ["answer"]}
    replay = await replay_repro_bundle(bundle)
    assert replay.reproduced, replay.first_divergence


@pytest.fixture
async def service(tmp_path: Path) -> AsyncIterator[tuple[Any, httpx.AsyncClient, list[Any]]]:
    dispatched: list[Any] = []

    async def echo(state: dict[str, Any]) -> dict[str, Any]:
        dispatched.append(state)
        return {"done": True}

    registry = ModuleRegistry()
    registry.register("echo", echo)
    app = create_api_app(db_path=tmp_path / "service.sqlite3", registry=registry)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 5000)),
            base_url="http://127.0.0.1",
            headers={"X-Nexura-Session": app.state.session_token},
        ) as client,
    ):
        yield app, client, dispatched


@pytest.mark.asyncio
@pytest.mark.parametrize("background", [False, True])
async def test_invalid_budget_creates_no_run(service: Any, background: bool) -> None:
    app, client, dispatched = service
    spec = WorkflowSpec(name="budget", entry_node="n", nodes={"n": WorkflowNode("n", "echo")})
    response = await client.post(
        "/api/v1/runs",
        json={
            "spec": spec.to_dict(),
            "budget_config": {"max_tokens": "not-a-number"},
            "async_execution": background,
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "INVALID_BUDGET_CONFIG"
    assert await app.state.store.list_runs(limit=None) == []
    assert app.state.workspace_repo.list_workspace_resource_ids("run", "default") == []
    assert dispatched == []


@pytest.mark.asyncio
async def test_rejection_claims_checkpoint_and_blocks_racing_approve(
    service: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, client, dispatched = service
    spec = WorkflowSpec(
        name="reject",
        entry_node="n",
        nodes={"n": WorkflowNode("n", "echo", requires_approval=True)},
    )
    response = await client.post("/api/v1/runs", json={"spec": spec.to_dict()})
    assert response.status_code == 201, response.text
    run = response.json()
    run_id = run["run_id"]
    approval_id = run["pending_approval"]["approval_id"]
    checkpoints = app.state.store.checkpoint_store
    claim = checkpoints.claim_approval
    approve_waiting = asyncio.Event()
    release_approve = asyncio.Event()

    async def blocked_claim(*args: Any, **kwargs: Any) -> bool:
        if not approve_waiting.is_set():
            approve_waiting.set()
            await release_approve.wait()
        return bool(await claim(*args, **kwargs))

    monkeypatch.setattr(checkpoints, "claim_approval", blocked_claim)
    approving = asyncio.create_task(
        client.post(
            f"/api/v1/runs/{run_id}/decide",
            json={"approval_id": approval_id, "decision": "approve"},
        )
    )
    try:
        await asyncio.wait_for(approve_waiting.wait(), timeout=5)
        rejected = await client.post(
            f"/api/v1/runs/{run_id}/decide", json={"approval_id": approval_id, "decision": "reject"}
        )
        assert rejected.status_code == 200, rejected.text
        assert rejected.json()["status"] == "rejected"
    finally:
        release_approve.set()
        approved = await asyncio.wait_for(approving, timeout=5)
    assert approved.status_code == 409, approved.text
    assert approved.json()["detail"]["code"] == "APPROVAL_ALREADY_CLAIMED"
    history = await client.get(f"/api/v1/runs/{run_id}/history")
    assert history.status_code == 200, history.text
    assert history.json()["items"][-1]["status"] == "rejected"
    checkpoint = await checkpoints.get(run_id)
    assert checkpoint.status == "rejected"
    assert checkpoint.pending_approval is None
    assert not await claim(run_id, approval_id)
    assert dispatched == []
