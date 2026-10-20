"""P22 review regressions: saving a run as a test stores it faithfully or refuses it.

Promoted from the adversarial review repros (.autopilot/p22/repros). Offline only: synthetic
handlers of the default registry, local-owner profile.
"""

from __future__ import annotations

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
# Same graph, but the router needs a human approval before it runs.
GATED = {
    "name": "gated",
    "entry_node": "lookup",
    "nodes": {
        "lookup": {
            "id": "lookup",
            "handler": "synthetic_order_lookup",
            "default_transition": "route",
        },
        "route": {"id": "route", "handler": "synthetic_router", "requires_approval": True},
    },
    "terminal_nodes": ["route"],
}
# An opaque credential that matches no secret *text* pattern; only its key name marks it.
OPAQUE = "hunter2-opaque-value-xyz"


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_api_app(db_path=str(tmp_path / "r.sqlite3"), profile="local-owner"))


def _workflow(
    c: TestClient, wid: str, spec: dict[str, Any], headers: dict[str, str] | None = None
) -> None:
    r = c.post(f"{API}/workflows", json={"workflow_id": wid, "spec": spec}, headers=headers)
    assert r.status_code in (200, 201), r.text


def _run(c: TestClient, wid: str, state: dict[str, Any]) -> dict[str, Any]:
    r = c.post(f"{API}/runs", json={"workflow_id": wid, "initial_state": state})
    assert r.status_code == 201, r.text
    out: dict[str, Any] = r.json()
    return out


def _save(c: TestClient, dataset_id: str, **body: Any) -> Any:
    return c.post(f"{BASE}/datasets/{dataset_id}/cases/from-run", json=body)


def test_output_key_naming_a_credential_does_not_store_it_in_clear(tmp_path: Path) -> None:
    """H: output_key='api_key' lifts the value out of its dict, so key-based redaction misses it."""
    c = _client(tmp_path)
    _workflow(c, "wf", TWO_STEP)
    run = _run(c, "wf", {"order_id": "ORD-DELHI-501", "api_key": OPAQUE})
    assert run["status"] == "completed" and "api_key" in run["current_state"]
    res = _save(c, "t", run_id=run["run_id"], dataset_name="T", output_key="api_key")
    # Either refused (secret-named output key) or stored redacted; never the clear value.
    if res.status_code == 200:
        assert res.json()["case"]["expected_output"] == "[REDACTED]"
        assert res.json()["redactions"] >= 1
    else:
        assert res.status_code == 422
    for url in (f"{BASE}/datasets/t", f"{BASE}/datasets/t/export"):
        assert OPAQUE not in c.get(url).text
    assert OPAQUE not in res.text


def test_run_inputs_are_not_silently_truncated_to_100_keys(tmp_path: Path) -> None:
    """M: redact_run_content keeps only 100 dict keys, so the saved input is not the run's input."""
    c = _client(tmp_path)
    _workflow(c, "wf", TWO_STEP)
    state = {"order_id": "ORD-DELHI-501", **{f"k{i}": i for i in range(120)}}
    run = _run(c, "wf", state)
    assert run["status"] == "completed"
    res = _save(c, "t", run_id=run["run_id"], dataset_name="T")
    # Either refuse a run that cannot be stored faithfully, or store all 121 keys.
    if res.status_code == 200:
        assert res.json()["case"]["input"] == state
    else:
        assert res.status_code in (413, 422)
        assert c.get(f"{BASE}/datasets/t").status_code == 404


def test_redacted_case_names_cannot_collide_and_brick_the_dataset(tmp_path: Path) -> None:
    """M: CASE_NAME_TAKEN is checked before redaction; two names redact to the same string."""
    c = _client(tmp_path)
    _workflow(c, "wf", TWO_STEP)
    a = _run(c, "wf", {"order_id": "ORD-DELHI-501"})
    b = _run(c, "wf", {"order_id": "ORD-MUMBAI-202"})
    third = _run(c, "wf", {"order_id": "ORD-DELHI-501", "n": 3})
    first = _save(
        c, "t", run_id=a["run_id"], dataset_name="T", case_name="c sk-abcdef1234567890abcdef"
    )
    assert first.status_code == 200, first.text
    second = _save(c, "t", run_id=b["run_id"], case_name="c sk-zzzzzz1234567890zzzzzz")
    names = [x["name"] for x in c.get(f"{BASE}/datasets/t").json()["cases"]]
    assert len(names) == len(set(names)), f"duplicate stored case names: {names}"
    if second.status_code != 200:
        assert second.status_code == 409 and second.json()["detail"]["code"] == "CASE_NAME_TAKEN"
    # The dataset stays usable: another run can still be appended.
    later = _save(c, "t", run_id=third["run_id"])
    assert later.status_code == 200, later.text


