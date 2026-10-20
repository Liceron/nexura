"""Unit tests for Python NexuraClient and its subclients using httpx.MockTransport."""

from __future__ import annotations

import json
import urllib.parse
from typing import Any

import httpx
import pytest

from nexura_runtime.service.api.client import NexuraApiError, NexuraClient


def _make_client(handler: Any) -> NexuraClient:
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    return NexuraClient(
        base_url="http://mock-service",
        api_key="test-api-key",
        workspace_id="test-workspace",
        http_client=http_client,
    )


@pytest.mark.asyncio
async def test_workflows_client_coverage() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        if request.method == "GET" and path == "/api/v1/workflows/wf-123/export/download":
            return httpx.Response(200, content=b"PK\x03\x04fakezipcontent")
        return httpx.Response(200, json={"ok": True, "method": request.method, "path": path})

    client = _make_client(handler)

    await client.workflows.list(limit=10, offset=5, query="test")
    assert calls[-1].method == "GET"
    assert "/api/v1/workflows" in calls[-1].url.path
    assert calls[-1].url.params["limit"] == "10"
    assert calls[-1].url.params["query"] == "test"

    await client.workflows.create({"nodes": {}}, workflow_id="wf-1", name="WF 1")
    assert calls[-1].method == "POST"
    assert json.loads(calls[-1].content)["workflow_id"] == "wf-1"

    await client.workflows.get("wf-1")
    assert calls[-1].method == "GET"
    assert calls[-1].url.path == "/api/v1/workflows/wf-1"

    await client.workflows.update("wf-1", {"nodes": {}}, expected_revision=2)
    assert calls[-1].method == "PUT"
    assert json.loads(calls[-1].content)["expected_revision"] == 2

    await client.workflows.delete("wf-1")
    assert calls[-1].method == "DELETE"
    assert calls[-1].url.path == "/api/v1/workflows/wf-1"

    await client.workflows.export("wf-1", target_dir="/tmp/out")
    assert calls[-1].method == "POST"
    assert calls[-1].url.path == "/api/v1/workflows/wf-1/export"

    await client.workflows.validate({"nodes": {}})
    assert calls[-1].method == "POST"
    assert calls[-1].url.path == "/api/v1/workflows/validate"

    dl = await client.workflows.download_export("wf-123")
    assert dl == b"PK\x03\x04fakezipcontent"

    await client.workflows.export_python("wf-1")
    assert calls[-1].method == "GET"
    assert calls[-1].url.path == "/api/v1/workflows/wf-1/export/python"

    await client.workflows.import_python("def run(): pass", name="PyWF", workflow_id="py-wf")
    assert calls[-1].method == "POST"
    assert calls[-1].url.path == "/api/v1/workflows/import/python"

    await client.workflows.import_bundle(archive_base64="UEsDBA==", name="ZipWF")
    assert calls[-1].method == "POST"
    assert calls[-1].url.path == "/api/v1/workflows/import"


@pytest.mark.asyncio
async def test_runs_client_coverage() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200, json={"ok": True, "method": request.method, "path": request.url.path}
        )

    client = _make_client(handler)

    await client.runs.list(workflow_id="wf-1", status="completed", limit=15, offset=0)
    assert calls[-1].method == "GET"
    assert calls[-1].url.path == "/api/v1/runs"
    assert calls[-1].url.params["workflow_id"] == "wf-1"

    await client.runs.start(workflow_id="wf-1", initial_state={"x": 1}, async_execution=True)
    assert calls[-1].method == "POST"
    assert calls[-1].url.path == "/api/v1/runs"

    await client.runs.get("run-123")
    assert calls[-1].method == "GET"
    assert calls[-1].url.path == "/api/v1/runs/run-123"

    await client.runs.cancel("run-123", reason="user cancelled")
    assert calls[-1].method == "POST"
    assert calls[-1].url.path == "/api/v1/runs/run-123/cancel"

    await client.runs.decide("run-123", approval_id="app-1", decision="approve")
    assert calls[-1].method == "POST"
    assert calls[-1].url.path == "/api/v1/runs/run-123/decide"

    await client.runs.diagnostics("run-123", include_content=True)
    assert calls[-1].method == "GET"
    assert calls[-1].url.path == "/api/v1/runs/run-123/diagnostics"
    assert calls[-1].url.params["include_content"] == "true"

    await client.runs.trace("run-123", include_content=True)
    assert calls[-1].method == "GET"
    assert calls[-1].url.path == "/api/v1/runs/run-123/trace"

    await client.runs.provide_input("run-123", interrupt_id="int-1", value="user response")
    assert calls[-1].method == "POST"
    assert calls[-1].url.path == "/api/v1/runs/run-123/input"

    await client.runs.history("run-123")
    assert calls[-1].method == "GET"
    assert calls[-1].url.path == "/api/v1/runs/run-123/history"

    await client.runs.subruns("run-123", limit=25, offset=10)
    assert calls[-1].method == "GET"
    assert calls[-1].url.path == "/api/v1/runs/run-123/subruns"
    assert calls[-1].url.params["limit"] == "25"

    await client.runs.subrun("run-123", "step_0")
    assert calls[-1].method == "GET"
    assert calls[-1].url.path == "/api/v1/runs/run-123/subruns/step_0"

    await client.runs.fork("run-123", checkpoint_index=2, state_update={"y": 2})
    assert calls[-1].method == "POST"
    assert calls[-1].url.path == "/api/v1/runs/run-123/fork"


