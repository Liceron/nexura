"""Workflow flags cannot weaken the operator-configured action policy."""

from pathlib import Path
from typing import Any

import httpx
import pytest

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.workflow import ModuleRegistry


@pytest.mark.asyncio
async def test_inline_workflow_cannot_remove_server_required_gate(tmp_path: Path) -> None:
    effects: list[bool] = []
    registry = ModuleRegistry()
    registry.register("protected_action", lambda state: effects.append(True))
    app = create_api_app(
        registry=registry,
        db_path=tmp_path / "policy.sqlite3",
        approval_required_handlers=frozenset({"protected_action"}),
    )
    spec: dict[str, Any] = {
        "name": "bypass",
        "entry_node": "effect",
        "nodes": {
            "effect": {"id": "effect", "handler": "protected_action", "requires_approval": False}
        },
    }
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client,
    ):
        denied = await client.post("/api/v1/runs", json={"spec": spec})
        assert denied.status_code == 422
        assert denied.json()["detail"]["code"] == "APPROVAL_POLICY_REQUIRED"
        assert effects == []
        spec["nodes"]["effect"]["requires_approval"] = True
        allowed = await client.post("/api/v1/runs", json={"spec": spec})
        assert allowed.status_code == 201
        assert allowed.json()["status"] == "pending_approval"
        assert effects == []
