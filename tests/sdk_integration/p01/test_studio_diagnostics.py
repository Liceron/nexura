"""Real SDK module -> persisted Studio API diagnostics; local fake provider only."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from nexura_runtime.contracts.context import ExecutionContext
from nexura_runtime.providers.fake import StreamingFakeProvider
from nexura_runtime.providers.module import ModelProviderModule
from nexura_runtime.providers.types import GenerationRequest
from nexura_runtime.types import ModelResponse, Usage

from nexura_runtime.modules.registry import VersionedModuleRegistry
from nexura_runtime.observability.run_diagnostics import describe_run
from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.workflow import ModuleRegistry


def make_registry() -> ModuleRegistry:
    modules = VersionedModuleRegistry()
    modules.register(
        ModelProviderModule(
            StreamingFakeProvider(
                canned_responses=["नमस्ते — order ORD-501"],
                default_usage=Usage(
                    prompt_tokens=12, completion_tokens=4, total_tokens=16, is_complete=True
                ),
            )
        )
    )
    return ModuleRegistry(versioned_registry=modules)


def spec() -> dict[str, Any]:
    return {
        "name": "Local fake diagnostics fixture",
        "entry_node": "model_a",
        "nodes": {
            "model_a": {
                "id": "model_a",
                "handler": "nexura.models.provider",
                "module_id": "nexura.models.provider",
                "module_version": "1.0.0",
                "input_mapping": {"operation": "operation", "messages": "messages"},
            }
        },
        "terminal_nodes": ["model_a"],
    }


@pytest.mark.asyncio
async def test_failed_sdk_provider_is_terminal_and_inspectable(tmp_path: Path) -> None:
    class FailedLocalProvider(StreamingFakeProvider):
        async def _generate_internal(
            self,
            request: GenerationRequest,
            context: ExecutionContext | None,
        ) -> ModelResponse:
            raise RuntimeError("Local fake provider failed: password=sentinel-secret")

    modules = VersionedModuleRegistry()
    modules.register(ModelProviderModule(FailedLocalProvider()))
    app = create_api_app(
        registry=ModuleRegistry(versioned_registry=modules), db_path=tmp_path / "failure.db"
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as client,
    ):
        response = await client.post(
            "/api/v1/runs",
            json={
                "spec": spec(),
                "initial_state": {"operation": "generate"},
            },
        )
        assert response.status_code == 201
        snapshot = response.json()
        assert snapshot["status"] == "failed"
        assert "sentinel-secret" not in snapshot["error"]
        diagnostic = (await client.get(f"/api/v1/runs/{snapshot['run_id']}/diagnostics")).json()
        assert diagnostic["attempts"][0]["status"] == "failed"
        assert diagnostic["trace_export"]["status"] == "error"
        assert "sentinel-secret" not in json.dumps(diagnostic)


@pytest.mark.asyncio
async def test_sdk_usage_timing_redaction_trace_and_restart(tmp_path: Path) -> None:
    db = tmp_path / "studio.sqlite3"
    app = create_api_app(registry=make_registry(), db_path=db)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as client,
    ):
        response = await client.post(
            "/api/v1/runs",
            json={
                "spec": spec(),
                "initial_state": {
                    "operation": "generate",
                    "messages": [{"role": "user", "content": "नमस्ते", "api_key": "sentinel-secret"}],
                },
            },
        )
        assert response.status_code == 201, response.text
        run_id = response.json()["run_id"]
        assert response.json()["status"] == "completed"
        route = f"/api/v1/runs/{run_id}/diagnostics"
        diagnostic = await client.get(route)
        assert diagnostic.status_code == 200
        data = diagnostic.json()
        assert data["usage"]["known_input_tokens"] == 12
        assert data["usage"]["known_output_tokens"] == 4
        assert data["usage"]["cost"] is None
        assert data["usage"]["complete"]
        assert data["attempt_count"] == 1
        assert data["attempts"][0]["node_id"] == "model_a"
        assert data["attempts"][0]["duration_ms"] >= 0
        assert data["attempts"][0]["input"] is None
        trace = data["trace_export"]
        assert trace["total_tokens"] == 16
        assert all(span["inputs"] is None for span in trace["spans"])
        inspected = (await client.get(route, params={"include_content": True})).json()
        assert "sentinel-secret" not in json.dumps(inspected)
        assert "[REDACTED]" in json.dumps(inspected)
        assert "नमस्ते" in json.dumps(inspected, ensure_ascii=False)
        denied = await client.get(route, headers={"X-Workspace-ID": "foreign"})
        # Local-owner mode names the workspace; shared mode answers 404 instead (B7).
        assert denied.status_code == 403
        assert "model_a" not in denied.text
        missing = await client.get("/api/v1/runs/not-a-run/diagnostics")
        assert missing.status_code == 404
    restored = create_api_app(registry=make_registry(), db_path=db)
    async with (
        restored.router.lifespan_context(restored),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restored), base_url="http://local"
        ) as client,
    ):
        recovered = (await client.get(route)).json()
        assert recovered["trace_export"] == trace
        assert recovered["usage"]["known_input_tokens"] == 12


def event(seq: int, kind: str, stamp: float, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "seq": seq,
        "type": kind,
        "node_id": "node-a",
        "timestamp": stamp,
        "data": {
            "module_id": "nexura.models.provider",
            "attempt_id": f"attempt-{seq // 2}",
            "payload": payload,
        },
    }


def test_unknown_usage_is_not_inferred_from_arbitrary_outputs_and_active_cannot_export() -> None:
    events = [
        event(2, "MODULE_INVOKE_STARTED", 1, {"inputs": {}}),
        event(
            3,
            "MODULE_INVOKE_COMPLETED",
            2,
            {
                "outputs": {
                    "usage": {
                        "prompt_tokens": 999999,
                        "completion_tokens": 999999,
                    }
                }
            },
        ),
    ]
    run = {
        "run_id": "run-test",
        "status": "running",
        "created_at": 0,
        "updated_at": 3,
        "events": events,
    }
    data = describe_run(run)
    assert data["usage"]["known_input_tokens"] == 0
    assert not data["usage"]["complete"]
    assert data["usage"]["unreported_attempts"] == 1
    assert data["trace_export"] is None
    assert "finish" in data["trace_export_unavailable_reason"]


def test_repeated_node_attempts_remain_distinct_and_failure_usage_is_kept() -> None:
    rows = [
        event(2, "MODULE_INVOKE_STARTED", 1, {"inputs": {}}),
        event(
            3,
            "MODULE_INVOKE_FAILED",
            2,
            {
                "usage": {"prompt_tokens": 7, "completion_tokens": None},
                "error_message": "password=do-not-display",
            },
        ),
        event(4, "MODULE_INVOKE_STARTED", 3, {"inputs": {}}),
        event(
            5, "MODULE_INVOKE_COMPLETED", 4, {"usage": {"prompt_tokens": 2, "completion_tokens": 3}}
        ),
    ]
    data = describe_run(
        {
            "run_id": "run-test",
            "status": "completed",
            "created_at": 0,
            "updated_at": 5,
            "events": rows,
        },
        include_content=True,
    )
    assert data["attempt_count"] == 2
    assert data["attempts"][0]["id"] != data["attempts"][1]["id"]
    assert data["usage"]["known_input_tokens"] == 9
    assert not data["usage"]["complete"]
    assert "do-not-display" not in json.dumps(data)
    assert data["trace_export"]["status"] == "error"
