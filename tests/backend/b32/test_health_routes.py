"""Health routes must work over HTTP without an invented response query argument."""

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from nexura_studio.server import create_app

from nexura_runtime.release.health import HealthChecker, create_health_router


@pytest.mark.asyncio
async def test_readiness_http_route_needs_no_query_parameter(tmp_path: Path) -> None:
    app = FastAPI()
    app.include_router(create_health_router(HealthChecker(db_path=tmp_path / "health.db")))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        assert (await client.get("/healthz")).status_code == 200
        response = await client.get("/readyz")
        assert response.status_code == 200
        assert response.json()["status"] == "healthy"


@pytest.mark.asyncio
async def test_packaged_studio_mounts_readiness(tmp_path: Path) -> None:
    app = create_app(db_path=tmp_path / "studio.sqlite3", allowed_hosts={"testserver"})
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client,
    ):
        assert (await client.get("/healthz")).status_code == 200
        assert (await client.get("/readyz")).status_code == 200
