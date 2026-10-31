"""Template catalog: every template validates and runs offline; API and negative cases."""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

import httpx
import pytest
from _local_env import local_headers
from fastapi.testclient import TestClient
from httpx import ASGITransport

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.workspaces.repository import WorkspaceRepository
from nexura_runtime.templates import (
    TemplateError,
    get_template,
    list_templates,
    run_fixture,
    template_registry,
)
from nexura_runtime.templates import catalog as catalog_module
from nexura_runtime.workflow import WorkflowSpec

ALL = list_templates()
IDS = [t.id for t in ALL]
REQUIRED_TOPICS = {
    "support-desk-refund-approval",
    "rag-qa-citations",
    "research-team",
    "approval-workflow",
    "scheduled-report",
    "structured-extraction",
}


def test_catalog_covers_the_required_scenarios() -> None:
    assert set(IDS) >= REQUIRED_TOPICS
    assert len(set(IDS)) == len(IDS)


@pytest.mark.parametrize("template", ALL, ids=IDS)
def test_template_metadata_and_validation(template: Any) -> None:
    assert template.title and template.description and template.category and template.tags
    assert template.difficulty in {"beginner", "intermediate", "advanced"}
    spec = WorkflowSpec.from_dict(template.new_spec())
    spec.validate(template_registry())
    assert "flowchart" in spec.to_mermaid()
    # credentials are names only, never values
    assert all(isinstance(c, str) and " " not in c for c in template.required_credentials)
    assert template.fixture["runs"]