def test_run_completed_through_an_approval_is_not_saved_as_a_test(tmp_path: Path) -> None:
    """M: the replay of such a run always pauses at the gate, so the test never checks anything."""
    c = _client(tmp_path)
    _workflow(c, "gwf", GATED)
    run = _run(c, "gwf", {"order_id": "ORD-DELHI-501"})
    assert run["status"] == "pending_approval", run["status"]
    approval_id = run["pending_approval"]["approval_id"]
    d = c.post(
        f"{API}/runs/{run['run_id']}/decide",
        json={"approval_id": approval_id, "decision": "approve"},
    )
    assert d.status_code == 200 and d.json()["status"] == "completed", d.text
    res = _save(c, "g", run_id=run["run_id"], dataset_name="G", output_key="route")
    if res.status_code == 200:
        # If accepted, replaying the saved case must actually be able to pass.
        target = {"kind": "workflow", "id": "gwf", "output_key": "route"}
        ev = c.post(f"{BASE}/runs", json={"dataset_id": "g", "target": target})
        summary = ev.json()["summary"]
        assert summary["paused"] == 0 and summary["passed"] == 1, summary
    else:
        assert res.status_code == 409
        assert c.get(f"{BASE}/datasets/g").status_code == 404


def test_bound_export_still_imports_into_another_workspace(tmp_path: Path) -> None:
    """L: the export is 'portable JSON' format_version 1, but a bound one now 404s elsewhere."""
    c = _client(tmp_path)
    a, b = {"X-Workspace-ID": "ws-a"}, {"X-Workspace-ID": "ws-b"}
    _workflow(c, "wf-a", TWO_STEP, headers=a)
    cases = [{"name": "one", "input": {"order_id": "ORD-DELHI-501"}}]
    put = c.put(f"{BASE}/datasets/d1", json={"cases": cases, "workflow_id": "wf-a"}, headers=a)
    assert put.status_code == 200, put.text
    exported = c.get(f"{BASE}/datasets/d1/export", headers=a).json()
    imported = c.put(f"{BASE}/datasets/d1", json=exported, headers=b)
    # The import should succeed (binding dropped / not carried across workspaces).
    assert imported.status_code == 200, imported.text
    assert imported.json()["workflow_id"] is None


def test_refusals_carry_stable_codes(tmp_path: Path) -> None:
    c = _client(tmp_path)
    _workflow(c, "wf", TWO_STEP)
    _workflow(c, "gwf", GATED)
    run = _run(c, "wf", {"order_id": "ORD-DELHI-501", "api_key": OPAQUE})
    secret = _save(c, "t", run_id=run["run_id"], dataset_name="T", output_key="api_key")
    assert secret.json()["detail"]["code"] == "OUTPUT_KEY_IS_SECRET"
    big = _run(c, "wf", {"order_id": "ORD-DELHI-501", **{f"k{i}": i for i in range(120)}})
    too_big = _save(c, "t", run_id=big["run_id"], dataset_name="T")
    assert too_big.status_code == 413
    assert too_big.json()["detail"]["code"] == "RUN_TOO_LARGE_FOR_TEST"
    gated = _run(c, "gwf", {"order_id": "ORD-DELHI-501"})
    approval_id = gated["pending_approval"]["approval_id"]
    c.post(
        f"{API}/runs/{gated['run_id']}/decide",
        json={"approval_id": approval_id, "decision": "approve"},
    )
    human = _save(c, "g", run_id=gated["run_id"], dataset_name="G")
    assert human.status_code == 409
    assert human.json()["detail"]["code"] == "RUN_HAD_HUMAN_STEP"