@pytest.mark.asyncio
async def test_workspaces_client_coverage() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200, json={"ok": True, "method": request.method, "path": request.url.path}
        )

    client = _make_client(handler)

    # Workspaces
    await client.workspaces.list(limit=10, offset=0)
    assert calls[-1].method == "GET"
    assert calls[-1].url.path == "/api/v1/workspaces"

    await client.workspaces.create("ws-test", "Test Workspace", description="Desc")
    assert calls[-1].method == "POST"
    assert calls[-1].url.path == "/api/v1/workspaces"

    await client.workspaces.get("ws-test")
    assert calls[-1].method == "GET"
    assert calls[-1].url.path == "/api/v1/workspaces/ws-test"

    await client.workspaces.update_policy(require_promotion_approval=True, workspace_id="ws-test")
    assert calls[-1].method == "PATCH"
    assert calls[-1].url.path == "/api/v1/workspaces/ws-test"
    assert json.loads(calls[-1].content)["require_promotion_approval"] is True


@pytest.mark.asyncio
async def test_sessions_and_knowledge_coverage() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200, json={"ok": True, "method": request.method, "path": request.url.path}
        )

    client = _make_client(handler)

    # Sessions
    await client.sessions.list(limit=20, offset=0)
    assert calls[-1].method == "GET"
    assert calls[-1].url.path == "/api/v1/sessions"

    await client.sessions.create(session_id="sess-1", title="Session 1")
    assert calls[-1].method == "POST"
    assert calls[-1].url.path == "/api/v1/sessions"

    await client.sessions.get("sess-1")
    assert calls[-1].method == "GET"
    assert calls[-1].url.path == "/api/v1/sessions/sess-1"

    await client.sessions.delete("sess-1")
    assert calls[-1].method == "DELETE"
    assert calls[-1].url.path == "/api/v1/sessions/sess-1"

    await client.sessions.models()
    assert calls[-1].method == "GET"
    assert calls[-1].url.path == "/api/v1/chat/models"

    await client.sessions.chat("sess-1", "Hello assistant", model="gpt-4o")
    assert calls[-1].method == "POST"
    assert calls[-1].url.path == "/api/v1/sessions/sess-1/chat"

    # Knowledge
    await client.knowledge.list_collections()
    assert calls[-1].method == "GET"
    assert calls[-1].url.path == "/api/v1/knowledge/collections"

    await client.knowledge.create_collection("col-1", "Collection 1")
    assert calls[-1].method == "POST"
    assert calls[-1].url.path == "/api/v1/knowledge/collections"

    await client.knowledge.limits()
    assert calls[-1].method == "GET"
    assert calls[-1].url.path == "/api/v1/knowledge/limits"

    await client.knowledge.list_documents(collection_id="col-1", limit=10, offset=0)
    assert calls[-1].method == "GET"
    assert calls[-1].url.path == "/api/v1/knowledge/documents"

    await client.knowledge.upload_document("doc.txt", "Sample text", collection_id="col-1")
    assert calls[-1].method == "POST"
    assert calls[-1].url.path == "/api/v1/knowledge/documents/upload"

    await client.knowledge.get_document("doc-123")
    assert calls[-1].method == "GET"
    assert calls[-1].url.path == "/api/v1/knowledge/documents/doc-123"

    await client.knowledge.get_chunk("doc-123", 4)
    assert calls[-1].method == "GET"
    assert calls[-1].url.path == "/api/v1/knowledge/documents/doc-123/chunks/4"

    await client.knowledge.delete_document("doc-123")
    assert calls[-1].method == "DELETE"
    assert calls[-1].url.path == "/api/v1/knowledge/documents/doc-123"

    await client.knowledge.retrieve("search query", collection_id="col-1", top_k=3)
    assert calls[-1].method == "POST"
    assert calls[-1].url.path == "/api/v1/knowledge/retrieve"

    await client.knowledge.answer("answer query", collection_id="col-1", language="en")
    assert calls[-1].method == "POST"
    assert calls[-1].url.path == "/api/v1/knowledge/answer"


