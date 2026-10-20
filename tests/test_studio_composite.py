"""The service validate/save endpoints must understand composite steps (P2)."""

from __future__ import annotations

import copy
import tempfile
from pathlib import Path
from typing import Any

import httpx
import pytest
from httpx import ASGITransport

from nexura_runtime.service.api.app import create_api_app

LEAF = {"id": "leaf", "handler": "synthetic_router"}
SUB = {"name": "child", "entry_node": "leaf", "nodes": {"leaf": LEAF}}


def _spec(node: dict[str, Any]) -> dict[str, Any]:
    return {"name": "wf", "entry_node": "a", "nodes": {"a": {"id": "a", **node}}}


@pytest.fixture
def client() -> Any:
    with tempfile.TemporaryDirectory(prefix="studio_comp_") as tmp:
        app = create_api_app(db_path=str(Path(tmp) / "c.sqlite3"), profile="local-owner")
        transport = ASGITransport(app=app)
        yield httpx.AsyncClient(transport=transport, base_url="http://testserver")


async def _validate(client: Any, node: dict[str, Any]) -> dict[str, Any]:
    resp = await client.post("/api/v1/workflows/validate", json={"spec": _spec(node)})
    assert resp.status_code == 200, resp.text
    return resp.json()  # type: ignore[no-any-return]


@pytest.mark.asyncio
async def test_valid_composites_pass(client: Any) -> None:
    parallel = {"handler": "", "parallel": ["synthetic_router", "synthetic_order_lookup"]}
    mapped = {"handler": "synthetic_router", "map_over": "items", "map_as": "row"}
    sub = {"handler": "", "subgraph": copy.deepcopy(SUB)}
    mapped_sub = {**sub, "map_over": "items", "max_concurrency": 3}
    for node in (parallel, mapped, sub, mapped_sub):
        body = await _validate(client, node)
        assert body["valid"] is True, body


@pytest.mark.asyncio
async def test_invalid_composites_rejected_with_node_id(client: Any) -> None:
    cases = [
        {"handler": "synthetic_router", "map_over": "items", "map_as": "bad key"},
        {"handler": "synthetic_router", "map_over": ""},
        {"handler": "synthetic_router", "map_over": "items", "max_concurrency": 0},
        {"handler": "", "parallel": ["synthetic_router", "synthetic_router"]},
        {"handler": "", "parallel": ["no_such_handler"]},
        {"handler": "", "subgraph": copy.deepcopy(SUB), "parallel": ["synthetic_router"]},
        {"handler": "synthetic_router", "reducers": {"x": "append"}},
        {"handler": "", "subgraph": {"name": "c", "entry_node": "z", "nodes": {"z": {"id": "z"}}}},
    ]
    for node in cases:
        body = await _validate(client, node)
        assert body["valid"] is False, node
        assert any(e["node_id"] == "a" for e in body["errors"]), body


@pytest.mark.asyncio
async def test_composite_saves_and_round_trips(client: Any) -> None:
    node = {"handler": "", "parallel": ["synthetic_router", "synthetic_order_lookup"]}
    created = await client.post("/api/v1/workflows", json={"spec": _spec(node), "name": "p"})
    assert created.status_code == 201, created.text
    wf_id = created.json()["workflow_id"]
    got = (await client.get(f"/api/v1/workflows/{wf_id}")).json()
    assert got["spec"]["nodes"]["a"]["parallel"] == node["parallel"]
