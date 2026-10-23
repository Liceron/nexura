"""Offline reproductions; real routes/graders, disposable review-owned test data."""
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from nexura_runtime.evaluation import (
    DatasetExample,
    ToolArgumentAccuracyGrader,
    recorded_tool_calls,
)
from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.auth.models import Principal
from nexura_runtime.service.extensions import Identity
from nexura_runtime.workflow import WorkflowNode, WorkflowSpec


def test_zero_tolerance_must_preserve_distinct_large_integers() -> None:
    expected = DatasetExample.model_validate({"input_data": {}, "expected_trajectory": [
        {"name": "pay", "args": {"account_id": 9007199254740992}}
    ]})
    trace = {"messages": [{"tool_calls": [
        {"id": "c", "name": "pay", "arguments": {"account_id": 9007199254740993}}
    ]}]}
    assert not ToolArgumentAccuracyGrader().score_trace(expected, trace).passed


def test_partial_message_capture_must_preserve_event_order() -> None:
    trace = {
        "run_id": "r",
        "messages": [{"tool_calls": [{"id": "second", "name": "ship", "arguments": {}}]}],
        "events": [
            {"run_id": "r", "type": "tool_started", "data": {
                "tool_call_id": "first", "tool_name": "charge", "arguments": {}}},
            {"run_id": "r", "type": "tool_started", "data": {
                "tool_call_id": "second", "tool_name": "ship", "arguments": {}}},
        ],
    }
    assert [step.name for step in recorded_tool_calls(trace)] == ["charge", "ship"]


async def test_import_trace_numeric_arguments_must_remain_valid_json(tmp_path: Path) -> None:
    app = create_api_app(db_path=str(tmp_path / "numeric.sqlite"))
    try:
        async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False),
                               base_url="http://127.0.0.1",
                               headers={"X-Nexura-Session": app.state.session_token}) as client:
            response = await client.post("/api/v1/evaluations/traces", json={
                "run_id": "card", "timestamp": 1, "messages": [{"tool_calls": [{
                    "id": "c", "name": "charge", "arguments": {"card": 4111111111111111}
                }]}],
            })
            assert response.status_code == 200, response.text
    finally:
        if getattr(app.state, "eval_store", None):
            app.state.eval_store.close()
        await app.state.store.close()


async def test_online_api_must_honor_existing_run_resource_restrictions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexura_runtime.secrets.keys import generate_master_key

    # A disposable key for this offline process, never a production credential.
    monkeypatch.setenv("NEXURA_MASTER_KEY", generate_master_key())
    class OfflineScopedIdentity:
        name = "review-offline-scoped-identity"
        is_authenticating = True

        def authenticate(self, request: object, target_workspace_id: str | None) -> Identity:
            return Identity(Principal(
                principal_id="scoped-caller", profile="authenticated-shared-service",
                scopes=["run"], resources=["workflow:allowed"],
            ), "workspace-a", "editor")

        def is_operator(self, request: object) -> bool:
            return False

    app = create_api_app(db_path=str(tmp_path / "scope.sqlite"),
                         profile="authenticated-shared-service",
                         auth_provider=OfflineScopedIdentity())
    spec = WorkflowSpec(name="private", entry_node="x",
                        nodes={"x": WorkflowNode(id="x", handler="synthetic_order_lookup")},
                        terminal_nodes=["x"])
    try:
        await app.state.store.record_run_start("private-run", "private-workflow", spec, {})
        await app.state.store.update_run("private-run", "completed", None,
                                         {"private_result": "confidential payload"}, 1)
        app.state.workspace_repo.record_resource_ownership(
            "run", "private-run", "workspace-a", "other-principal")
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://127.0.0.1") as client:
            denied = await client.get("/api/v1/runs/private-run")
            assert denied.status_code == 403, denied.text
            response = await client.post("/api/v1/evaluations/online", json={
                "example": {"input_data": {}, "expected_output": "unused"},
                "graders": [{"type": "exact_match"}],
            })
            assert response.status_code in (200, 403), response.text
            assert "confidential payload" not in response.text, response.text
    finally:
        if getattr(app.state, "eval_store", None):
            app.state.eval_store.close()
        await app.state.store.close()


async def test_imported_agent_identity_survives_overlay_of_the_same_studio_run(
    tmp_path: Path,
) -> None:
    app = create_api_app(db_path=str(tmp_path / "overlay.sqlite"))
    spec = WorkflowSpec(name="fixture", entry_node="x",
                        nodes={"x": WorkflowNode(id="x", handler="synthetic_order_lookup")},
                        terminal_nodes=["x"])
    try:
        await app.state.store.record_run_start("recorded", "wf", spec, {})
        await app.state.store.update_run("recorded", "completed", None, {"answer": "ok"}, 1)
        app.state.workspace_repo.record_resource_ownership(
            "run", "recorded", "default", "local-owner")
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://127.0.0.1",
                               headers={"X-Nexura-Session": app.state.session_token}) as client:
            imported = await client.post("/api/v1/evaluations/traces", json={
                "run_id": "recorded", "timestamp": 1, "workflow_id": "wf", "agent_id": "agent-a",
                "events": [{"type": "tool_started", "data": {
                    "tool_call_id": "x", "tool_name": "lookup", "arguments": {}}}],
            })
            assert imported.status_code == 200, imported.text
            response = await client.post("/api/v1/evaluations/online", json={
                "example": {"input_data": {}, "expected_trajectory": ["lookup"]},
                "config": {"agent_id": "agent-a"},
            })
            assert response.status_code == 200, response.text
            assert response.json()["total_examples"] == 1, response.text
            assert response.json()["pass_rate"] == 1, response.text
    finally:
        if getattr(app.state, "eval_store", None):
            app.state.eval_store.close()
        await app.state.store.close()
