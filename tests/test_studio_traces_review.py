"""Regression tests for P3 run-trace review findings (redaction, retention, ownership)."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

import httpx
import pytest
from _local_env import local_headers
from httpx import ASGITransport

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.workspaces.repository import WorkspaceRepository
from nexura_runtime.studio.traces import build_trace
from nexura_runtime.workflow import WorkflowSpec

SECRET = "sk-FAKE0123456789abcdef"
SPEC = {
    "name": "t",
    "entry_node": "agent",
    "terminal_nodes": ["agent"],
    "nodes": {"agent": {"id": "agent", "handler": "synthetic_trace_agent"}},
}


def test_root_error_is_redacted_with_content() -> None:
    run = {
        "run_id": "r1",
        "status": "failed",
        "created_at": 1.0,
        "updated_at": 2.0,
        "error": f"boom api_key={SECRET} Authorization: Bearer abcdef123456",
        "events": [],
    }
    body = build_trace(run, include_content=True)
    assert SECRET not in str(body), body["spans"][0]["error"]
    assert "abcdef123456" not in str(body), body["spans"][0]["error"]


@pytest.mark.asyncio
async def test_retention_purge_is_honoured_like_diagnostics() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        app = create_api_app(db_path=str(Path(tmp) / "t.sqlite3"), profile="local-owner")
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/api/v1/runs", json={"spec": SPEC, "initial_state": {"topic": "x"}})
            rid = r.json()["run_id"]
            app.state.diagnostic_retention_seconds = 0.001
            await asyncio.sleep(0.05)
            diag = (await c.get(f"/api/v1/runs/{rid}/diagnostics?include_content=true")).text
            tr = (await c.get(f"/api/v1/runs/{rid}/trace?include_content=true")).text
            import json

            assert "PURGED" in diag
            spans = json.loads(tr)["spans"]
            assert all(s["input"] is None and s["output"] is None for s in spans), (
                "trace returned content after retention purge"
            )


def _tok(sub: str, ws: str) -> dict[str, str]:
    return local_headers(ws)


@pytest.mark.asyncio
async def test_cross_workspace_and_unowned_denied() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo = WorkspaceRepository()
        repo.create_workspace("wa", "A", "alice")
        repo.create_workspace("wb", "B", "bob")
        app = create_api_app(
            db_path=Path(tmp) / "s.sqlite3",
            workspace_repo=repo,
            profile="local-owner",
        )
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                "/api/v1/runs",
                json={"spec": SPEC, "initial_state": {}},
                headers=_tok("alice", "wa"),
            )
            assert r.status_code in (200, 201, 202), r.text
            rid = r.json()["run_id"]
            ok = await c.get(f"/api/v1/runs/{rid}/trace", headers=_tok("alice", "wa"))
            assert ok.status_code == 200, ok.text
            cross = await c.get(
                f"/api/v1/runs/{rid}/trace?include_content=true", headers=_tok("bob", "wb")
            )
            # Local owner: another workspace's run trace is denied and names the owner.
            assert cross.status_code == 403, cross.text
            assert cross.json()["detail"]["code"] == "CROSS_WORKSPACE_ACCESS_DENIED"
            assert "'wa'" in cross.json()["detail"]["message"]
            assert "spans" not in cross.json()
            # No workspace header: the local owner's "default" workspace, also denied.
            anon = await c.get(f"/api/v1/runs/{rid}/trace")
            assert anon.status_code == 403, anon.status_code
            assert anon.json()["detail"]["code"] == "CROSS_WORKSPACE_ACCESS_DENIED"
            assert "spans" not in anon.json()
            await app.state.store.record_run_start(
                run_id="unowned-legacy",
                workflow_id=None,
                spec=WorkflowSpec.from_dict(SPEC),
                initial_state={},
            )
            unowned = await c.get(
                "/api/v1/runs/unowned-legacy/trace?include_content=true",
                headers=local_headers("wb"),
            )
            assert unowned.status_code == 403
            assert unowned.json()["detail"]["code"] == "RUN_OWNERSHIP_UNKNOWN"


def _agent_run(created: float) -> dict[str, Any]:
    def ev(seq: int, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "seq": seq,
            "type": "AGENT_EVENT",
            "node_id": "agent",
            "timestamp": created + seq,
            "data": {"kind": kind, "ts": created + seq, "payload": payload},
        }

    return {
        "run_id": "r2",
        "status": "failed",
        "created_at": created,
        "updated_at": created + 10,
        "error": "x",
        "events": [
            {
                "seq": 1,
                "type": "NODE_STARTED",
                "node_id": "agent",
                "timestamp": created,
                "data": {"inputs": {"q": "hi"}},
            },
            ev(2, "agent_started", {"agent": f"ag api_key={SECRET}"}),
            ev(
                3,
                "tool_started",
                {
                    "tool_name": f"tool {SECRET}",
                    "tool_call_id": "c1",
                    "arguments": {"a": f"token={SECRET}"},
                },
            ),
            ev(4, "retry", {"reason": f"bad api_key={SECRET}", "tool_call_id": "c1"}),
            ev(5, "handoff", {"from": "a", "to": f"api_key={SECRET}", "reason": SECRET}),
            ev(
                6, "run_failed", {"error_type": f"E api_key={SECRET}", "error": f"api_key={SECRET}"}
            ),
        ],
    }


@pytest.mark.parametrize("content", [True, False])
def test_every_span_string_is_redacted(content: bool) -> None:
    body = build_trace(_agent_run(1.0), include_content=content)
    assert SECRET not in str(body)
    assert len(body["spans"]) > 3


def test_purged_run_suppresses_agent_event_content() -> None:
    body = build_trace(_agent_run(1.0), include_content=True, retention_seconds=1.0)
    for s in body["spans"]:
        assert s["input"] is None and s["output"] is None and s["error"] is None, s
        assert s["has_content"] is False
    assert SECRET not in str(body)


def test_fresh_run_within_retention_keeps_content() -> None:
    import time

    run = _agent_run(time.time())
    body = build_trace(run, include_content=True, retention_seconds=3600.0)
    assert any(s["input"] is not None for s in body["spans"] if s["kind"] == "tool")
