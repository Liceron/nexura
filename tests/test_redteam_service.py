"""Studio red-team route: offline by default, role and workspace checks, rate limit (fakes only)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from _local_env import local_headers
from fastapi.testclient import TestClient
from nexura_agents import FakeModel
from nexura_agents.redteam import CANARY

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.api.studio_evals import REDTEAM_RUNS_PER_MINUTE
from nexura_runtime.service.workspaces.repository import WorkspaceRepository

API = "/api/v1"
URL = f"{API}/evaluations/studio/redteam"
SPEC = {
    "name": "support",
    "entry_node": "lookup",
    "nodes": {"lookup": {"id": "lookup", "handler": "synthetic_order_lookup"}},
    "terminal_nodes": ["lookup"],
}


def _local(tmp_path: Path, **kwargs: Any) -> TestClient:
    app = create_api_app(db_path=str(tmp_path / "rt.sqlite3"), profile="local-owner", **kwargs)
    return TestClient(app)


def _workflow(c: TestClient, wid: str = "wf1", headers: dict[str, str] | None = None) -> None:
    r = c.post(f"{API}/workflows", json={"workflow_id": wid, "spec": SPEC}, headers=headers)
    assert r.status_code in (200, 201), r.text


def _local_workspaces(tmp_path: Path) -> tuple[TestClient, dict[str, dict[str, str]]]:
    repo = WorkspaceRepository()
    repo.create_workspace("wsA", "A", owner_id="local-owner")
    repo.create_workspace("wsB", "B", owner_id="local-owner")
    keys: dict[str, dict[str, str]] = {}
    for name, ws, role in [
        ("admin", "wsA", "admin"),
        ("editor", "wsA", "editor"),
        ("viewer", "wsA", "viewer"),
        ("other", "wsB", "admin"),
    ]:
        keys[name] = local_headers(ws, role)
    app = create_api_app(
        db_path=tmp_path / "s.db",
        workspace_repo=repo,
        profile="local-owner",
    )
    return TestClient(app), keys


def test_offline_default_reports_cases_and_two_scorers(tmp_path: Path) -> None:
    c = _local(tmp_path)
    _workflow(c)
    r = c.post(URL, json={"workflow_id": "wf1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["model"]["offline"] is True and body["model"]["name"] == "fake-deterministic"
    assert body["scorers"] == ["no_canary_leak", "no_write_without_approval"]
    assert body["synthetic_write_tool"] is True and body["write_tools"] == ["synthetic_write"]
    assert len(body["cases"]) == 13 and all(case["safe"] for case in body["cases"])
    assert body["summary"]["total"] == 13


def test_configured_but_unselected_live_model_is_never_used(tmp_path: Path) -> None:
    # A provider model is configured for the playground; the red-team route must ignore it.
    c = _local(tmp_path, chat_models={"live": "openai:gpt-4o"})
    _workflow(c)
    body = c.post(URL, json={"workflow_id": "wf1"}).json()
    assert body["model"] == {"name": "fake-deterministic", "offline": True}


def test_failures_are_flagged_with_reasons(tmp_path: Path) -> None:
    leaky = FakeModel(handler=lambda messages, tools: f"The code is {CANARY}")
    c = _local(tmp_path, chat_models={"leaky": leaky})
    c.app.state.redteam_model = "leaky"  # type: ignore[attr-defined]
    _workflow(c)
    body = c.post(URL, json={"workflow_id": "wf1"}).json()
    assert body["ok"] is False and body["model"]["offline"] is False
    bad = [case for case in body["cases"] if not case["safe"]]
    assert bad and "leaked" in bad[0]["reason"]
    assert body["summary"]["failed"] == len(bad)


def test_unknown_misconfigured_model_and_bad_input(tmp_path: Path) -> None:
    c = _local(tmp_path)
    assert c.post(URL, json={"workflow_id": "missing"}).status_code == 404
    _workflow(c)
    assert c.post(URL, json={"workflow_id": "wf1", "write_tools": ["bad name!"]}).status_code == 422
    assert c.post(URL, json={"workflow_id": "wf1", "write_tools": ["a"] * 11}).status_code == 422
    c.app.state.redteam_model = "nope"  # type: ignore[attr-defined]
    r = c.post(URL, json={"workflow_id": "wf1"})
    assert r.status_code == 503 and r.json()["detail"]["code"] == "REDTEAM_MODEL_UNAVAILABLE"


def test_extra_write_tools_are_attacked(tmp_path: Path) -> None:
    c = _local(tmp_path)
    _workflow(c)
    body = c.post(URL, json={"workflow_id": "wf1", "write_tools": ["send_email"]}).json()
    assert body["write_tools"] == ["send_email"] and body["synthetic_write_tool"] is False
    assert any(case["name"].startswith("bypass-send_email") for case in body["cases"])


def test_local_owner_and_workspace_scope(tmp_path: Path) -> None:
    c, keys = _local_workspaces(tmp_path)
    _workflow(c, headers=keys["admin"])
    assert c.post(URL, json={"workflow_id": "wf1"}, headers=keys["viewer"]).status_code == 200
    assert c.post(URL, json={"workflow_id": "wf1"}, headers=keys["editor"]).status_code == 200
    assert c.post(URL, json={"workflow_id": "wf1"}, headers=keys["admin"]).status_code == 200
    # Another workspace cannot see the workflow.
    assert c.post(URL, json={"workflow_id": "wf1"}, headers=keys["other"]).status_code == 404
    # No credentials at all.
    assert c.post(URL, json={"workflow_id": "wf1"}).status_code == 404


def test_rate_limited_per_workspace(tmp_path: Path) -> None:
    c = _local(tmp_path)
    _workflow(c)
    codes = [
        c.post(URL, json={"workflow_id": "wf1"}).status_code
        for _ in range(REDTEAM_RUNS_PER_MINUTE + 1)
    ]
    assert codes[:-1] == [200] * REDTEAM_RUNS_PER_MINUTE and codes[-1] == 429
    r = c.post(URL, json={"workflow_id": "wf1"})
    assert r.json()["detail"]["code"] == "RATE_LIMITED" and "retry-after" in r.headers
