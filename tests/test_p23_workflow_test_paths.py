"""P23: a workflow test can also record the path (handler order) the saved run took."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from nexura_runtime.service.api.app import create_api_app

API = "/api/v1"
BASE = f"{API}/evaluations/studio"
TWO_STEP = {
    "name": "support",
    "entry_node": "lookup",
    "nodes": {
        "lookup": {
            "id": "lookup",
            "handler": "synthetic_order_lookup",
            "default_transition": "route",
        },
        "route": {"id": "route", "handler": "synthetic_router"},
    },
    "terminal_nodes": ["route"],
}


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_api_app(db_path=str(tmp_path / "p.sqlite3"), profile="local-owner"))


def _put_workflow(c: TestClient, spec: dict[str, Any]) -> None:
    r = c.post(f"{API}/workflows", json={"workflow_id": "wf", "spec": spec})
    assert r.status_code in (200, 201), r.text


def _completed_run(c: TestClient) -> str:
    r = c.post(f"{API}/runs", json={"workflow_id": "wf", "initial_state": {"order_id": "ORD-1"}})
    assert r.status_code == 201 and r.json()["status"] == "completed", r.text
    return str(r.json()["run_id"])


def _save(c: TestClient, run_id: str, **extra: Any) -> Any:
    body = {"run_id": run_id, "dataset_name": "T", "output_key": "route", **extra}
    return c.post(f"{BASE}/datasets/t/cases/from-run", json=body)


def _eval(c: TestClient) -> dict[str, Any]:
    target = {"kind": "workflow", "id": "wf", "output_key": "route"}
    r = c.post(f"{BASE}/runs", json={"dataset_id": "t", "target": target})
    assert r.status_code == 200, r.text
    out: dict[str, Any] = r.json()
    return out


def test_record_path_stores_handler_order_and_replay_passes(tmp_path: Path) -> None:
    c = _client(tmp_path)
    _put_workflow(c, TWO_STEP)
    res = _save(c, _completed_run(c), record_path=True)
    assert res.status_code == 200, res.text
    case = res.json()["case"]
    assert case["expected_trajectory"] == ["synthetic_order_lookup", "synthetic_router"]
    run = _eval(c)
    assert {s["type"] for s in run["scorers"]} >= {"exact_match", "tool_trajectory"}
    assert run["summary"]["passed"] == 1, run["summary"]


def test_without_record_path_no_trajectory_is_stored(tmp_path: Path) -> None:
    c = _client(tmp_path)
    _put_workflow(c, TWO_STEP)
    res = _save(c, _completed_run(c))
    assert res.status_code == 200, res.text
    assert res.json()["case"]["expected_trajectory"] is None


def test_changed_path_fails_the_recorded_test(tmp_path: Path) -> None:
    c = _client(tmp_path)
    _put_workflow(c, TWO_STEP)
    assert _save(c, _completed_run(c), record_path=True).status_code == 200
    # A new revision adds a step before the router; the output is unchanged, the path is not.
    spec = copy.deepcopy(TWO_STEP)
    spec["nodes"]["lookup"]["default_transition"] = "again"
    spec["nodes"]["again"] = {
        "id": "again",
        "handler": "synthetic_order_lookup",
        "default_transition": "route",
    }
    revision = c.get(f"{API}/workflows/wf").json()["revision"]
    r = c.put(f"{API}/workflows/wf", json={"spec": spec, "expected_revision": revision})
    assert r.status_code == 200, r.text
    run = _eval(c)
    assert run["summary"]["passed"] == 0, run["summary"]


def test_path_missing_from_checkpoints_is_refused(tmp_path: Path) -> None:
    c = _client(tmp_path)
    _put_workflow(c, TWO_STEP)
    run_id = _completed_run(c)
    store = c.app.state.store  # type: ignore[attr-defined]
    original = store.get_run

    async def inflated(rid: str) -> Any:
        found = await original(rid)
        if found is not None:
            found = {**found, "step_count": 7}
        return found

    store.get_run = inflated
    res = _save(c, run_id, record_path=True)
    assert res.status_code == 422
    assert res.json()["detail"]["code"] == "PATH_UNAVAILABLE"
    assert c.get(f"{BASE}/datasets/t").status_code == 404
