"""P20b: workspace-scoped skills service, roles, refusals, labels and run-time attachment."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from _local_env import local_headers
from fastapi.testclient import TestClient
from nexura_agents import FakeModel
from nexura_agents.types import Message, ToolCall

from nexura_runtime.runtime.agents import (
    AgentCoordinator,
    AgentCoordinatorModule,
    AgentDefinition,
)
from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.workspaces.repository import WorkspaceRepository
from nexura_runtime.workflow import ModuleRegistry

REFUNDS_V1 = (
    "---\nname: refunds\ndescription: How to handle refund requests.\n---\n"
    "# Refunds\nRefund only within 30 days."
)
REFUNDS_V2 = (
    "---\nname: refunds\ndescription: How to handle refund requests.\n---\n"
    "# Refunds\nRefund within 60 days, manager approval above 500."
)


class Seen:
    """What the scripted model saw: system text per call and the skill text it loaded."""

    def __init__(self) -> None:
        self.system: list[str] = []
        self.loaded: list[str] = []
        self.tool_names: list[list[str]] = []


def _agent_module(seen: Seen, skill_name: str = "refunds") -> AgentCoordinatorModule:
    def handler(messages: list[Message], tools: list[dict[str, Any]] | None) -> Any:
        seen.system.append(next((m.content or "" for m in messages if m.role == "system"), ""))
        seen.tool_names.append([t.get("name") or t["function"]["name"] for t in tools or []])
        if messages[-1].role == "tool":
            seen.loaded.append(messages[-1].content or "")
            return "done"
        if "load_skill" in seen.tool_names[-1]:
            return [ToolCall(id="c1", name="load_skill", arguments={"skill": skill_name})]
        return "no skills attached"

    agent = AgentDefinition(name="support", model=FakeModel(handler=handler))
    return AgentCoordinatorModule(AgentCoordinator(agents=[agent]), module_id="nexura.agents.t")


def _spec(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "spec_version": 1,
        "name": "skilled",
        "entry_node": "ask",
        "nodes": {"ask": {"id": "ask", "handler": "agent", "transitions": {}, "config": config}},
        "terminal_nodes": ["ask"],
    }


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setenv("NEXURA_INSECURE_ALLOW_PLAINTEXT_SECRETS", "I_UNDERSTAND_THIS_IS_UNSAFE")
    repo = WorkspaceRepository()
    repo.create_workspace("wsA", "A", owner_id="local-owner")
    repo.create_workspace("wsB", "B", owner_id="local-owner")
    headers: dict[str, dict[str, str]] = {}
    for label, ws, role in (
        ("admin", "wsA", "admin"),
        ("editor", "wsA", "editor"),
        ("viewer", "wsA", "viewer"),
        ("other", "wsB", "admin"),
    ):
        headers[label] = local_headers(ws, role)
    seen = Seen()
    registry = ModuleRegistry()
    registry.register("agent", _agent_module(seen))
    db = tmp_path / "s.db"

    def make_app() -> Any:
        return create_api_app(
            registry=registry,
            db_path=db,
            workspace_repo=repo,
            profile="local-owner",
        )

    return {
        "c": TestClient(make_app()),
        "make_app": make_app,
        "headers": headers,
        "seen": seen,
        "repo": repo,
    }


def _create(env: dict[str, Any], who: str = "admin", **body: Any) -> Any:
    return env["c"].post("/api/v1/skills", headers=env["headers"][who], json=body)


def _run(env: dict[str, Any], config: dict[str, Any], who: str = "admin") -> dict[str, Any]:
    c, h = env["c"], env["headers"][who]
    wf = c.post("/api/v1/workflows", headers=h, json={"spec": _spec(config)})
    assert wf.status_code == 201, wf.text
    run = c.post(
        "/api/v1/runs",
        headers=h,
        json={"workflow_id": wf.json()["workflow_id"], "initial_state": {"prompt": "refund?"}},
    )
    assert run.status_code in (200, 201), run.text
    data: dict[str, Any] = c.get(f"/api/v1/runs/{run.json()['run_id']}", headers=h).json()
    return data


def test_crud_versions_and_label_filter(env: dict[str, Any]) -> None:
    c, h = env["c"], env["headers"]["admin"]
    assert c.get("/api/v1/skills", headers=h).json() == {"items": [], "total": 0}
    created = _create(env, markdown=REFUNDS_V1, resources={"policy.md": "30 days"})
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["version"] == 1 and body["labels"] == ["latest"]
    assert body["resources"] == {"policy.md": "30 days"}
    # Identical text is not a new version.
    assert (
        _create(env, markdown=REFUNDS_V1, resources={"policy.md": "30 days"}).json()["version"] == 1
    )
    assert _create(env, markdown=REFUNDS_V2).json()["version"] == 2
    listed = c.get("/api/v1/skills", headers=h).json()
    assert listed["total"] == 1
    assert listed["items"][0]["latest_version"] == 2
    assert listed["items"][0]["labels"] == {"latest": 2}
    assert c.get("/api/v1/skills?label=production", headers=h).json()["items"] == []
    set_ = c.put("/api/v1/skills/refunds/labels/production", headers=h, json={"version": 1})
    assert set_.status_code == 200, set_.text
    assert set_.json()["labels"] == {"production": 1, "latest": 2}
    prod = c.get("/api/v1/skills?label=production", headers=h).json()["items"]
    assert [i["name"] for i in prod] == ["refunds"]
    assert (
        "30 days"
        in c.get("/api/v1/skills/refunds?label=production", headers=h).json()["instructions"]
    )
    assert c.get("/api/v1/skills/refunds?version=2", headers=h).json()["version"] == 2
    assert c.get("/api/v1/skills/refunds", headers=h).json()["version"] == 2
    assert len(c.get("/api/v1/skills/refunds/versions", headers=h).json()["versions"]) == 2
    assert c.get("/api/v1/skills/refunds?version=9", headers=h).status_code == 404
    assert c.get("/api/v1/skills/refunds?label=nope", headers=h).status_code == 404
    assert c.get("/api/v1/skills/ghost", headers=h).status_code == 404
    # latest cannot be pinned to an old version; unknown version is 404.
    bad = c.put("/api/v1/skills/refunds/labels/latest", headers=h, json={"version": 1})
    assert bad.status_code == 400 and bad.json()["detail"]["code"] == "SKILL_INVALID"
    gone = c.put("/api/v1/skills/refunds/labels/x", headers=h, json={"version": 7})
    assert gone.status_code == 404
    # Remove one version, then the skill.
    assert c.delete("/api/v1/skills/refunds?version=2", headers=h).json()["removed"] == 1
    assert c.get("/api/v1/skills/refunds", headers=h).json()["version"] == 1
    assert c.delete("/api/v1/skills/refunds", headers=h).status_code == 200
    assert c.delete("/api/v1/skills/refunds", headers=h).status_code == 404
    assert c.get("/api/v1/skills", headers=h).json()["total"] == 0


def test_skills_survive_restart(env: dict[str, Any]) -> None:
    _create(env, markdown=REFUNDS_V1)
    _create(env, markdown=REFUNDS_V2)
    reborn = TestClient(env["make_app"]())
    got = reborn.get("/api/v1/skills/refunds/versions", headers=env["headers"]["admin"]).json()
    assert [v["version"] for v in got["versions"]] == [1, 2]


def test_workspace_isolation(env: dict[str, Any]) -> None:
    c, headers = env["c"], env["headers"]
    _create(env, markdown=REFUNDS_V1)
    assert c.get("/api/v1/skills", headers=headers["other"]).json()["total"] == 0
    assert c.get("/api/v1/skills/refunds", headers=headers["other"]).status_code == 404
    assert c.delete("/api/v1/skills/refunds", headers=headers["other"]).status_code == 404
    label = c.put(
        "/api/v1/skills/refunds/labels/production", headers=headers["other"], json={"version": 1}
    )
    assert label.status_code == 404
    # The same name in another workspace is a separate skill with its own versions.
    assert _create(env, "other", markdown=REFUNDS_V2).json()["version"] == 1
    assert (
        "30 days"
        in c.get("/api/v1/skills/refunds", headers=headers["admin"]).json()["instructions"]
    )
    # The sole local owner may switch project folders via X-Workspace-ID. Forged
    # principal/role headers do not change that identity or its admin permissions.
    switched = local_headers("wsA", "viewer")
    switched["X-Principal-ID"] = "workspace-b-user"
    selected = c.get("/api/v1/skills/refunds", headers=switched)
    assert selected.status_code == 200
    assert "30 days" in selected.json()["instructions"]
    promoted = c.put(
        "/api/v1/skills/refunds/labels/production", headers=switched, json={"version": 1}
    )
    assert promoted.status_code == 200, promoted.text
    other = c.get("/api/v1/skills/refunds", headers=headers["other"])
    assert "60 days" in other.json()["instructions"]
    other_versions = c.get("/api/v1/skills/refunds/versions", headers=headers["other"]).json()
    assert "production" not in other_versions["labels"]


@pytest.mark.parametrize(
    "body",
    [
        {"markdown": "---\nname: Bad Name\ndescription: x\n---\nbody"},
        {"markdown": "---\nname: a\ndescription: x\n---\n"},
        {"markdown": "---\nname: a\ndescription: >\n  folded\n---\nbody"},
        {"markdown": "---\nname: a\ndescription: " + "d" * 301 + "\n---\nbody"},
        {"markdown": "---\nname: a\ndescription: x\n---\nbody", "resources": {"run.sh": "echo"}},
        {"markdown": "---\nname: a\ndescription: x\n---\nbody", "resources": {"x.py": "print()"}},
        {
            "markdown": "---\nname: a\ndescription: x\n---\nbody",
            "resources": {"a.txt": "#!/bin/sh"},
        },
        {"markdown": "---\nname: a\ndescription: x\n---\nbody", "resources": {"../a.md": "t"}},
        {"markdown": "---\nname: a\ndescription: x\n---\nbody", "resources": {"a/b.md": "t"}},
        {"markdown": "---\nname: a\ndescription: x\n---\nbody", "resources": {"a.md": "x" * 70000}},
        {"markdown": "---\nname: a\ndescription: x\n---\n" + "y" * 66000},
        {"markdown": "---\nname: a\ndescription: x\n---\nbody", "label": "Bad Label"},
    ],
)
def test_refusals(env: dict[str, Any], body: dict[str, Any]) -> None:
    r = _create(env, **body)
    assert r.status_code in (400, 422), r.text
    assert env["c"].get("/api/v1/skills", headers=env["headers"]["admin"]).json()["total"] == 0


def test_too_many_resources_refused(env: dict[str, Any]) -> None:
    md = "---\nname: a\ndescription: x\n---\nbody"
    many = {f"r{i}.md": "t" for i in range(51)}
    assert _create(env, markdown=md, resources=many).status_code == 422


def test_skill_text_is_returned_verbatim_not_rendered(env: dict[str, Any]) -> None:
    md = "---\nname: xss\ndescription: <b>bold</b>\n---\n<script>alert(1)</script>"
    r = _create(env, markdown=md)
    assert r.status_code == 201
    assert r.headers["content-type"].startswith("application/json")
    assert r.json()["instructions"] == "<script>alert(1)</script>"


def test_run_time_attachment_and_label_promotion(env: dict[str, Any]) -> None:
    c, h, seen = env["c"], env["headers"]["admin"], env["seen"]
    _create(env, markdown=REFUNDS_V1)
    c.put("/api/v1/skills/refunds/labels/production", headers=h, json={"version": 1})
    _create(env, markdown=REFUNDS_V2)  # latest is v2; production still v1

    first = _run(env, {"skills": ["refunds"]})
    assert first["status"] == "completed", first
    assert "Available skills" in seen.system[0] and "- refunds: How to handle" in seen.system[0]
    assert "load_skill" in seen.tool_names[0]
    assert "Refund only within 30 days." in seen.loaded[-1]

    # Promote v2: the same saved workflow now serves the new text, with no edit to it.
    c.put("/api/v1/skills/refunds/labels/production", headers=h, json={"version": 2})
    second = _run(env, {"skills": ["refunds"], "skills_label": "production"})
    assert second["status"] == "completed", second
    assert "60 days" in seen.loaded[-1]

    # skills_label picks another label.
    third = _run(env, {"skills": ["refunds"], "skills_label": "latest"})
    assert third["status"] == "completed" and "60 days" in seen.loaded[-1]


def test_node_without_skills_is_unchanged(env: dict[str, Any]) -> None:
    seen = env["seen"]
    out = _run(env, {})
    assert out["status"] == "completed"
    assert "Available skills" not in seen.system[0]
    assert "load_skill" not in seen.tool_names[0]


def test_run_fails_closed_for_unknown_skill_label_or_other_workspace(env: dict[str, Any]) -> None:
    c, h = env["c"], env["headers"]["admin"]
    _create(env, markdown=REFUNDS_V1)  # no production label yet
    assert _run(env, {"skills": ["refunds"]})["status"] == "failed"
    assert _run(env, {"skills": ["ghost"], "skills_label": "latest"})["status"] == "failed"
    c.put("/api/v1/skills/refunds/labels/production", headers=h, json={"version": 1})
    # Workspace B cannot attach workspace A's skill.
    assert _run(env, {"skills": ["refunds"]}, who="other")["status"] == "failed"
    assert env["seen"].loaded == []
    # A malformed 'skills' value is refused when the workflow is saved.
    bad = c.post("/api/v1/workflows", headers=h, json={"spec": _spec({"skills": "refunds"})})
    assert bad.status_code == 422
