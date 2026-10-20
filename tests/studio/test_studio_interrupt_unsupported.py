"""The local Studio server cannot answer interrupt(); it must say so, not crash (B5)."""

from __future__ import annotations

from pathlib import Path

import httpx
from httpx import ASGITransport
from nexura_studio.server import create_app

from nexura_runtime.studio.builtin_modules import get_default_registry
from nexura_runtime.workflow import interrupt


async def test_run_that_asks_for_input_fails_with_a_pointer_to_nexura_serve(
    tmp_path: Path,
) -> None:
    registry = get_default_registry()
    registry.register("ask", lambda s: {"answer": interrupt({"question": "Refund?"})})
    app = create_app(registry=registry, db_path=tmp_path / "studio.sqlite3")
    spec = {
        "name": "asks",
        "entry_node": "a",
        "terminal_nodes": ["a"],
        "nodes": {"a": {"id": "a", "handler": "ask"}},
    }
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as client:
        res = await client.post("/api/v1/runs", json={"spec": spec, "initial_state": {}})
        assert res.status_code == 201, res.text  # was a 500 from an AssertionError
        run = res.json()
        assert run["status"] == "failed"
        assert "interrupt()" in run["error"] and "nexura-runtime serve" in run["error"]


async def test_question_after_an_approval_also_fails_cleanly(tmp_path: Path) -> None:
    registry = get_default_registry()
    registry.register("prep", lambda s: {"ready": True})
    registry.register("ask", lambda s: {"answer": interrupt({"question": "Refund?"})})
    app = create_app(registry=registry, db_path=tmp_path / "studio.sqlite3")
    spec = {
        "name": "gate-then-ask",
        "entry_node": "p",
        "terminal_nodes": ["a"],
        "nodes": {
            "p": {"id": "p", "handler": "prep", "default_transition": "a"},
            "a": {"id": "a", "handler": "ask", "requires_approval": True},
        },
    }
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as client:
        paused = (await client.post("/api/v1/runs", json={"spec": spec})).json()
        assert paused["status"] == "paused"
        approval_id = paused["pending_approval"]["approval_id"]
        res = await client.post(
            f"/api/v1/runs/{paused['run_id']}/decide",
            json={"approval_id": approval_id, "decision": "approve"},
        )
        assert res.status_code < 500, res.text
        run = (await client.get(f"/api/v1/runs/{paused['run_id']}")).json()
        assert run["status"] == "failed" and "nexura-runtime serve" in run["error"]
