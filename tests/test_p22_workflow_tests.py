"""P22 workflow tests: save a completed workflow run as an eval case, bound datasets, filters.

Offline only: workflows use the synthetic handlers of the default registry.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from nexura_runtime.evaluation import studio_engine
from nexura_runtime.service.api import studio_evals
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
ONE_STEP = {
    "name": "support",
    "entry_node": "lookup",
    "nodes": {"lookup": {"id": "lookup", "handler": "synthetic_order_lookup"}},
    "terminal_nodes": ["lookup"],
}
SECRET = "sk-abcdef1234567890abcdef"


def _local(tmp_path: Path) -> tuple[Any, TestClient]:
    app = create_api_app(db_path=str(tmp_path / "p22.sqlite3"), profile="local-owner")
    return app, TestClient(app)


def _ws(workspace: str) -> dict[str, str]:
    return {"X-Workspace-ID": workspace}


def _workflow(
    c: TestClient, wid: str, spec: dict[str, Any], headers: dict[str, str] | None = None
) -> None:
    r = c.post(f"{API}/workflows", json={"workflow_id": wid, "spec": spec}, headers=headers)
    assert r.status_code in (200, 201), r.text


def _run(
    c: TestClient,
    wid: str,
    state: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    r = c.post(f"{API}/runs", json={"workflow_id": wid, "initial_state": state}, headers=headers)
    assert r.status_code == 201, r.text
    out: dict[str, Any] = r.json()
    return out


def _save(
    c: TestClient, dataset_id: str, headers: dict[str, str] | None = None, **body: Any
) -> Any:
    return c.post(f"{BASE}/datasets/{dataset_id}/cases/from-run", json=body, headers=headers)


def _code(res: Any) -> str:
    return str(res.json()["detail"]["code"])


def test_save_run_as_test_then_run_and_compare(tmp_path: Path) -> None:
    _, c = _local(tmp_path)
    _workflow(c, "wf", TWO_STEP)
    delhi = _run(c, "wf", {"order_id": "ORD-DELHI-501"})
    mumbai = _run(c, "wf", {"order_id": "ORD-MUMBAI-202"})
    assert delhi["status"] == "completed"

    first = _save(
        c, "wf-tests", run_id=delhi["run_id"], dataset_name="WF tests", output_key="route"
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["created"] is True and body["revision"] == 1 and body["workflow_id"] == "wf"
    assert body["redactions"] == 0
    case = body["case"]
    assert case["name"] == delhi["run_id"]
    assert case["input"] == {"order_id": "ORD-DELHI-501"}
    assert case["expected_output"] == delhi["current_state"]["route"]
    assert case["metadata"] == {
        "source_run_id": delhi["run_id"],
        "source_workflow_id": "wf",
        "source_spec_hash": delhi["spec_hash"],
        "output_field": "route",
    }

    second = _save(
        c,
        "wf-tests",
        run_id=mumbai["run_id"],
        case_name="mumbai",
        output_key="route",
        expected_revision=1,
    )
    assert second.status_code == 200, second.text
    assert second.json()["created"] is False and second.json()["revision"] == 2
    assert [x["name"] for x in second.json()["cases"]] == [delhi["run_id"], "mumbai"]

    listed = c.get(f"{BASE}/datasets", params={"workflow_id": "wf"}).json()["datasets"]
    assert [(d["dataset_id"], d["case_count"]) for d in listed] == [("wf-tests", 2)]
    assert c.get(f"{BASE}/datasets", params={"workflow_id": "other"}).json()["datasets"] == []
    exported = c.get(f"{BASE}/datasets/wf-tests/export").json()
    assert exported["workflow_id"] == "wf"

    target = {"kind": "workflow", "id": "wf", "output_key": "route"}
    base = c.post(f"{BASE}/runs", json={"dataset_id": "wf-tests", "target": target})
    assert base.status_code == 200, base.text
    assert base.json()["summary"]["passed"] == 2
    # Change the workflow: the router is gone, so both cases regress and the gate fails.
    wf = c.get(f"{API}/workflows/wf").json()
    upd = c.put(
        f"{API}/workflows/wf",
        json={"spec": ONE_STEP, "expected_revision": wf["revision"]},
    )
    assert upd.status_code == 200, upd.text
    cand = c.post(f"{BASE}/runs", json={"dataset_id": "wf-tests", "target": target})
    assert cand.json()["summary"]["passed"] == 0
    cmp = c.post(
        f"{BASE}/compare",
        json={"baseline_run_id": base.json()["run_id"], "candidate_run_id": cand.json()["run_id"]},
    ).json()
    assert cmp["gate"]["passed"] is False and len(cmp["regressions"]) == 2


def test_full_final_state_is_the_default_expectation(tmp_path: Path) -> None:
    _, c = _local(tmp_path)
    _workflow(c, "wf", TWO_STEP)
    run = _run(c, "wf", {"order_id": "ORD-DELHI-501"})
    res = _save(c, "full", run_id=run["run_id"], dataset_name="Full state")
    assert res.status_code == 200, res.text
    assert res.json()["case"]["expected_output"] == run["current_state"]
    assert res.json()["case"]["metadata"]["output_field"] is None
    ev = c.post(
        f"{BASE}/runs", json={"dataset_id": "full", "target": {"kind": "workflow", "id": "wf"}}
    )
    assert ev.json()["summary"]["passed"] == 1, ev.text


def test_local_owner_other_workspace_run_is_404_not_403(tmp_path: Path) -> None:
    _, c = _local(tmp_path)
    _workflow(c, "wf-a", TWO_STEP, headers=_ws("ws-a"))
    run = _run(c, "wf-a", {"order_id": "ORD-DELHI-501"}, headers=_ws("ws-a"))
    # GET /runs/{id} explains the cross-workspace denial to the local owner ...
    assert c.get(f"{API}/runs/{run['run_id']}", headers=_ws("ws-b")).status_code == 403
    # ... but saving as a test never confirms the id exists elsewhere.
    res = _save(c, "t", headers=_ws("ws-b"), run_id=run["run_id"], dataset_name="T")
    assert res.status_code == 404 and _code(res) == "RUN_NOT_FOUND"


@pytest.mark.parametrize(
    "status", ["running", "pending_approval", "waiting_for_input", "cancelled"]
)
def test_only_completed_runs_qualify(tmp_path: Path, status: str) -> None:
    app, c = _local(tmp_path)
    _workflow(c, "wf", TWO_STEP)
    run = _run(c, "wf", {"order_id": "ORD-DELHI-501"})
    asyncio.run(
        app.state.store.update_run(
            run_id=run["run_id"],
            status=status,
            current_node_id="route",
            current_state=run["current_state"],
            step_count=1,
        )
    )
    res = _save(c, "t", run_id=run["run_id"], dataset_name="T")
    assert res.status_code == 409 and _code(res) == "RUN_NOT_COMPLETED", res.text
    assert c.get(f"{BASE}/datasets/t").status_code == 404


def test_failed_adhoc_forked_and_sub_runs_are_refused(tmp_path: Path) -> None:
    _, c = _local(tmp_path)
    _workflow(c, "wf", ONE_STEP)
    failed = _run(c, "wf", {"order_id": [1]})
    assert failed["status"] == "failed"
    res = _save(c, "t", run_id=failed["run_id"], dataset_name="T")
    assert res.status_code == 409 and _code(res) == "RUN_NOT_COMPLETED"

    adhoc = c.post(
        f"{API}/runs", json={"spec": ONE_STEP, "initial_state": {"order_id": "ORD-DELHI-501"}}
    ).json()
    assert adhoc["status"] == "completed"
    res = _save(c, "t", run_id=adhoc["run_id"], dataset_name="T")
    assert res.status_code == 422 and _code(res) == "RUN_HAS_NO_WORKFLOW"

    _workflow(c, "wf2", TWO_STEP)
    source = _run(c, "wf2", {"order_id": "ORD-DELHI-501"})
    fork = c.post(f"{API}/runs/{source['run_id']}/fork", json={"checkpoint_index": 0})
    assert fork.status_code == 201, fork.text
    res = _save(c, "t", run_id=fork.json()["run_id"], dataset_name="T")
    assert res.status_code == 409 and _code(res) == "RUN_IS_FORK"

    # Sub-runs are not run records: their ids are not found.
    res = _save(c, "t", run_id=f"{source['run_id']}/sub/0", dataset_name="T")
    assert res.status_code == 404 and _code(res) == "RUN_NOT_FOUND"
    assert c.get(f"{BASE}/datasets/t").status_code == 404


def test_unknown_dataset_needs_a_name_and_bad_ids(tmp_path: Path) -> None:
    _, c = _local(tmp_path)
    _workflow(c, "wf", TWO_STEP)
    run = _run(c, "wf", {"order_id": "ORD-DELHI-501"})
    res = _save(c, "new-ds", run_id=run["run_id"])
    assert res.status_code == 422 and _code(res) == "DATASET_NAME_REQUIRED"
    res = _save(c, "new-ds", run_id=run["run_id"], dataset_name="   ")
    assert res.status_code == 422 and _code(res) == "DATASET_NAME_REQUIRED"
    assert c.get(f"{BASE}/datasets/new-ds").status_code == 404
    res = _save(c, "bad id!", run_id=run["run_id"], dataset_name="T")
    assert res.status_code == 400 and _code(res) == "INVALID_DATASET_ID"
    res = _save(c, "new-ds", run_id=run["run_id"], dataset_name="T", output_key="nope")
    assert res.status_code == 422 and _code(res) == "OUTPUT_KEY_MISSING"
    res = _save(c, "new-ds", run_id=run["run_id"], dataset_name="T", case_name="x" * 121)
    assert res.status_code == 422
    created = _save(c, "new-ds", run_id=run["run_id"], dataset_name="T")
    assert created.status_code == 200 and created.json()["name"] == "T"


def test_duplicate_names_and_output_key_and_workflow_mismatch(tmp_path: Path) -> None:
    _, c = _local(tmp_path)
    _workflow(c, "wf", TWO_STEP)
    _workflow(c, "wf-other", TWO_STEP)
    run = _run(c, "wf", {"order_id": "ORD-DELHI-501"})
    again = _run(c, "wf", {"order_id": "ORD-MUMBAI-202"})
    other = _run(c, "wf-other", {"order_id": "ORD-DELHI-501"})
    assert (
        _save(c, "t", run_id=run["run_id"], dataset_name="T", output_key="route").status_code == 200
    )
    # Same run again (default name = run id) and a reused explicit name both conflict.
    dup = _save(c, "t", run_id=run["run_id"], output_key="route")
    assert dup.status_code == 409 and _code(dup) == "CASE_NAME_TAKEN"
    dup = _save(c, "t", run_id=again["run_id"], case_name=run["run_id"], output_key="route")
    assert dup.status_code == 409 and _code(dup) == "CASE_NAME_TAKEN"
    mixed = _save(c, "t", run_id=again["run_id"])
    assert mixed.status_code == 409 and _code(mixed) == "OUTPUT_KEY_MISMATCH"
    wrong = _save(c, "t", run_id=other["run_id"], output_key="route")
    assert wrong.status_code == 409 and _code(wrong) == "DATASET_WORKFLOW_MISMATCH"
    assert c.get(f"{BASE}/datasets/t").json()["revision"] == 1


def test_case_and_size_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, c = _local(tmp_path)
    _workflow(c, "wf", TWO_STEP)
    run = _run(c, "wf", {"order_id": "ORD-DELHI-501"})
    full = [{"name": f"c{i}", "input": "x"} for i in range(studio_engine.MAX_CASES)]
    assert c.put(f"{BASE}/datasets/full", json={"cases": full}).status_code == 200
    res = _save(c, "full", run_id=run["run_id"])
    assert res.status_code == 413 and _code(res) == "DATASET_TOO_LARGE"
    assert c.get(f"{BASE}/datasets/full").json()["case_count"] == studio_engine.MAX_CASES

    monkeypatch.setattr(studio_engine, "MAX_DATASET_BYTES", 300)
    res = _save(c, "small", run_id=run["run_id"], dataset_name="S")
    assert res.status_code == 413 and _code(res) == "DATASET_TOO_LARGE"
    monkeypatch.undo()

    monkeypatch.setattr(studio_evals, "MAX_DATASETS_PER_WORKSPACE", 1)
    res = _save(c, "second", run_id=run["run_id"], dataset_name="Second")
    assert res.status_code == 413 and _code(res) == "TOO_MANY_DATASETS"
    assert c.get(f"{BASE}/datasets/second").status_code == 404


def test_secrets_redacted_in_input_and_expected(tmp_path: Path) -> None:
    _, c = _local(tmp_path)
    _workflow(c, "wf", TWO_STEP)
    run = _run(c, "wf", {"order_id": "ORD-DELHI-501", "note": f"key {SECRET}", "api_key": "x"})
    assert run["status"] == "completed"
    res = _save(c, "t", run_id=run["run_id"], dataset_name="T")
    assert res.status_code == 200, res.text
    assert res.json()["redactions"] == 1  # one case changed (same count rule as PUT)
    case = res.json()["case"]
    assert case["input"]["note"] == "key [REDACTED]" and case["input"]["api_key"] == "[REDACTED]"
    assert case["expected_output"]["note"] == "key [REDACTED]"
    assert case["expected_output"]["api_key"] == "[REDACTED]"
    for url in (f"{BASE}/datasets/t", f"{BASE}/datasets/t/export", f"{BASE}/datasets"):
        assert SECRET not in c.get(url).text
    assert SECRET not in res.text
    # A secret in the case name is redacted too.
    named = _save(c, "t2", run_id=run["run_id"], dataset_name="T", case_name=f"c {SECRET}")
    assert named.status_code == 200 and SECRET not in named.text


def test_revision_conflict(tmp_path: Path) -> None:
    _, c = _local(tmp_path)
    _workflow(c, "wf", TWO_STEP)
    a = _run(c, "wf", {"order_id": "ORD-DELHI-501"})
    b = _run(c, "wf", {"order_id": "ORD-MUMBAI-202"})
    res = _save(c, "t", run_id=a["run_id"], dataset_name="T", expected_revision=3)
    assert res.status_code == 409 and _code(res) == "REVISION_CONFLICT"
    assert (
        _save(c, "t", run_id=a["run_id"], dataset_name="T", expected_revision=0).status_code == 200
    )
    stale = _save(c, "t", run_id=b["run_id"], expected_revision=0)
    assert stale.status_code == 409 and _code(stale) == "REVISION_CONFLICT"
    assert "revision 1" in stale.json()["detail"]["message"]
    assert c.get(f"{BASE}/datasets/t").json()["case_count"] == 1


def test_binding_via_put_and_workspace_isolation(tmp_path: Path) -> None:
    _, c = _local(tmp_path)
    a, b = _ws("ws-a"), _ws("ws-b")
    _workflow(c, "wf-a", TWO_STEP, headers=a)
    cases = [{"name": "one", "input": {"order_id": "ORD-DELHI-501"}}]
    put = c.put(f"{BASE}/datasets/d1", json={"cases": cases, "workflow_id": "wf-a"}, headers=a)
    assert put.status_code == 200 and put.json()["workflow_id"] == "wf-a"
    # Omitting workflow_id keeps the binding (the Evals editor does not send it).
    kept = c.put(f"{BASE}/datasets/d1", json={"name": "renamed", "cases": cases}, headers=a)
    assert kept.json()["workflow_id"] == "wf-a"
    # Another workspace can neither bind to wf-a nor see datasets bound to it.
    bad = c.put(f"{BASE}/datasets/d1", json={"cases": cases, "workflow_id": "wf-a"}, headers=b)
    assert bad.status_code == 404 and _code(bad) == "WORKFLOW_NOT_FOUND"
    assert c.get(f"{BASE}/datasets", params={"workflow_id": "wf-a"}, headers=b).json() == {
        "datasets": [],
        "total": 0,
    }
    assert c.get(f"{BASE}/datasets/d1", headers=b).status_code == 404
    listed = c.get(f"{BASE}/datasets", params={"workflow_id": "wf-a"}, headers=a).json()
    assert [d["dataset_id"] for d in listed["datasets"]] == ["d1"]
    # "" removes the binding.
    cleared = c.put(f"{BASE}/datasets/d1", json={"cases": cases, "workflow_id": ""}, headers=a)
    assert cleared.json()["workflow_id"] is None
    assert c.get(f"{BASE}/datasets", params={"workflow_id": "wf-a"}, headers=a).json()["total"] == 0


OLD_SCHEMA = """
CREATE TABLE studio_eval_datasets (
    workspace_id TEXT NOT NULL,
    dataset_id TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    revision INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    cases_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (workspace_id, dataset_id)
);
"""


def test_datasets_stored_before_the_binding_still_work(tmp_path: Path) -> None:
    db = tmp_path / "p22.sqlite3"
    cases = [{"name": "legacy", "input": {"order_id": "ORD-DELHI-501"}, "metadata": {}}]
    conn = sqlite3.connect(db)
    conn.executescript(OLD_SCHEMA)
    conn.execute(
        "INSERT INTO studio_eval_datasets VALUES (?,?,?,?,?,?,?,?,?)",
        ("default", "old", "Old", "", 1, "h", json.dumps(cases), 1.0, 1.0),
    )
    conn.commit()
    conn.close()
    _, c = _local(tmp_path)
    listed = c.get(f"{BASE}/datasets").json()["datasets"]
    assert [(d["dataset_id"], d["workflow_id"]) for d in listed] == [("old", None)]
    assert c.get(f"{BASE}/datasets/old").json()["cases"][0]["name"] == "legacy"
    exported = c.get(f"{BASE}/datasets/old/export").json()
    assert "workflow_id" not in exported
    imported = c.put(f"{BASE}/datasets/copy", json=exported)
    assert imported.status_code == 200 and imported.json()["workflow_id"] is None
    # The first save-from-run binds an unbound dataset to the run's workflow.
    _workflow(c, "wf", TWO_STEP)
    run = _run(c, "wf", {"order_id": "ORD-MUMBAI-202"})
    res = _save(c, "old", run_id=run["run_id"], case_name="new", dataset_name="ignored")
    assert res.status_code == 200, res.text
    assert res.json()["workflow_id"] == "wf" and res.json()["name"] == "Old"
    assert [x["name"] for x in res.json()["cases"]] == ["legacy", "new"]
    bound = c.get(f"{BASE}/datasets/old/export").json()
    assert bound["workflow_id"] == "wf"
    # A bound export imports back into the same workspace with its binding.
    again = c.put(f"{BASE}/datasets/old-copy", json=bound)
    assert again.status_code == 200 and again.json()["workflow_id"] == "wf"