@pytest.mark.asyncio
async def test_credentials_client_coverage() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200, json={"ok": True, "method": request.method, "path": request.url.path}
        )

    client = _make_client(handler)

    # Credentials
    await client.credentials.list(limit=10, offset=0)
    assert calls[-1].method == "GET"
    assert calls[-1].url.path == "/api/v1/credentials"

    await client.credentials.create("cred-1", "api_key", "secret123", provider="openai")
    assert calls[-1].method == "POST"
    assert calls[-1].url.path == "/api/v1/credentials"

    await client.credentials.get("cred-1")
    assert calls[-1].method == "GET"
    assert calls[-1].url.path == "/api/v1/credentials/cred-1"

    await client.credentials.verify("cred-1")
    assert calls[-1].method == "POST"
    assert calls[-1].url.path == "/api/v1/credentials/cred-1/verify"

    await client.credentials.revoke("cred-1")
    assert calls[-1].method == "POST"
    assert calls[-1].url.path == "/api/v1/credentials/cred-1/revoke"

    await client.credentials.delete("cred-1")
    assert calls[-1].method == "DELETE"
    assert calls[-1].url.path == "/api/v1/credentials/cred-1"


@pytest.mark.asyncio
async def test_evaluations_and_studio_coverage() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200, json={"ok": True, "method": request.method, "path": request.url.path}
        )

    client = _make_client(handler)

    # Core evaluations
    await client.evaluations.list_datasets()
    assert calls[-1].method == "GET"
    assert calls[-1].url.path == "/api/v1/evaluations/datasets"

    await client.evaluations.create_dataset("ds-1", "DS 1", [{"input": "hi"}])
    assert calls[-1].method == "POST"
    assert calls[-1].url.path == "/api/v1/evaluations/datasets"

    await client.evaluations.get_dataset("ds-1")
    assert calls[-1].method == "GET"
    assert calls[-1].url.path == "/api/v1/evaluations/datasets/ds-1"

    await client.evaluations.delete_dataset("ds-1")
    assert calls[-1].method == "DELETE"
    assert calls[-1].url.path == "/api/v1/evaluations/datasets/ds-1"

    await client.evaluations.run_evaluation("ds-1", target_workflow_id="wf-1")
    assert calls[-1].method == "POST"
    assert calls[-1].url.path == "/api/v1/evaluations/run"

    await client.evaluations.list_runs(limit=10, offset=0)
    assert calls[-1].method == "GET"
    assert calls[-1].url.path == "/api/v1/evaluations/runs"

    await client.evaluations.get_run("eval-1")
    assert calls[-1].method == "GET"
    assert calls[-1].url.path == "/api/v1/evaluations/runs/eval-1"

    await client.evaluations.compare_runs("eval-1", "eval-2")
    assert calls[-1].method == "POST"
    assert calls[-1].url.path == "/api/v1/evaluations/compare"

    # Studio evaluations
    await client.evaluations.studio.list_targets()
    assert calls[-1].method == "GET"
    assert calls[-1].url.path == "/api/v1/evaluations/studio/targets"

    await client.evaluations.studio.list_datasets(workflow_id="wf-1")
    assert calls[-1].method == "GET"
    assert calls[-1].url.path == "/api/v1/evaluations/studio/datasets"

    await client.evaluations.studio.put_dataset("sds-1", cases=[{"name": "c1"}], name="SDS 1")
    assert calls[-1].method == "PUT"
    assert calls[-1].url.path == "/api/v1/evaluations/studio/datasets/sds-1"

    await client.evaluations.studio.get_dataset("sds-1")
    assert calls[-1].method == "GET"
    assert calls[-1].url.path == "/api/v1/evaluations/studio/datasets/sds-1"

    await client.evaluations.studio.export_dataset("sds-1")
    assert calls[-1].method == "GET"
    assert calls[-1].url.path == "/api/v1/evaluations/studio/datasets/sds-1/export"

    await client.evaluations.studio.import_openai('{"input":"hi"}', dataset_id="sds-openai")
    assert calls[-1].method == "POST"
    assert calls[-1].url.path == "/api/v1/evaluations/studio/datasets/import-openai"

    await client.evaluations.studio.add_case_from_run("sds-1", "run-100", case_name="case_100")
    assert calls[-1].method == "POST"
    assert calls[-1].url.path == "/api/v1/evaluations/studio/datasets/sds-1/cases/from-run"

    await client.evaluations.studio.create_run(
        "sds-1", target={"kind": "workflow", "id": "wf-1"}, repeats=2
    )
    assert calls[-1].method == "POST"
    assert calls[-1].url.path == "/api/v1/evaluations/studio/runs"

    await client.evaluations.studio.list_runs(dataset_id="sds-1", limit=20)
    assert calls[-1].method == "GET"
    assert calls[-1].url.path == "/api/v1/evaluations/studio/runs"

    await client.evaluations.studio.get_run("srun-1")
    assert calls[-1].method == "GET"
    assert calls[-1].url.path == "/api/v1/evaluations/studio/runs/srun-1"

    await client.evaluations.studio.delete_run("srun-1")
    assert calls[-1].method == "DELETE"
    assert calls[-1].url.path == "/api/v1/evaluations/studio/runs/srun-1"

    await client.evaluations.studio.compare("srun-1", "srun-2", tolerance=0.05)
    assert calls[-1].method == "POST"
    assert calls[-1].url.path == "/api/v1/evaluations/studio/compare"

    await client.evaluations.studio.redteam(workflow_id="wf-1", write_tools=["db_write"])
    assert calls[-1].method == "POST"
    assert calls[-1].url.path == "/api/v1/evaluations/studio/redteam"


