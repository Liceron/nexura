"""GET /runs/{id}/subruns/{child_key} and RunResponse.has_subworkflows (P16)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_runs_interrupt_history_fork import (  # noqa: E402
    ASK_SPEC,
    CHILD_SPEC,
    PARENT_SPEC,
    Env,
    env,
)

from nexura_runtime.workflow import WorkflowNode, WorkflowSpec  # noqa: E402

__all__ = ["env"]

MAP_SPEC = WorkflowSpec(
    name="MapParent",
    entry_node="each",
    nodes={"each": WorkflowNode(id="each", handler="", subgraph=CHILD_SPEC, map_over="items")},
    terminal_nodes=["each"],
)


async def _children(env: Env, run_id: str, who: str = "alpha") -> list[dict[str, Any]]:
    res = await env.client.get(f"/api/v1/runs/{run_id}/subruns", headers=env.headers(who))
    assert res.status_code == 200, res.text
    items: list[dict[str, Any]] = res.json()["items"]
    return items


@pytest.mark.asyncio
async def test_has_subworkflows_true_and_false(env: Env) -> None:
    parent = await env.start(PARENT_SPEC)
    plain = await env.start(ASK_SPEC)
    got = (await env.client.get(f"/api/v1/runs/{parent['run_id']}", headers=env.headers())).json()
    assert got["has_subworkflows"] is True
    assert "spec" not in got
    got = (await env.client.get(f"/api/v1/runs/{plain['run_id']}", headers=env.headers())).json()
    assert got["has_subworkflows"] is False


@pytest.mark.asyncio
async def test_has_subworkflows_for_map_over_subgraph(env: Env) -> None:
    run = await env.start(MAP_SPEC, {"items": [1, 2]})
    got = (await env.client.get(f"/api/v1/runs/{run['run_id']}", headers=env.headers())).json()
    assert got["has_subworkflows"] is True


@pytest.mark.asyncio
async def test_child_view_returns_status_steps_and_history(env: Env) -> None:
    run = await env.start(PARENT_SPEC)
    rid = run["run_id"]
    children = await _children(env, rid)
    assert children
    key = children[0]["child_key"]
    assert key.replace("-", "").replace("_", "").isalnum()  # URL-safe, no '/' or '#'
    res = await env.client.get(f"/api/v1/runs/{rid}/subruns/{key}", headers=env.headers())
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["run_id"] == children[0]["workflow_run_id"]
    assert body["parent_run_id"] == rid
    assert body["status"] == children[0]["status"]
    assert body["steps"]
    history = body["history"]["items"]
    assert len(history) == len(body["steps"])
    assert all(h["workflow_run_id"] == body["run_id"] for h in history)


@pytest.mark.asyncio
async def test_unknown_child_key_and_unknown_run_are_404(env: Env) -> None:
    run = await env.start(PARENT_SPEC)
    for key in ("nope", "c3ViIzk5"):
        res = await env.client.get(
            f"/api/v1/runs/{run['run_id']}/subruns/{key}", headers=env.headers()
        )
        assert res.status_code == 404
        assert res.json()["detail"]["code"] == "RUN_NOT_FOUND"
    res = await env.client.get("/api/v1/runs/missing/subruns/abc", headers=env.headers())
    assert res.status_code == 404
    assert res.json()["detail"]["code"] == "RUN_NOT_FOUND"


@pytest.mark.asyncio
async def test_child_denied_to_other_workspace(env: Env) -> None:
    run = await env.start(PARENT_SPEC)
    rid = run["run_id"]
    key = (await _children(env, rid))[0]["child_key"]
    res = await env.client.get(f"/api/v1/runs/{rid}/subruns/{key}", headers=env.headers("beta"))
    # Local owner: the parent run's workspace is named, the child run is not returned.
    assert res.status_code == 403
    assert res.json()["detail"]["code"] == "CROSS_WORKSPACE_ACCESS_DENIED"
    assert "run_id" not in res.json() and "history" not in res.json()
    # A key from one run is not valid under another run of the same workspace.
    other = await env.start(PARENT_SPEC)
    other_children = await _children(env, other["run_id"])
    assert other_children[0]["child_key"] == key  # same suffix, different parent
    res = await env.client.get(
        f"/api/v1/runs/{other['run_id']}/subruns/{key}", headers=env.headers("beta")
    )
    assert res.status_code == 403
    assert res.json()["detail"]["code"] == "CROSS_WORKSPACE_ACCESS_DENIED"
    assert "run_id" not in res.json() and "history" not in res.json()


@pytest.mark.asyncio
async def test_subruns_paginated_and_naturally_sorted(env: Env) -> None:
    spec = WorkflowSpec(
        name="MapParent",
        entry_node="each",
        nodes={"each": WorkflowNode(id="each", handler="", subgraph=CHILD_SPEC, map_over="items")},
        terminal_nodes=["each"],
    )
    run = await env.start(spec, {"items": list(range(12))})
    url = f"/api/v1/runs/{run['run_id']}/subruns"
    first = (await env.client.get(f"{url}?limit=5", headers=env.headers("alpha"))).json()
    assert len(first["items"]) == 5
    assert first["has_more"] is True
    assert first["total"] == 12
    last = (await env.client.get(f"{url}?limit=5&offset=10", headers=env.headers("alpha"))).json()
    assert len(last["items"]) == 2
    assert last["has_more"] is False
    ids = [i["workflow_run_id"] for i in (await _children(env, run["run_id"]))]
    assert ids[1].endswith("[1]") and ids[2].endswith("[2]") and ids[10].endswith("[10]")
    bad = await env.client.get(f"{url}?limit=0", headers=env.headers("alpha"))
    assert bad.status_code == 422


@pytest.mark.asyncio
async def test_unknown_child_message_names_the_child(env: Env) -> None:
    run = await env.start(PARENT_SPEC)
    res = await env.client.get(f"/api/v1/runs/{run['run_id']}/subruns/nope", headers=env.headers())
    assert res.status_code == 404
    assert "nope" in res.json()["detail"]["message"]
    assert run["run_id"] not in res.json()["detail"]["message"]
