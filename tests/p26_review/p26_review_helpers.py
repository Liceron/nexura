"""Shared offline fixture for the P26 review regressions.

Promoted from .autopilot/p26/repros; local fakes only, no network.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from _local_env import local_headers
from fastapi.testclient import TestClient
from nexura_agents import FakeModel
from nexura_agents.contracts.base import BaseModule
from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.contracts.manifest import ModuleManifest
from nexura_agents.contracts.result import ModuleResult

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.workspaces.repository import WorkspaceRepository
from nexura_runtime.workflow import ModuleRegistry

API = "/api/v1"
WRITE_ID = "review.ticket.create"
WRITE_TOOL = "review_ticket_create"


class TicketModule(BaseModule):
    """Local recording write module (always_write)."""

    def __init__(self) -> None:
        self.created: list[str] = []
        self._manifest = ModuleManifest(
            module_id=WRITE_ID,
            version="1.0.0",
            kind="integration",
            title="Create ticket",
            description="Create a support ticket (writes).",
            input_schema={
                "type": "object",
                "properties": {"title": {"type": "string"}},
                "required": ["title"],
            },
            effect_policy="non_idempotent_write",
            always_write=True,
        )

    @property
    def manifest(self) -> ModuleManifest:
        return self._manifest

    async def invoke(
        self, context: ExecutionContext, inputs: dict[str, Any], config: dict[str, Any]
    ) -> ModuleResult:
        context.require_write_approval("Ticket creation")
        self.created.append(str(inputs["title"]))
        return ModuleResult.success({"ticket_id": f"T-{len(self.created)}"})


@pytest.fixture
def review_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Local-owner app in wsA/wsB; role headers are untrusted, models are local FakeModels."""
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
    ticket = TicketModule()
    db = tmp_path / "s.db"

    def make(
        chat_models: dict[str, Any], registry: ModuleRegistry | None = None, **app_kw: Any
    ) -> TestClient:
        reg = registry
        if reg is None:
            reg = ModuleRegistry()
            reg.register(WRITE_ID, ticket)
        app = create_api_app(
            registry=reg,
            db_path=db,
            workspace_repo=repo,
            profile="local-owner",
            chat_models=chat_models,
            **app_kw,
        )
        return TestClient(app)

    return {"make": make, "headers": headers, "ticket": ticket, "FakeModel": FakeModel}


def workflow(c: TestClient, h: dict[str, str], config: dict[str, Any]) -> str:
    spec = {
        "spec_version": 1,
        "name": "agentic",
        "entry_node": "ask",
        "nodes": {
            "ask": {
                "id": "ask",
                "handler": "nexura.agents.agent",
                "module_id": "nexura.agents.agent",
                "config": config,
            }
        },
        "terminal_nodes": ["ask"],
    }
    wf = c.post(f"{API}/workflows", headers=h, json={"spec": spec})
    assert wf.status_code == 201, wf.text
    return str(wf.json()["workflow_id"])


def run(
    c: TestClient, h: dict[str, str], workflow_id: str, state: dict[str, Any]
) -> dict[str, Any]:
    r = c.post(f"{API}/runs", headers=h, json={"workflow_id": workflow_id, "initial_state": state})
    assert r.status_code in (200, 201), r.text
    data: dict[str, Any] = c.get(f"{API}/runs/{r.json()['run_id']}", headers=h).json()
    return data


def decide(c: TestClient, h: dict[str, str], r: dict[str, Any], decision: str) -> dict[str, Any]:
    res = c.post(
        f"{API}/runs/{r['run_id']}/decide",
        headers=h,
        json={"approval_id": r["pending_approval"]["approval_id"], "decision": decision},
    )
    assert res.status_code == 200, res.text
    data: dict[str, Any] = c.get(f"{API}/runs/{r['run_id']}", headers=h).json()
    return data
