"""P28 step 4: publishing is not part of the open-source edition.

Publishing (``/api/v1/published`` admin routes, the ``/api/v1/public/...`` chat gateway and
widget, the self-host launcher) moved to the closed edition (EDITIONS_PLAN 2026-10-18 step 4).
The open-source service must not serve any of it, and no open-source module may import it.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from httpx import ASGITransport

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.api.builtin_extensions import BUILTIN_EXTENSION_NAMES

ROOT = Path(__file__).resolve().parents[1]
PUB = "pub_0123456789abcdef"
SITE = "https://shop.example.com"

# Every route the publishing extension served, with a request that would have reached it.
PUBLISHING_REQUESTS: list[tuple[str, str, Any]] = [
    ("GET", "/api/v1/published", None),
    ("POST", "/api/v1/published", {"workflow_id": "w", "allowed_origins": [SITE]}),
    ("GET", f"/api/v1/published/{PUB}", None),
    ("PATCH", f"/api/v1/published/{PUB}", {"rate_limit_per_minute": 5}),
    ("DELETE", f"/api/v1/published/{PUB}", None),
    ("POST", f"/api/v1/published/{PUB}/rotate-key", None),
    ("POST", f"/api/v1/published/{PUB}/promote", {"credential_map": {}}),
    ("GET", f"/api/v1/published/{PUB}/promotions", None),
    ("POST", f"/api/v1/published/{PUB}/promotions/p1/approve", None),
    ("GET", f"/api/v1/published/{PUB}/map-changes", None),
    ("POST", f"/api/v1/published/{PUB}/map-changes/c1/reject", None),
    ("GET", f"/api/v1/published/{PUB}/audit", None),
    ("GET", "/api/v1/public/widget.js", None),
    ("POST", f"/api/v1/public/chat/{PUB}", {"message": "hi", "visitor_id": "visitor-abcdef12"}),
    ("GET", f"/api/v1/public/chat/{PUB}/runs/run_1", None),
    ("OPTIONS", f"/api/v1/public/chat/{PUB}", None),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "body"),
    PUBLISHING_REQUESTS,
    ids=[f"{m} {p}" for m, p, _ in PUBLISHING_REQUESTS],
)
async def test_oss_service_does_not_serve_publishing(
    tmp_path: Path, method: str, path: str, body: Any
) -> None:
    app = create_api_app(db_path=str(tmp_path / "oss.sqlite3"), profile="local-owner")
    headers = {"X-Nexura-Public-Key": "nxp_x", "X-Nexura-Visitor-Id": "visitor-abcdef12"}
    if method == "OPTIONS":
        headers |= {"Origin": SITE, "Access-Control-Request-Method": "POST"}
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        response = await client.request(method, path, headers=headers, json=body)
    # Unknown routes answer with the standard 404, never with gateway or widget content.
    assert "NexuraChat" not in response.text
    if method == "OPTIONS":
        # The gateway answered CORS preflights for allowed origins; OSS refuses the foreign
        # origin outright and never grants CORS on these paths.
        assert response.status_code in (403, 404, 405), (path, response.status_code)
        assert "access-control-allow-origin" not in response.headers
    elif method in ("GET", "POST"):
        assert response.status_code == 404, (method, path, response.status_code)
    else:
        assert response.status_code in (404, 405), (method, path, response.status_code)


def test_oss_app_has_no_publishing_state_or_gateway(tmp_path: Path) -> None:
    app = create_api_app(db_path=str(tmp_path / "oss.sqlite3"))
    assert not hasattr(app.state, "publication_store")
    assert not hasattr(app.state, "public_rate_limiter")
    assert "nexura.publishing" not in BUILTIN_EXTENSION_NAMES
    assert "nexura.publishing" not in app.state.extensions.configured
    middleware = [getattr(m.cls, "__name__", str(m.cls)) for m in app.user_middleware]
    assert "PublicGatewayMiddleware" not in middleware
    assert not any("public_app" in getattr(m, "kwargs", {}) for m in app.user_middleware)
    paths = {getattr(r, "path", "") for r in app.routes}
    assert not any(p.startswith(("/api/v1/published", "/api/v1/public")) for p in paths)
    assert "/published" not in app.openapi()["paths"]


@pytest.mark.parametrize(
    "module",
    [
        "nexura_runtime.publishing",
        "nexura_runtime.publishing.selfhost",
        "nexura_runtime.service.api.publishing",
    ],
)
def test_publishing_modules_are_not_in_the_oss_package(module: str) -> None:
    try:
        spec = importlib.util.find_spec(module)
    except ModuleNotFoundError:
        spec = None
    assert spec is None, module


_MOVED_IMPORT = re.compile(
    r"nexura_runtime\.publishing|nexura_runtime\.service\.api\.publishing"
    r"|nexura_runtime\.service\.api import publishing|features/publish/|widget/src/"
)


@pytest.mark.parametrize(
    ("base", "suffixes"),
    [
        ("src", (".py",)),
        ("packages", (".py",)),
        ("clients/typescript/src", (".ts", ".tsx", ".js", ".mjs")),
        ("studio-web/src", (".ts", ".tsx", ".js", ".jsx")),
    ],
)
def test_no_oss_source_references_a_moved_module(base: str, suffixes: tuple[str, ...]) -> None:
    hits = []
    for path in sorted((ROOT / base).rglob("*")):
        if path.suffix not in suffixes or not path.is_file():
            continue
        if {"node_modules", "dist", "__pycache__", ".venv"} & set(path.parts):
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _MOVED_IMPORT.search(line):
                hits.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
    assert not hits, hits


def test_moved_files_are_gone_from_the_oss_tree() -> None:
    moved = [
        "src/nexura_runtime/publishing",
        "src/nexura_runtime/service/api/publishing.py",
        "studio-web/src/features/publish",
        "studio-web/widget",
        "studio-web/vite.widget.config.ts",
        "studio-web/tests/e2e/publish.spec.ts",
        "studio-web/tests/unit/publish_widget.test.ts",
        "studio-web/tests/unit/publish_snippet.test.ts",
        "docs/publishing.md",
        "docs/self-hosting.md",
        "Dockerfile",
        "docker-compose.yml",
        ".env.example",
    ]
    present = [p for p in moved if (ROOT / p).exists()]
    assert not present, present
    # The single-user local Docker setup stays in the open-source edition.
    assert (ROOT / "deployment" / "Dockerfile").is_file()
    assert "nexura_runtime.publishing" not in (ROOT / "deployment" / "Dockerfile").read_text()
