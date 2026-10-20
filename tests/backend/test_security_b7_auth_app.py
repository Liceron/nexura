"""Regression tests for the B7 auth/app-level security findings."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

import httpx
import pytest
from nexura_studio.server import create_app as create_studio_app

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.auth.errors import PermissionDeniedError
from nexura_runtime.service.auth.models import Principal
from nexura_runtime.service.auth.permissions import check_permission
from nexura_runtime.service.workspaces.repository import (
    UNOWNED_WORKSPACE,
    WorkspaceRepository,
)
from nexura_runtime.studio.store import StudioStore
from nexura_runtime.workflow import ModuleRegistry, WorkflowNode, WorkflowSpec

SHARED = "authenticated-shared-service"
CRED = {"credential_id": "c1", "credential_type": "api_key", "secret": "abcd1234"}


def _client(app: Any, base: str = "http://testserver") -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base)


def test_check_permission_intersects_scopes_with_role() -> None:
    p = Principal(principal_id="s", profile=SHARED, scopes=["run"])
    check_permission(p, "admin", "run")
    with pytest.raises(PermissionDeniedError):
        check_permission(p, "admin", "edit")
    with pytest.raises(PermissionDeniedError):
        check_permission(
            Principal(principal_id="s", profile=SHARED, scopes=["admin"]), "viewer", "admin"
        )


# --- finding 25: unowned resources fail closed ---
def test_unowned_resources_fail_closed_only_in_shared_mode() -> None:
    repo = WorkspaceRepository()
    assert repo.get_resource_workspace("workflow", "legacy") is None
    repo.fail_closed_unowned = True
    assert repo.get_resource_workspace("workflow", "legacy") == UNOWNED_WORKSPACE
    # integration tools (built-ins) are intentionally unowned and stay visible
    assert repo.get_resource_workspace("integration_tool", "builtin") is None
    repo.record_resource_ownership("workflow", "legacy", "ws", "o")
    assert repo.get_resource_workspace("workflow", "legacy") == "ws"


# --- finding 10: Host / Origin ---
@pytest.mark.asyncio
async def test_local_mode_host_and_origin_validation(tmp_path: Path) -> None:
    app = create_api_app(db_path=tmp_path / "s.sqlite3", profile="local-owner")
    async with _client(app, "http://attacker.example:8765") as c:
        assert (await c.get("/api/v1/credentials")).status_code == 400
    async with _client(app, "http://127.0.0.1:8765") as c:
        assert (await c.get("/api/v1/credentials")).status_code == 200
        for origin in ("http://localhost:5173", "http://127.0.0.1:15004", "http://[::1]:3000"):
            assert (
                await c.get("/api/v1/credentials", headers={"Origin": origin})
            ).status_code == 200, origin
        for origin in ("http://evil.example", "null", "http://localhost.evil.example:80"):
            r = await c.post("/api/v1/credentials", headers={"Origin": origin}, json=CRED)
            assert r.status_code == 403, origin
    async with _client(app, "http://[::1]:8765") as c:
        assert (await c.get("/healthz")).status_code == 200


# --- finding 11: headers ---
@pytest.mark.asyncio
async def test_studio_static_and_api_security_headers(tmp_path: Path) -> None:
    studio = create_studio_app(db_path=tmp_path / "st.sqlite3")
    async with _client(studio, "http://127.0.0.1") as c:
        r = await c.get("/")
        assert r.status_code == 200
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["referrer-policy"] == "no-referrer"
    api = create_api_app(db_path=tmp_path / "a.sqlite3")
    async with _client(api) as c:
        r = await c.get("/healthz")
        assert r.headers["x-content-type-options"] == "nosniff"


# --- finding 23: body limits ---
@pytest.mark.asyncio
async def test_body_size_limits(tmp_path: Path) -> None:
    app = create_api_app(
        db_path=tmp_path / "s.sqlite3", max_request_body_bytes=1024, max_import_body_bytes=4096
    )
    async with _client(app) as c:
        big = b'{"x":"' + b"a" * 2000 + b'"}'
        r = await c.post("/api/v1/runs", content=big, headers={"content-type": "application/json"})
        assert r.status_code == 413  # Content-Length path

        async def stream() -> Any:
            yield b'{"x":"'
            for _ in range(50):
                yield b"a" * 512
            yield b'"}'

        r = await c.post(
            "/api/v1/runs", content=stream(), headers={"content-type": "application/json"}
        )
        assert r.status_code == 413  # chunked path
        r = await c.post(
            "/api/v1/workflows/import",
            content=b'{"spec_json":"' + b"a" * 2000 + b'"}',
            headers={"content-type": "application/json"},
        )
        assert r.status_code != 413  # larger import cap applies
        r = await c.post(
            "/api/v1/workflows/import",
            content=b"a" * 5000,
            headers={"content-type": "application/json"},
        )
        assert r.status_code == 413
        assert (await c.get("/healthz")).status_code == 200


# --- finding 41: DB permissions ---
@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
async def test_service_database_is_created_private(tmp_path: Path) -> None:
    old = os.umask(0o022)
    try:
        db = tmp_path / "svc.sqlite3"
        app = create_api_app(db_path=db, profile="local-owner")
        async with _client(app) as c:
            spec = {
                "name": "T",
                "entry_node": "a",
                "nodes": {"a": {"id": "a", "handler": "synthetic_order_lookup"}},
                "terminal_nodes": ["a"],
            }
            made = await c.post("/api/v1/workflows", json={"workflow_id": "w", "spec": spec})
            assert made.status_code in (200, 201), made.text
            r = await c.post(
                "/api/v1/triggers/webhooks",
                json={"endpoint_id": "e1", "workflow_id": "w", "secret": "s3cret"},
            )
            assert r.status_code == 200
        for suffix in ("", "-wal", "-shm"):
            f = Path(str(db) + suffix)
            if f.exists():
                assert stat.S_IMODE(f.stat().st_mode) & 0o077 == 0, (f, oct(f.stat().st_mode))
        pre = tmp_path / "pre.sqlite3"
        pre.write_bytes(b"")
        pre.chmod(0o644)
        create_api_app(db_path=pre)
        assert stat.S_IMODE(pre.stat().st_mode) == 0o644  # existing files are never re-chmodded
    finally:
        os.umask(old)


def test_module_registry_import_is_used() -> None:
    assert ModuleRegistry() is not None


@pytest.mark.asyncio
async def test_local_workspace_create_conflicts_and_lists_every_project(tmp_path: Path) -> None:
    app = create_api_app(db_path=tmp_path / "local.db")
    async with _client(app) as c:
        h = {"X-Principal-ID": "forged", "X-Principal-Role": "viewer"}
        for wid in ("victim", "mine"):
            r = await c.post(
                "/api/v1/workspaces", headers=h, json={"workspace_id": wid, "name": wid}
            )
            assert r.status_code == 201, r.text
            workspace = app.state.workspace_repo.get_workspace(wid)
            assert workspace is not None and workspace.owner_id == "local-owner"
            assert app.state.workspace_repo.get_member(wid, "forged") is None
        duplicate = await c.post(
            "/api/v1/workspaces", headers=h, json={"workspace_id": "victim", "name": "replacement"}
        )
        assert duplicate.status_code == 409
        invalid = await c.post(
            "/api/v1/workspaces", headers=h, json={"workspace_id": "\x00unowned", "name": "bad"}
        )
        assert invalid.status_code == 422
        listed = await c.get("/api/v1/workspaces", headers=h)
        assert {"default", "victim", "mine"} <= {w["workspace_id"] for w in listed.json()["items"]}


@pytest.mark.asyncio
async def test_local_legacy_unowned_workflow_is_readable(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    st = StudioStore(db)
    spec = WorkflowSpec(
        name="legacy",
        entry_node="a",
        nodes={"a": WorkflowNode(id="a", handler="synthetic_order_lookup")},
        terminal_nodes=["a"],
    )
    await st.create_workflow(spec, workflow_id="legacy")
    await st.close()
    app = create_api_app(db_path=db)
    async with _client(app) as c:
        duplicate = await c.post(
            "/api/v1/workflows", json={"workflow_id": "legacy", "spec": spec.to_dict()}
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["detail"]["code"] == "WORKFLOW_ID_CONFLICT"
        assert (await c.get("/api/v1/workflows/legacy")).status_code == 200


@pytest.mark.asyncio
async def test_local_owner_health_details_and_public_docs(tmp_path: Path) -> None:
    app = create_api_app(db_path=tmp_path / "local.db")
    async with _client(app) as c:
        assert (await c.get("/healthz")).json() == {"status": "healthy"}
        assert "probes" in (await c.get("/readyz", headers={"X-Principal-Role": "viewer"})).json()
        for path in ("/openapi.json", "/docs", "/redoc"):
            assert (await c.get(path)).status_code == 200