@pytest.mark.asyncio
async def test_triggers_and_agents_and_skills_coverage() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200, json={"ok": True, "method": request.method, "path": request.url.path}
        )

    client = _make_client(handler)

    # Triggers
    await client.triggers.list_schedules()
    assert calls[-1].method == "GET"
    assert calls[-1].url.path == "/api/v1/triggers/schedules"

    await client.triggers.create_schedule("wf-1", "0 * * * *", schedule_id="sch-1")
    assert calls[-1].method == "POST"
    assert calls[-1].url.path == "/api/v1/triggers/schedules"

    await client.triggers.pause_schedule("sch-1")
    assert calls[-1].method == "POST"
    assert calls[-1].url.path == "/api/v1/triggers/schedules/sch-1/pause"

    await client.triggers.resume_schedule("sch-1")
    assert calls[-1].method == "POST"
    assert calls[-1].url.path == "/api/v1/triggers/schedules/sch-1/resume"

    await client.triggers.delete_schedule("sch-1")
    assert calls[-1].method == "DELETE"
    assert calls[-1].url.path == "/api/v1/triggers/schedules/sch-1"

    await client.triggers.list_webhooks()
    assert calls[-1].method == "GET"
    assert calls[-1].url.path == "/api/v1/triggers/webhooks"

    await client.triggers.register_webhook("wf-1", endpoint_id="wh-1")
    assert calls[-1].method == "POST"
    assert calls[-1].url.path == "/api/v1/triggers/webhooks"

    await client.triggers.delete_webhook("wh-1")
    assert calls[-1].method == "DELETE"
    assert calls[-1].url.path == "/api/v1/triggers/webhooks/wh-1"

    await client.triggers.test_webhook("wh-1", payload={"test": True})
    assert calls[-1].method == "POST"
    assert calls[-1].url.path == "/api/v1/triggers/webhooks/wh-1/test"

    await client.triggers.list_jobs(limit=10)
    assert calls[-1].method == "GET"
    assert calls[-1].url.path == "/api/v1/triggers/jobs"

    await client.triggers.list_outbox(channel="email")
    assert calls[-1].method == "GET"
    assert calls[-1].url.path == "/api/v1/triggers/outbox"

    await client.triggers.test_dispatch_outbox("email", "to@example.com", "body content")
    assert calls[-1].method == "POST"
    assert calls[-1].url.path == "/api/v1/triggers/outbox/test-dispatch"

    # Agents
    await client.agents.list()
    assert calls[-1].method == "GET"
    assert calls[-1].url.path == "/api/v1/agents"

    await client.agents.options()
    assert calls[-1].method == "GET"
    assert calls[-1].url.path == "/api/v1/agents/options"

    await client.agents.get("ag-1")
    assert calls[-1].method == "GET"
    assert calls[-1].url.path == "/api/v1/agents/ag-1"

    await client.agents.create({"name": "Agent 1", "model": "gpt-4o"})
    assert calls[-1].method == "POST"
    assert calls[-1].url.path == "/api/v1/agents"

    await client.agents.update("ag-1", {"name": "Agent 1 updated"}, expected_revision=1)
    assert calls[-1].method == "PUT"
    assert calls[-1].url.path == "/api/v1/agents/ag-1"

    await client.agents.delete("ag-1", expected_revision=2)
    assert calls[-1].method == "DELETE"
    assert calls[-1].url.path == "/api/v1/agents/ag-1"

    # Skills
    await client.skills.list(label="prod")
    assert calls[-1].method == "GET"
    assert calls[-1].url.path == "/api/v1/skills"

    await client.skills.create("Skill markdown text", label="prod")
    assert calls[-1].method == "POST"
    assert calls[-1].url.path == "/api/v1/skills"

    await client.skills.get("skill-1", version=2)
    assert calls[-1].method == "GET"
    assert calls[-1].url.path == "/api/v1/skills/skill-1"

    await client.skills.versions("skill-1")
    assert calls[-1].method == "GET"
    assert calls[-1].url.path == "/api/v1/skills/skill-1/versions"

    await client.skills.set_label("skill-1", "production", version=2)
    assert calls[-1].method == "PUT"
    assert calls[-1].url.path == "/api/v1/skills/skill-1/labels/production"

    await client.skills.remove("skill-1", version=1)
    assert calls[-1].method == "DELETE"
    assert calls[-1].url.path == "/api/v1/skills/skill-1"


