"""Second-campaign regressions for triggers: scopes, workflow ownership, dedup, cron, redaction."""

from __future__ import annotations

import time
from pathlib import Path

from _local_env import local_headers
from fastapi.testclient import TestClient
from test_security_b7_triggers import (  # type: ignore[import-not-found]
    TRIG,
    _local,
    _register,
)

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.triggers.errors import InvalidScheduleError
from nexura_runtime.service.triggers.schedules import calculate_next_cron_fire
from nexura_runtime.service.workspaces.repository import WorkspaceRepository


def _app_with_keys(tmp_path: Path, keys: list[tuple[str, str, str, str, list[str] | None]]):
    repo = WorkspaceRepository()
    for ws in {k[1] for k in keys}:
        repo.create_workspace(ws, ws, owner_id="owner")
    raw: dict[str, dict[str, str]] = {}
    for name, ws, _pid, role, _scopes in keys:
        raw[name] = local_headers(ws, role)
    app = create_api_app(
        db_path=tmp_path / "s.db",
        workspace_repo=repo,
        profile="local-owner",
    )
    return TestClient(app), raw


def test_trigger_target_workflow_must_exist_in_caller_workspace(tmp_path: Path) -> None:
    c, k, _ = _local(tmp_path)
    assert _register(c, k["a"], "e1", workflow_id="missing").status_code == 404
    # Owned by workspace B: identical 404 for workspace A in shared mode.
    missing = c.post(
        f"{TRIG}/schedules",
        headers=k["a"],
        json={"schedule_id": "s", "workflow_id": "missing", "interval_seconds": 120},
    )
    foreign = c.post(
        f"{TRIG}/schedules",
        headers=k["a"],
        json={"schedule_id": "s", "workflow_id": "evil", "interval_seconds": 120},
    )
    assert missing.status_code == foreign.status_code == 404
    assert missing.json()["detail"]["code"] == foreign.json()["detail"]["code"]
    assert _register(c, k["a"], "e2", workflow_id="evil").status_code == 404


def test_outbox_dedup_key_is_unambiguous_across_workspaces(tmp_path: Path) -> None:
    c, k = _app_with_keys(
        tmp_path,
        [("m", "a", "mallory", "admin", None), ("v", "a:b", "victim", "admin", None)],
    )
    body = {"channel": "email", "recipient": "x@y.z", "subject_or_template": "s", "body": "b"}
    r1 = c.post(f"{TRIG}/outbox/test-dispatch", headers=k["m"], json={**body, "dedup_key": "b:c"})
    r2 = c.post(f"{TRIG}/outbox/test-dispatch", headers=k["v"], json={**body, "dedup_key": "c"})
    assert r1.status_code == r2.status_code == 200
    assert r2.json()["is_duplicate"] is False
    again = c.post(f"{TRIG}/outbox/test-dispatch", headers=k["v"], json={**body, "dedup_key": "c"})
    assert again.json()["is_duplicate"] is True


def test_impossible_cron_is_rejected_quickly() -> None:
    for expr in ("0 0 31 2 *", "0 0 30 2 *"):
        start = time.process_time()
        try:
            calculate_next_cron_fire(time.time(), expr, "America/New_York")
        except InvalidScheduleError:
            pass
        else:
            raise AssertionError("impossible cron accepted")
        assert time.process_time() - start < 0.3


def test_cron_skip_ahead_matches_expected_fires() -> None:
    base = 1_700_000_000.0  # 2023-11-14 22:13:20 UTC
    nxt = calculate_next_cron_fire(base, "30 3 29 2 *", "UTC")  # next leap day: 2024-02-29 03:30
    assert nxt == 1_709_177_400.0
    assert calculate_next_cron_fire(base, "0 9 * * 1-5", "Asia/Kolkata") > base


def test_schedule_api_never_matching_cron_is_400_and_does_not_block(tmp_path: Path) -> None:
    c, k, _ = _local(tmp_path)
    start = time.process_time()
    r = c.post(
        f"{TRIG}/schedules",
        headers=k["a"],
        json={
            "schedule_id": "s",
            "workflow_id": "wf",
            "cron_expr": "0 0 31 2 *",
            "timezone": "America/New_York",
        },
    )
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "INVALID_CRON"
    assert time.process_time() - start < 0.5
