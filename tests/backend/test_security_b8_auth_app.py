"""Second-campaign security regressions: auth/app group (JWT exp/sub, operator gating,
no-store caching, central JSON depth limit)."""

from __future__ import annotations

from pathlib import Path

import pytest
import test_security_b7_auth_app as T

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.workspaces.repository import WorkspaceRepository

SHARED = "authenticated-shared-service"


def _repo() -> WorkspaceRepository:
    repo = WorkspaceRepository()
    repo.create_workspace("w", "W", "u")
    return repo


@pytest.mark.asyncio
async def test_api_responses_are_no_store(tmp_path: Path) -> None:
    app = create_api_app(db_path=tmp_path / "local.db", workspace_repo=_repo())
    async with T._client(app) as c:
        r = await c.get(
            "/api/v1/workflows", headers={"X-Workspace-ID": "w", "X-Principal-Role": "viewer"}
        )
        assert r.headers["cache-control"] == "no-store"
        r = await c.get("/api/v1/workflows")
        assert r.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_deeply_nested_json_rejected_centrally(tmp_path: Path) -> None:
    app = create_api_app(db_path=tmp_path / "local.db", workspace_repo=_repo())
    h = {
        **{"X-Workspace-ID": "w", "X-Principal-Role": "viewer"},
        "content-type": "application/json",
    }
    async with T._client(app) as c:
        deep = "[" * 500 + "]" * 500
        r = await c.post("/api/v1/workflows", headers=h, content=f'{{"a":{deep}}}')
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "JSON_TOO_DEEP"
        # brackets inside strings (and escaped quotes) do not count
        tricky = '{"a":"' + "[" * 500 + '\\"' + "[" * 500 + '"}'
        r = await c.post("/api/v1/workflows", headers=h, content=tricky)
        assert "JSON_TOO_DEEP" not in r.text
        shallow = "[" * 30 + "]" * 30
        r = await c.post("/api/v1/workflows", headers=h, content=f'{{"a":{shallow}}}')
        assert "JSON_TOO_DEEP" not in r.text