@pytest.mark.asyncio
async def test_templates_builder_client_coverage() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200, json={"ok": True, "method": request.method, "path": request.url.path}
        )

    client = _make_client(handler)

    # Templates
    await client.templates.list(category="support", tag="crm")
    assert calls[-1].method == "GET"
    assert calls[-1].url.path == "/api/v1/templates"

    await client.templates.get("tmpl-1")
    assert calls[-1].method == "GET"
    assert calls[-1].url.path == "/api/v1/templates/tmpl-1"

    await client.templates.instantiate("tmpl-1", name="Custom Support WF")
    assert calls[-1].method == "POST"
    assert calls[-1].url.path == "/api/v1/templates/tmpl-1/instantiate"

    # Builder
    await client.builder.draft("Create a ticket classifier workflow", model="gpt-4o")
    assert calls[-1].method == "POST"
    assert calls[-1].url.path == "/api/v1/builder/draft"


@pytest.mark.asyncio
async def test_path_encoding_with_special_characters() -> None:
    """Ensure path parameters with special characters are URL-quoted correctly."""
    captured_paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_paths.append(request.url.raw_path.decode("ascii"))
        return httpx.Response(200, json={"ok": True})

    client = _make_client(handler)

    special_id = "test/special:id?name=val#frag"
    quoted = urllib.parse.quote(special_id, safe="")

    await client.workflows.get(special_id)
    assert captured_paths[-1] == f"/api/v1/workflows/{quoted}"

    await client.runs.get(special_id)
    assert captured_paths[-1] == f"/api/v1/runs/{quoted}"

    await client.skills.get(special_id)
    assert captured_paths[-1] == f"/api/v1/skills/{quoted}"

    await client.templates.get(special_id)
    assert captured_paths[-1] == f"/api/v1/templates/{quoted}"


@pytest.mark.asyncio
async def test_api_error_handling() -> None:
    """Ensure HTTP error responses raise structured NexuraApiError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={"detail": {"code": "NOT_FOUND", "message": "Resource was not found"}},
        )

    client = _make_client(handler)
    with pytest.raises(NexuraApiError) as exc_info:
        await client.workflows.get("missing-id")

    assert exc_info.value.status_code == 404
    assert exc_info.value.code == "NOT_FOUND"
    assert "Resource was not found" in exc_info.value.message


def test_closed_edition_subclients_are_not_in_the_open_source_client() -> None:
    """PKG-03: members, hosted secrets, organisations and publishing clients live in the closed
    edition (``nexura_enterprise.clients.EnterpriseClient``); the open-source client has none."""
    import inspect

    from nexura_runtime.service.api import client as client_module

    client = NexuraClient(base_url="http://test")
    for name in ("members", "secrets", "organisations", "published"):
        assert not hasattr(client, name), name
    for name in (
        "WorkspaceMembersClient",
        "SecretsClient",
        "OrganisationsClient",
        "PublishedClient",
    ):
        assert not hasattr(client_module, name), name
    source = inspect.getsource(client_module)
    for segment in ("/organisations", "/published", "/secrets", "/members", "/my-membership"):
        assert segment not in source, segment

    # Subclasses (the closed edition's add-on) keep their class through with_workspace.
    class Sub(NexuraClient):
        pass

    assert type(Sub(base_url="http://test").with_workspace("w2")) is Sub
