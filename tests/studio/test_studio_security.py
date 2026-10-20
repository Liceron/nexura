"""Security tests for Nexura Studio.

Verifies:
1. Rejection of unauthorized/malicious Host headers (DNS rebinding prevention).
2. Rejection of cross-origin mutating browser requests (CSRF protection).
3. Enforcement of application/json Content-Type on mutating requests.
4. Error masking ensuring zero traceback or secret leaks.
5. CLI entrypoint missing-extra error formatting.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import httpx
import pytest
from httpx import ASGITransport
from nexura_studio.server import create_app


@pytest.fixture
def temp_db_path() -> Any:
    with tempfile.TemporaryDirectory(prefix="studio_sec_") as tmp_dir:
        yield Path(tmp_dir) / "sec_test.sqlite3"


@pytest.fixture
def app_and_client(temp_db_path: Path) -> Any:
    app = create_app(db_path=temp_db_path)
    transport = ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://testserver")
    yield app, client


@pytest.mark.asyncio
async def test_malicious_host_header_rejected(app_and_client: Any) -> None:
    _, client = app_and_client
    async with client:
        # Malicious Host header
        res = await client.get("/api/v1/modules", headers={"host": "attacker.com"})
        assert res.status_code == 400
        data = res.json()
        assert data["error"]["code"] == "INVALID_HOST"
        assert "not authorized" in data["error"]["message"]

        # Valid Host headers
        for valid_host in ("127.0.0.1", "localhost", "testserver"):
            ok_res = await client.get("/api/v1/modules", headers={"host": valid_host})
            assert ok_res.status_code == 200


@pytest.mark.asyncio
async def test_cross_origin_mutating_request_rejected(app_and_client: Any) -> None:
    _, client = app_and_client
    async with client:
        # Cross-origin POST request from remote domain
        res = await client.post(
            "/api/v1/workflows/validate",
            headers={"origin": "https://malicious-site.com"},
            json={"spec": {}},
        )
        assert res.status_code == 403
        data = res.json()
        assert data["error"]["code"] == "CROSS_ORIGIN_FORBIDDEN"
        assert "forbidden" in data["error"]["message"]

        # Same-origin POST request succeeds (or fails gracefully with validation error, not 403)
        ok_res = await client.post(
            "/api/v1/workflows/validate",
            headers={"origin": "http://testserver"},
            json={"spec": {}},
        )
        assert ok_res.status_code != 403


@pytest.mark.asyncio
async def test_content_type_enforcement(app_and_client: Any) -> None:
    _, client = app_and_client
    async with client:
        # Mutating request with non-JSON Content-Type
        res = await client.post(
            "/api/v1/workflows/validate",
            headers={"content-type": "text/plain"},
            content=b'{"spec": {}}',
        )
        assert res.status_code == 415
        data = res.json()
        assert data["error"]["code"] == "UNSUPPORTED_MEDIA_TYPE"


@pytest.mark.asyncio
async def test_api_errors_omit_tracebacks(app_and_client: Any) -> None:
    _, client = app_and_client
    async with client:
        # Trigger an unhandled error or validation error
        res = await client.get("/api/v1/runs/non-existent-id")
        assert res.status_code == 404
        data = res.json()
        assert "error" in data
        assert "traceback" not in str(data).lower()
        assert "file " not in str(data).lower()


def test_cli_missing_extra_message() -> None:
    """Verifies nexura-studio outputs a friendly message if studio dependencies are missing."""
    # Run a subprocess masking fastapi and uvicorn via PYTHONPATH trick or script check
    script = (
        "import sys\n"
        "sys.modules['fastapi'] = None\n"
        "sys.modules['uvicorn'] = None\n"
        "from nexura_studio.cli import main\n"
        "main()\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert "Nexura Studio requires the optional 'studio' extra dependencies" in proc.stderr
    assert "pip install" in proc.stderr
