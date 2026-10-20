"""Run traces: agent-internal span capture, redaction, bounds and the span-tree endpoint (P3)."""

from __future__ import annotations

import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from nexura_studio.server import create_app

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.studio import traces

SECRET = "sk-FAKE0123456789abcdef"


def _spec(**state: Any) -> dict[str, Any]:
    return {
        "name": "trace wf",
        "entry_node": "agent",
        "terminal_nodes": ["agent"],
        "nodes": {"agent": {"id": "agent", "handler": "synthetic_trace_agent"}},
    }


@pytest_asyncio.fixture
async def service_client() -> AsyncIterator[httpx.AsyncClient]:
    with tempfile.TemporaryDirectory(prefix="studio_trace_") as tmp:
        app = create_api_app(db_path=str(Path(tmp) / "t.sqlite3"), profile="local-owner")
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            yield client


async def _run(client: httpx.AsyncClient, state: dict[str, Any]) -> str:
    resp = await client.post("/api/v1/runs", json={"spec": _spec(), "initial_state": state})
    run_id = resp.json().get("run_id")
    assert run_id, resp.text
    return str(run_id)


@pytest.mark.asyncio
async def test_agent_spans_recorded_with_tokens_and_redaction(
    service_client: httpx.AsyncClient,
) -> None:
    run_id = await _run(service_client, {"topic": "refunds"})
    res = await service_client.get(f"/api/v1/runs/{run_id}/trace?include_content=true")
    assert res.status_code == 200, res.text
    body = res.json()
    kinds = [s["kind"] for s in body["spans"]]
    assert kinds[0] == "run"
    assert kinds.count("node") == 1
    assert kinds.count("model") == 2
    assert kinds.count("tool") == 1
    assert "agent" in kinds
    by_kind = {s["kind"]: s for s in body["spans"] if s["kind"] in {"tool", "node", "run"}}
    tool = by_kind["tool"]
    assert tool["name"] == "lookup_policy"
    assert tool["status"] == "ok"
    assert tool["input"]["api_key"] == "[REDACTED]"
    assert tool["input"]["topic"] == "refunds"
    assert SECRET not in res.text
    assert by_kind["node"]["total_tokens"] == 300  # two model calls of 150, rolled up
    assert by_kind["run"]["total_tokens"] == 300
    models = [s for s in body["spans"] if s["kind"] == "model"]
    assert all(m["parent_id"] and m["duration_ms"] is not None for m in models)
    # Tool spans nest under the agent span, which nests under the node span.
    ids = {s["span_id"]: s for s in body["spans"]}
    assert ids[tool["parent_id"]]["kind"] == "agent"
    assert ids[ids[tool["parent_id"]]["parent_id"]]["kind"] == "node"


@pytest.mark.asyncio
async def test_content_is_hidden_unless_requested(service_client: httpx.AsyncClient) -> None:
    run_id = await _run(service_client, {})
    body = (await service_client.get(f"/api/v1/runs/{run_id}/trace")).json()
    assert body["include_content"] is False
    assert all(s["input"] is None and s["output"] is None for s in body["spans"])
    assert all(s["has_content"] is False for s in body["spans"])


@pytest.mark.asyncio
async def test_failed_run_marks_error_spans_and_redacts_error(
    service_client: httpx.AsyncClient,
) -> None:
    resp = await service_client.post(
        "/api/v1/runs", json={"spec": _spec(), "initial_state": {"fail": True}}
    )
    run_id = (
        resp.json().get("run_id")
        or ((await service_client.get("/api/v1/runs?status=failed")).json()["items"][0]["run_id"])
    )
    res = await service_client.get(f"/api/v1/runs/{run_id}/trace?include_content=true")
    body = res.json()
    assert body["status"] == "failed"
    node = next(s for s in body["spans"] if s["kind"] == "node")
    assert node["status"] == "error"
    assert body["spans"][0]["status"] == "error"
    assert SECRET not in res.text
    # The agent finished before the node raised, so its tool and model spans stay ok.
    assert next(s for s in body["spans"] if s["kind"] == "tool")["status"] == "ok"


@pytest.mark.asyncio
async def test_unknown_run_is_404(service_client: httpx.AsyncClient) -> None:
    res = await service_client.get("/api/v1/runs/run-does-not-exist/trace")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_studio_server_also_serves_traces() -> None:
    with tempfile.TemporaryDirectory(prefix="studio_trace_srv_") as tmp:
        app = create_app(db_path=Path(tmp) / "s.sqlite3")
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            resp = await client.post(
                "/api/v1/runs", json={"spec": _spec(), "initial_state": {"topic": "x"}}
            )
            run_id = resp.json()["run_id"]
            body = (await client.get(f"/api/v1/runs/{run_id}/trace?include_content=true")).json()
            assert [s["kind"] for s in body["spans"]].count("tool") == 1
            assert SECRET not in str(body)


class _Ev:
    def __init__(self, kind: str, **data: Any) -> None:
        class _T:
            value = kind

        self.type = _T()
        self.timestamp = 1.0
        self.run_id = "r"
        self.data = data


class _Store:
    def __init__(self) -> None:
        self.rows: list[tuple[str, dict[str, Any]]] = []

    async def record_run_event(
        self, run_id: str, event_type: str, node_id: str | None = None, data: Any = None
    ) -> None:
        self.rows.append((event_type, data))


@pytest.mark.asyncio
async def test_capture_is_bounded_and_drops_noise() -> None:
    store = _Store()
    capture = traces.AgentEventCapture(store, "run", "node")
    for i in range(traces.MAX_AGENT_EVENTS_PER_NODE + 50):
        await capture.record(_Ev("tool_started", tool_call_id=str(i), tool_name="t", arguments={}))
    await capture.record(_Ev("text_delta", text="ignored"))
    await capture.record(_Ev("run_completed", messages=["big"], output="x"))
    kinds = [row[1]["kind"] for row in store.rows]
    assert kinds.count("tool_started") == traces.MAX_AGENT_EVENTS_PER_NODE
    assert kinds.count("truncated") == 1
    assert "text_delta" not in kinds
    assert "run_completed" not in kinds


@pytest.mark.asyncio
async def test_traced_run_outside_a_node_is_a_plain_run() -> None:
    from nexura_agents.agent import Agent
    from nexura_agents.models.fake import FakeModel
    from nexura_agents.runner import Runner

    agent = Agent(name="A", instructions="i", model=FakeModel(responses=["hi"]))
    result = await traces.traced_run(Runner(), agent, "hello")
    assert result.final_output == "hi"