@pytest.mark.parametrize("template", ALL, ids=IDS)
async def test_template_runs_offline(template: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_network(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("template run opened a network connection")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    outcomes = await run_fixture(template)
    assert len(outcomes) == len(template.fixture["runs"])
    for outcome in outcomes:
        assert outcome.failures == [], f"{template.id}/{outcome.name}: {outcome.failures}"


def test_support_desk_and_approval_templates_have_gates() -> None:
    for template_id, node in (
        ("support-desk-refund-approval", "issue_refund"),
        ("approval-workflow", "execute"),
    ):
        template = get_template(template_id)
        assert template is not None
        assert template.spec["nodes"][node]["requires_approval"] is True


def test_scheduled_report_suggests_a_trigger() -> None:
    template = get_template("scheduled-report")
    assert template is not None and template.suggested_trigger is not None
    assert template.suggested_trigger["cron_expr"]


def test_filters() -> None:
    assert {t.id for t in list_templates(category="support")} == {
        "support-desk-refund-approval",
        "faq-chatbot",
        "ticket-triage",
        "sentiment-escalation",
    }
    assert "approval-workflow" in {t.id for t in list_templates(tag="approval")}
    assert list_templates(category="nope") == []


def test_broken_template_file_is_rejected(tmp_path: Path) -> None:
    good = (catalog_module.DATA_DIR / "approval-workflow.json").read_text(encoding="utf-8")
    broken = tmp_path / "approval-workflow.json"
    broken.write_text(good.replace('"tpl_request_notify"', '"no_such_handler"', 1))
    with pytest.raises(TemplateError):
        catalog_module._load_file(broken)
    wrong_id = tmp_path / "other.json"
    wrong_id.write_text(good)
    with pytest.raises(TemplateError, match="file name"):
        catalog_module._load_file(wrong_id)


# --- API ------------------------------------------------------------------------------------------


@pytest.fixture
def local_client(tmp_path: Path) -> httpx.AsyncClient:
    app = create_api_app(db_path=str(tmp_path / "t.sqlite3"), profile="local-owner")
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


@pytest.mark.asyncio
async def test_list_get_and_filter(local_client: httpx.AsyncClient) -> None:
    body = (await local_client.get("/api/v1/templates")).json()
    assert body["total"] == len(ALL) >= 6
    assert "support" in body["categories"] and "approval" in body["tags"]
    assert "spec" not in body["items"][0]
    only = (await local_client.get("/api/v1/templates", params={"category": "data"})).json()
    assert [t["id"] for t in only["items"]] == [
        "customer-feedback-analysis",
        "data-quality-check",
        "structured-extraction",
    ]
    tagged = (await local_client.get("/api/v1/templates", params={"tag": "rag"})).json()
    assert [t["id"] for t in tagged["items"]] == ["rag-qa-citations"]
    detail = (await local_client.get("/api/v1/templates/rag-qa-citations")).json()
    assert detail["spec"]["entry_node"] == "retrieve" and "flowchart" in detail["mermaid"]
    assert detail["sample_input"]["question"]


@pytest.mark.asyncio
async def test_unknown_template_is_404(local_client: httpx.AsyncClient) -> None:
    assert (await local_client.get("/api/v1/templates/nope")).status_code == 404
    resp = await local_client.post("/api/v1/templates/nope/instantiate", json={})
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "TEMPLATE_NOT_FOUND"


@pytest.mark.asyncio
@pytest.mark.parametrize("template_id", IDS)
async def test_instantiate_every_template_and_run_it(
    local_client: httpx.AsyncClient, template_id: str
) -> None:
    resp = await local_client.post(
        f"/api/v1/templates/{template_id}/instantiate", json={"name": "From template"}
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["runs_offline"] is True
    wf_id = body["workflow_id"]
    stored = (await local_client.get(f"/api/v1/workflows/{wf_id}")).json()
    assert stored["spec"]["name"] == "From template" or stored.get("name") == "From template"
    template = get_template(template_id)
    assert template is not None
    run = await local_client.post(
        "/api/v1/runs", json={"workflow_id": wf_id, "initial_state": template.sample_input}
    )
    assert run.status_code == 201, run.text
    body = run.json()
    if body["status"] == "pending_approval":
        decided = await local_client.post(
            f"/api/v1/runs/{body['run_id']}/decide",
            json={"approval_id": body["pending_approval"]["approval_id"], "decision": "approve"},
        )
        assert decided.status_code == 200, decided.text
        body = decided.json()
    assert body["status"] == "completed", body


@pytest.mark.asyncio
async def test_missing_credentials_are_reported_by_name_only(
    local_client: httpx.AsyncClient,
) -> None:
    first = (await local_client.post("/api/v1/templates/research-team/instantiate", json={})).json()
    assert first["missing_credentials"] == ["model_api_key", "search_api_key"]
    made = await local_client.post(
        "/api/v1/credentials",
        json={
            "credential_id": "model_api_key",
            "credential_type": "api_key",
            "secret": "sk-test-SECRET-VALUE",
        },
    )
    assert made.status_code == 201, made.text
    second = await local_client.post("/api/v1/templates/research-team/instantiate", json={})
    assert second.json()["missing_credentials"] == ["search_api_key"]
    assert "SECRET-VALUE" not in second.text


@pytest.mark.asyncio
async def test_workflow_id_conflict_is_409(local_client: httpx.AsyncClient) -> None:
    payload = {"workflow_id": "mine"}
    assert (
        await local_client.post("/api/v1/templates/approval-workflow/instantiate", json=payload)
    ).status_code == 201
    again = await local_client.post("/api/v1/templates/approval-workflow/instantiate", json=payload)
    assert again.status_code == 409


def _local(tmp_path: Path) -> tuple[TestClient, dict[str, dict[str, str]]]:
    repo = WorkspaceRepository()
    repo.create_workspace("wsA", "A", owner_id="local-owner")
    repo.create_workspace("wsB", "B", owner_id="local-owner")
    keys: dict[str, dict[str, str]] = {}
    for name, ws, _pid, role in [
        ("a", "wsA", "alice", "admin"),
        ("b", "wsB", "bob", "admin"),
        ("v", "wsA", "vic", "viewer"),
    ]:
        keys[name] = local_headers(ws, role)
    app = create_api_app(
        db_path=tmp_path / "s.db",
        workspace_repo=repo,
        profile="local-owner",
    )
    return TestClient(app), keys


def test_instantiated_workflow_stays_in_the_callers_workspace(tmp_path: Path) -> None:
    client, keys = _local(tmp_path)
    made = client.post(
        "/api/v1/templates/approval-workflow/instantiate",
        headers=keys["a"],
        json={"workflow_id": "wf-a"},
    )
    assert made.status_code == 201, made.text
    assert client.get("/api/v1/workflows/wf-a", headers=keys["a"]).status_code == 200
    # another workspace cannot open it (local owner: 403 naming the owner) or list it
    foreign = client.get("/api/v1/workflows/wf-a", headers=keys["b"])
    assert foreign.status_code == 403
    assert foreign.json()["detail"]["code"] == "CROSS_WORKSPACE_ACCESS_DENIED"
    assert "'wsA'" in foreign.json()["detail"]["message"]
    assert "spec" not in foreign.json()
    assert client.get("/api/v1/workflows", headers=keys["b"]).json()["total"] == 0
    # Local owner: workspace B reusing the id is a conflict (one person owns every local
    # workspace), and workspace A's copy is untouched
    other = client.post(
        "/api/v1/templates/approval-workflow/instantiate",
        headers=keys["b"],
        json={"workflow_id": "wf-a"},
    )
    assert other.status_code == 409
    assert client.get("/api/v1/workflows", headers=keys["a"]).json()["total"] == 1
