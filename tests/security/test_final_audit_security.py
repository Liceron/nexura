"""Final audit S1–S3 regressions, using local ASGI transport and no providers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from nexura_agents.repro import export_repro_bundle
from starlette.applications import Starlette
from starlette.routing import Mount

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.api.middleware import AuthFailure, SecurityScopingMiddleware, route_path
from nexura_runtime.workflow import ModuleRegistry


@pytest.mark.parametrize(
    "message,secret",
    [
        ("headers {'X-Api-Key': 'zq81Lm2Pq7Wv0aaa'}", "zq81Lm2Pq7Wv0aaa"),
        ('config {"password": "hunter2-Zq81Lm9"}', "hunter2-Zq81Lm9"),
        ('{"api_key": "structured-secret", "nested": {"note": "visible"}}', "structured-secret"),
        ("https://example.test/?api_key=zq81Lm2Pq7Wv0ccc", "zq81Lm2Pq7Wv0ccc"),
        ("https://example.test/?key=AIza" + "a" * 35, "AIza" + "a" * 35),
        ("https://example.test/?key=arbitrary-value", "arbitrary-value"),
        ("https://example.test/?token=private-token&password=private-pass", "private-token"),
        ("https://example.test/?password=private-pass", "private-pass"),
    ],
)
def test_repro_redacts_every_stored_string(message: str, secret: str) -> None:
    recording = {
        "schema_version": 1,
        "run_kind": "agent",
        "inputs": message,
        "spec": {"name": "a", "tools": [], "instructions": message},
        "model_responses": [{"content": message}],
        "tool_fixtures": [
            {"name": "api", "arguments": {}, "output": message, "error": {"message": message}}
        ],
        "approvals": [],
        "budget_config": None,
        "operations": ["run"],
        "expected_outcome": {
            "status": "failed",
            "error_type": "UpstreamError",
            "error_message": message,
            "failing_step": "api",
        },
    }
    bundle = export_repro_bundle(recording)
    assert secret not in json.dumps(bundle.data)
    if message.startswith("{"):
        assert json.loads(bundle.data["inputs"])["nested"] == {"note": "visible"}
        assert json.loads(export_repro_bundle(bundle).data["inputs"])["nested"] == {
            "note": "visible"
        }


@pytest.mark.asyncio
async def test_approval_view_is_complete_and_redacts_values_only(tmp_path: Path) -> None:
    sent: list[dict[str, Any]] = []

    async def pay(state: dict[str, Any]) -> dict[str, Any]:
        sent.append(dict(state))
        return {"sent": True}

    registry = ModuleRegistry()
    registry.register("pay", pay)
    app = create_api_app(registry=registry, db_path=tmp_path / "review.sqlite3")
    secret = 'stored-credential-"with-escapes'
    app.state.credential_store.store("default", "stored", "api_key", secret, status="revoked")
    app.state.credential_store.store("foreign", "other", "api_key", "VISIBLE-FOREIGN-VALUE")
    nested: Any = {"password_policy": "allow-review"}
    for _ in range(20):
        nested = {"child": nested}
    state = {f"note_{i:03d}": "x" * 90 for i in range(101)}
    state.update(
        zz_pay_to="ATTACKER-IBAN-0001",
        api_key="VISIBLE-ACCOUNT-REFERENCE",
        password="VISIBLE-POLICY-NAME",
        prose='change {"password": "policy-name"}',
        long_text="y" * 9000 + "END-OF-REVIEW",
        items=list(range(120)),
        nested=nested,
        credential_copy=secret,
        detected="AIza" + "a" * 35,
        foreign="VISIBLE-FOREIGN-VALUE",
    )
    spec = {
        "name": "review",
        "entry_node": "pay",
        "nodes": {"pay": {"id": "pay", "handler": "pay", "requires_approval": True}},
    }
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 5000)),
            base_url="http://127.0.0.1",
            headers={"X-Nexura-Session": app.state.session_token},
        ) as client,
    ):
        start = await client.post("/api/v1/runs", json={"spec": spec, "initial_state": state})
        assert start.status_code == 201, start.text
        run_id = start.json()["run_id"]
        pending = (await client.get(f"/api/v1/runs/{run_id}")).json()["pending_approval"]
        view = pending["input_state"]
        assert view.keys() == state.keys()
        assert view["zz_pay_to"] == state["zz_pay_to"]
        assert view["api_key"] == state["api_key"]
        assert view["password"] == state["password"]
        assert view["prose"] == state["prose"]
        assert view["long_text"] == state["long_text"]
        assert view["items"] == state["items"]
        assert view["nested"] == state["nested"]
        assert view["foreign"] == state["foreign"]
        assert view["credential_copy"] == "[REDACTED]"
        assert view["detected"] == "[REDACTED]"
        canonical = json.loads(pending["canonical_payload_json"])
        assert canonical == view
        assert len(pending["canonical_payload_json"]) > 8000
        decision = await client.post(
            f"/api/v1/runs/{run_id}/decide",
            json={"approval_id": pending["approval_id"], "decision": "approve"},
        )
        assert decision.json()["status"] == "completed", decision.text
    assert sent == [state]


class DenyAll:
    name = "deny-all"
    is_authenticating = True
    routes_without_membership = ()

    def authenticate(self, request: Any, target_workspace_id: str | None) -> Any:
        raise AuthFailure(401, "UNAUTHENTICATED", "no credentials")

    def is_operator(self, request: Any) -> bool:
        return False


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["", "/svc", "/outer/svc"])
async def test_mounted_shared_service_authenticates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prefix: str
) -> None:
    monkeypatch.setenv("NEXURA_MASTER_KEY", "A" * 43)
    app = create_api_app(
        db_path=tmp_path / "shared.sqlite3",
        profile="authenticated-shared-service",
        auth_provider=DenyAll(),
    )
    target = Starlette(routes=[Mount(prefix, app=app)]) if prefix else app
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=target), base_url="http://127.0.0.1"
        ) as client,
    ):
        response = await client.post(
            prefix + "/api/v1/credentials",
            json={"credential_id": "planted", "credential_type": "api_key", "secret": "s3cr3t"},
        )
        assert response.status_code == 401
        assert (await client.get(prefix + "/api/v1/credentials")).status_code == 401


@pytest.mark.asyncio
async def test_shared_api_without_principal_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NEXURA_MASTER_KEY", "A" * 43)
    app = create_api_app(
        db_path=tmp_path / "closed.sqlite3",
        profile="authenticated-shared-service",
        auth_provider=DenyAll(),
    )
    # Simulate the authentication middleware being omitted by an embedder.
    app.user_middleware = [m for m in app.user_middleware if m.cls is not SecurityScopingMiddleware]
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client,
    ):
        response = await client.get("/api/v1/credentials")
        assert response.status_code == 401
        assert response.json()["detail"]["code"] == "UNAUTHENTICATED"


@pytest.mark.parametrize(
    "path,root,expected",
    [
        ("/svc/api/v1/runs", "/svc", "/api/v1/runs"),
        ("/svc2/api/v1/runs", "/svc", "/svc2/api/v1/runs"),
        ("/api/v1/runs", "/svc", "/api/v1/runs"),
        ("/svc", "/svc", ""),
    ],
)
def test_security_route_path(path: str, root: str, expected: str) -> None:
    assert route_path({"type": "http", "path": path, "root_path": root}) == expected


@pytest.mark.asyncio
async def test_mounted_local_security_checks(tmp_path: Path) -> None:
    app = create_api_app(db_path=tmp_path / "local.sqlite3")
    parent = Starlette(routes=[Mount("/svc", app=app)])
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=parent, client=("127.0.0.1", 5000)),
            base_url="http://127.0.0.1",
        ) as client,
    ):
        response = await client.post("/svc/api/v1/runs", json={})
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "SESSION_TOKEN_REQUIRED"
        assert (
            await client.get("/svc/api/v1/runs", headers={"Host": "evil.test"})
        ).status_code == 400
        # A signed webhook must reach its own verifier without the local token.
        webhook = await client.post("/svc/api/v1/triggers/webhooks/missing/deliver", json={})
        assert webhook.status_code == 401, webhook.text
        assert webhook.json()["detail"]["code"] == "MISSING_SIGNATURE_HEADERS"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=parent, client=("192.0.2.1", 5000)),
        base_url="http://127.0.0.1",
    ) as remote:
        refused = await remote.get("/svc/api/v1/runs")
        assert refused.status_code == 403
        assert refused.json()["detail"]["code"] == "NON_LOOPBACK_CLIENT"
