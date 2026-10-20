"""Python export/import endpoints (P5): parsed with ast, never executed."""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from httpx import ASGITransport

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.api.workflows import MAX_PYTHON_IMPORT_BYTES

SPEC = {
    "name": "api-wf",
    "entry_node": "a",
    "nodes": {
        "a": {
            "id": "a",
            "handler": "synthetic_router",
            "default_transition": "b",
            "retry": {"max_attempts": 2},
        },
        "b": {"id": "b", "handler": "synthetic_order_lookup"},
    },
    "terminal_nodes": ["b"],
}


@pytest.fixture
def client() -> Iterator[httpx.AsyncClient]:
    with tempfile.TemporaryDirectory(prefix="codegen_api_") as tmp:
        app = create_api_app(db_path=str(Path(tmp) / "c.sqlite3"), profile="local-owner")
        yield httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


async def _create(client: httpx.AsyncClient, spec: dict[str, Any] | None = None) -> str:
    resp = await client.post("/api/v1/workflows", json={"spec": spec or SPEC})
    assert resp.status_code == 201, resp.text
    return str(resp.json()["workflow_id"])


@pytest.mark.asyncio
async def test_export_then_import_round_trips(client: httpx.AsyncClient) -> None:
    wf_id = await _create(client)
    resp = await client.get(f"/api/v1/workflows/{wf_id}/export/python")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["filename"] == f"workflow_{wf_id}.py"
    assert "WorkflowNode(" in body["source"]
    assert "Retries: up to 2" in body["source"]

    imported = await client.post(
        "/api/v1/workflows/import/python", json={"source": body["source"], "name": "copy"}
    )
    assert imported.status_code == 201, imported.text
    record = imported.json()
    assert record["workflow_id"] != wf_id
    assert record["name"] == "copy"
    assert record["spec"]["nodes"]["a"]["retry"] == {"max_attempts": 2}
    assert record["spec"]["nodes"]["b"]["handler"] == "synthetic_order_lookup"


@pytest.mark.asyncio
async def test_import_accepts_raw_text_plain(client: httpx.AsyncClient) -> None:
    wf_id = await _create(client)
    source = (await client.get(f"/api/v1/workflows/{wf_id}/export/python")).json()["source"]
    resp = await client.post(
        "/api/v1/workflows/import/python",
        content=source.encode(),
        headers={"content-type": "text/plain"},
    )
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_export_missing_workflow_is_404(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/v1/workflows/nope/export/python")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "WORKFLOW_NOT_FOUND"


@pytest.mark.asyncio
async def test_unsafe_source_is_rejected_with_line_numbers_and_never_runs(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    marker = tmp_path / "pwned"
    before = (await client.get("/api/v1/workflows")).json()["total"]
    source = (
        "from nexura_runtime.workflow import WorkflowNode, WorkflowSpec\n"
        f"open({str(marker)!r}, 'w').write('x')\n"
    )
    resp = await client.post("/api/v1/workflows/import/python", json={"source": source})
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "INVALID_PYTHON_SOURCE"
    assert detail["errors"][0]["line"] == 2
    assert "line 2" in detail["errors"][0]["message"]
    assert not marker.exists()
    listing = await client.get("/api/v1/workflows")
    assert listing.json()["total"] == before


@pytest.mark.asyncio
async def test_syntax_error_and_bad_bodies(client: httpx.AsyncClient) -> None:
    resp = await client.post("/api/v1/workflows/import/python", json={"source": "spec = (\n"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["errors"][0]["line"] == 1
    for body in ({}, {"source": ""}, {"source": 5}, {"source": "x", "name": 1}):
        resp = await client.post("/api/v1/workflows/import/python", json=body)
        assert resp.status_code == 400, body
    resp = await client.post(
        "/api/v1/workflows/import/python",
        content=b"not json",
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_oversized_source_is_rejected(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/workflows/import/python",
        json={"source": "#" * (MAX_PYTHON_IMPORT_BYTES + 10)},
    )
    assert resp.status_code == 413
    assert resp.json()["detail"]["code"] == "SOURCE_TOO_LARGE"


@pytest.mark.asyncio
async def test_import_runs_the_same_graph_validation_as_json_import(
    client: httpx.AsyncClient,
) -> None:
    source = (
        "from nexura_runtime.workflow import WorkflowNode, WorkflowSpec\n"
        'spec = WorkflowSpec(name="x", entry_node="a", nodes={"a": WorkflowNode('
        'id="a", handler="no_such_handler")})\n'
    )
    resp = await client.post("/api/v1/workflows/import/python", json={"source": source})
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_workflow_id_conflict_is_409(client: httpx.AsyncClient) -> None:
    wf_id = await _create(client)
    source = (await client.get(f"/api/v1/workflows/{wf_id}/export/python")).json()["source"]
    resp = await client.post(
        "/api/v1/workflows/import/python", json={"source": source, "workflow_id": wf_id}
    )
    assert resp.status_code == 409
