"""P35 A4: opt-in capture of failed service runs as repro bundles (off by default)."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from nexura_agents.repro import ReproBundle, replay_repro_bundle

from nexura_runtime.cli.main import main
from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.repro_capture import CAPTURE_ENV, TABLE
from nexura_runtime.workflow import ModuleRegistry

API = "/api/v1"
SECRET = "fake-service-key-0123456789"
SPEC: dict[str, Any] = {
    "name": "charge",
    "entry_node": "quote",
    "nodes": {
        "quote": {"id": "quote", "handler": "quote", "default_transition": "charge"},
        "charge": {"id": "charge", "handler": "charge"},
    },
    "terminal_nodes": ["charge"],
}
GATED = {
    **SPEC,
    "nodes": {**SPEC["nodes"], "charge": {**SPEC["nodes"]["charge"], "requires_approval": True}},
}
VALIDATION_SPEC: dict[str, Any] = {
    "name": "quote validation",
    "entry_node": "quote",
    "nodes": {
        "quote": {"id": "quote", "handler": "quote", "default_transition": "check"},
        "check": {"id": "check", "handler": "check"},
    },
    "terminal_nodes": ["check"],
}
GATED_VALIDATION = {
    **VALIDATION_SPEC,
    "nodes": {
        **VALIDATION_SPEC["nodes"],
        "check": {**VALIDATION_SPEC["nodes"]["check"], "requires_approval": True},
    },
}


def _registry(calls: list[str]) -> ModuleRegistry:
    registry = ModuleRegistry()

    @registry.register_handler("quote", effect="pure")
    async def quote(state: dict[str, Any]) -> dict[str, Any]:
        calls.append("quote")
        return {"amount": state["qty"] * 500}

    @registry.register_handler("charge")
    async def charge(state: dict[str, Any]) -> dict[str, Any]:
        calls.append("charge")
        if state.get("fail", True):
            raise ValueError(f"card declined for amount {state['amount']} using {state['api_key']}")
        return {"charged": True}

    @registry.register_handler("check", effect="pure")
    async def check(state: dict[str, Any]) -> dict[str, Any]:
        calls.append("check")
        raise ValueError(f"invalid quote for amount {state['amount']} using {state['api_key']}")

    return registry


def _client(tmp_path: Path, calls: list[str], **kwargs: Any) -> TestClient:
    app = create_api_app(
        db_path=str(tmp_path / "svc.sqlite3"),
        profile="local-owner",
        registry=_registry(calls),
        **kwargs,
    )
    return TestClient(app)


def _run(c: TestClient, spec: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    r = c.post(f"{API}/runs", json={"spec": spec, "initial_state": state})
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


def _unknown_charge(c: TestClient, run: dict[str, Any]) -> None:
    assert run["status"] == "paused", run
    effects = c.get(f"{API}/runs/{run['run_id']}/effects").json()["effects"]
    assert len(effects) == 1 and effects[0]["status"] == "indeterminate"
    assert effects[0]["intent"]["effect_class"] == "non_idempotent"
    assert c.get(f"{API}/runs/{run['run_id']}/repro").status_code == 404


@pytest.fixture(autouse=True)
def _no_capture_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CAPTURE_ENV, raising=False)


def test_capture_is_off_by_default(tmp_path: Path) -> None:
    calls: list[str] = []
    c = _client(tmp_path, calls)
    assert c.app.state.repro_capture is None  # type: ignore[attr-defined]
    run = _run(c, SPEC, {"qty": 2, "api_key": SECRET})
    _unknown_charge(c, run)
    r = c.get(f"{API}/runs/{run['run_id']}/repro")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "REPRO_BUNDLE_NOT_FOUND"
    with sqlite3.connect(tmp_path / "svc.sqlite3") as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    assert TABLE not in tables


def test_failed_run_bundle_downloads_redacted_and_replays(tmp_path: Path) -> None:
    calls: list[str] = []
    c = _client(tmp_path, calls, capture_failed_runs=True)
    run = _run(c, SPEC, {"qty": 2, "api_key": SECRET})
    _unknown_charge(c, run)
    assert calls == ["quote", "charge"]
    # An uncertain charge is not a failed run. Keep failed-run capture/replay coverage
    # on a separate, genuinely pure validation failure.
    run = _run(c, VALIDATION_SPEC, {"qty": 2, "api_key": SECRET})
    assert run["status"] == "failed", run
    assert calls == ["quote", "charge", "quote", "check"]

    r = c.get(f"{API}/runs/{run['run_id']}/repro")
    assert r.status_code == 200, r.text
    assert r.headers["content-disposition"] == (
        f'attachment; filename="{run["run_id"]}.repro.json"'
    )
    assert SECRET not in r.text
    data = r.json()
    assert data["run_kind"] == "workflow"
    assert data["inputs"]["api_key"] == {"$redacted": "api_key"}
    assert data["expected_outcome"]["failing_step"] == "check"
    assert "invalid quote for amount 1000" in data["expected_outcome"]["error_message"]

    path = tmp_path / "downloaded.json"
    path.write_text(r.text)
    result = asyncio.run(replay_repro_bundle(ReproBundle.load(path)))
    assert result.reproduced, result.first_divergence
    assert result.notes == ()  # a new bundle records the (null) final state of a failed run
    assert data["expected_outcome"]["state"] is None
    assert calls == ["quote", "charge", "quote", "check"]  # replay ran no handler

    # The same bundle through the CLI, straight from the service database.
    out = tmp_path / "cli.json"
    db = str(tmp_path / "svc.sqlite3")
    assert main(["repro", "export", run["run_id"], "--db", db, "-o", str(out)]) == 0
    assert SECRET not in out.read_text()
    assert main(["repro", "run", str(out)]) == 0
    # Another workspace's name does not find it.
    other = tmp_path / "other.json"
    assert (
        main(
            [
                "repro",
                "export",
                run["run_id"],
                "--db",
                db,
                "-o",
                str(other),
                "--workspace",
                "elsewhere",
            ]
        )
        == 1
    )
    assert not other.exists()


def test_completed_runs_get_no_bundle_and_unknown_runs_404(tmp_path: Path) -> None:
    c = _client(tmp_path, [], capture_failed_runs=True)
    run = _run(c, SPEC, {"qty": 1, "api_key": SECRET, "fail": False})
    assert run["status"] == "completed", run
    r = c.get(f"{API}/runs/{run['run_id']}/repro")
    assert r.status_code == 404
    capture = c.app.state.repro_capture  # type: ignore[attr-defined]
    assert capture.recorder(run["run_id"]) is None  # nothing kept
    assert c.get(f"{API}/runs/run-doesnotexist/repro").status_code == 404


def test_failure_after_an_approval_decision_is_captured(tmp_path: Path) -> None:
    calls: list[str] = []
    c = _client(tmp_path, calls, capture_failed_runs=True)
    run = _run(c, GATED, {"qty": 3, "api_key": SECRET})
    assert run["status"] == "pending_approval", run
    assert c.get(f"{API}/runs/{run['run_id']}/repro").status_code == 404
    d = c.post(
        f"{API}/runs/{run['run_id']}/decide",
        json={"approval_id": run["pending_approval"]["approval_id"], "decision": "approve"},
    )
    assert d.status_code == 200, d.text
    _unknown_charge(c, d.json())
    assert calls == ["quote", "charge"]
    run = _run(c, GATED_VALIDATION, {"qty": 3, "api_key": SECRET})
    assert run["status"] == "pending_approval", run
    d = c.post(
        f"{API}/runs/{run['run_id']}/decide",
        json={"approval_id": run["pending_approval"]["approval_id"], "decision": "approve"},
    )
    assert d.status_code == 200 and d.json()["status"] == "failed", d.text
    r = c.get(f"{API}/runs/{run['run_id']}/repro")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["operations"] == ["run", "resume"]
    assert data["approvals"] == [{"decision": "approve"}]
    result = asyncio.run(replay_repro_bundle(ReproBundle(data)))
    assert result.reproduced, result.first_divergence
    assert result.outcome["failing_step"] == "check"
    assert calls == ["quote", "charge", "quote", "check"]


def test_environment_setting_and_explicit_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(CAPTURE_ENV, "1")
    on = create_api_app(db_path=str(tmp_path / "a.sqlite3"), profile="local-owner")
    assert on.state.repro_capture is not None
    off = create_api_app(
        db_path=str(tmp_path / "b.sqlite3"), profile="local-owner", capture_failed_runs=False
    )
    assert off.state.repro_capture is None


def test_bundle_stays_downloadable_after_capture_is_turned_off(tmp_path: Path) -> None:
    c = _client(tmp_path, [], capture_failed_runs=True)
    run = _run(c, SPEC, {"qty": 1, "api_key": SECRET})
    _unknown_charge(c, run)
    unknown_id = run["run_id"]
    run = _run(c, VALIDATION_SPEC, {"qty": 1, "api_key": SECRET})
    assert run["status"] == "failed", run
    c.close()
    later = _client(tmp_path, [])  # same database, capture off
    assert later.get(f"{API}/runs/{unknown_id}/repro").status_code == 404
    r = later.get(f"{API}/runs/{run['run_id']}/repro")
    assert r.status_code == 200, r.text
    assert json.loads(r.text)["expected_outcome"]["status"] == "failed"
