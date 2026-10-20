"""Security regressions: run events, workflow import/export, credentials, destination policy."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest
from _local_env import local_headers
from fastapi.testclient import TestClient

from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.workspaces.repository import WorkspaceRepository
from nexura_runtime.workflow import ModuleRegistry, WorkflowNode, WorkflowSpec

SECRET = "sk-live-ABCDEF123456"
SPEC: dict[str, Any] = {
    "name": "w",
    "entry_node": "n",
    "terminal_nodes": ["n"],
    "nodes": {"n": {"id": "n", "handler": "synthetic_order_lookup", "transitions": {}}},
}


def _local(tmp_path: Path, **kw: Any) -> tuple[TestClient, dict[str, dict[str, str]]]:
    repo = WorkspaceRepository()
    repo.create_workspace("wsA", "A", owner_id="local-owner")
    repo.create_workspace("wsB", "B", owner_id="local-owner")
    keys: dict[str, dict[str, str]] = {}
    for name, ws, _pid, role in [
        ("a", "wsA", "alice", "admin"),
        ("b", "wsB", "bob", "admin"),
        ("v", "wsA", "vic", "viewer"),
    ]:
        keys[name] = local_headers(ws, role)
    app = create_api_app(
        db_path=tmp_path / "s.db",
        workspace_repo=repo,
        profile="local-owner",
        **kw,
    )
    return TestClient(app), keys


def _failing_app(tmp_path: Path) -> Any:
    reg = ModuleRegistry()

    async def boom(state: dict[str, Any]) -> Any:
        raise RuntimeError(f"401: password={SECRET} Bearer {SECRET}")

    async def ok(state: dict[str, Any]) -> Any:
        return {"api_key": SECRET + "-out", "note": "x"}

    reg.register("boom", boom)
    reg.register("ok", ok)
    return create_api_app(registry=reg, db_path=tmp_path / "s.sqlite3")


@pytest.mark.asyncio
async def test_run_events_redact_state_and_node_failures(tmp_path: Path) -> None:
    app = _failing_app(tmp_path)
    good = WorkflowSpec(
        name="g",
        entry_node="a",
        nodes={"a": WorkflowNode(id="a", handler="ok")},
        terminal_nodes=["a"],
    )
    bad = WorkflowSpec(
        name="b",
        entry_node="a",
        nodes={"a": WorkflowNode(id="a", handler="boom")},
        terminal_nodes=["a"],
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        for spec, state in ((good, {"api_key": SECRET}), (bad, {"note": SECRET})):
            r = await c.post("/api/v1/runs", json={"spec": spec.to_dict(), "initial_state": state})
            run_id = r.json()["run_id"]
            ev = await c.get(f"/api/v1/runs/{run_id}/events")
            snap = await c.get(f"/api/v1/runs/{run_id}")
            assert SECRET not in ev.text
            assert "NODE_" in ev.text
            assert SECRET not in str(snap.json()["events"])


def test_secret_value_patterns_are_redacted() -> None:
    from nexura_runtime.observability.run_diagnostics import redact_run_content

    out = redact_run_content({"msg": f"using {SECRET} at https://u:hunter2pw@h/x"})
    assert SECRET not in str(out)
    assert "hunter2pw" not in str(out)


def test_local_export_cannot_overwrite_an_arbitrary_directory(tmp_path: Path) -> None:
    c, ks = _local(tmp_path)
    assert c.post("/api/v1/workflows", headers=ks["a"], json={"workflow_id": "w1", "spec": SPEC})
    c.app.state.export_root = tmp_path / "exports"
    c.app.state.export_root.mkdir()
    victim = tmp_path / "other"
    victim.mkdir()
    (victim / "pyproject.toml").write_text("ORIGINAL")
    r = c.post("/api/v1/workflows/w1/export", headers=ks["a"], json={"target_dir": str(victim)})
    assert r.status_code == 400
    assert (victim / "pyproject.toml").read_text() == "ORIGINAL"
    r = c.post("/api/v1/workflows/w1/export", headers=ks["a"], json={})
    assert r.status_code == 200
    assert r.json()["bundle_path"] is None
    assert "workflow.json" in r.json()["files"]


def test_shared_export_is_confined_to_export_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    c, ks = _local(tmp_path)
    c.app.state.export_root = root  # type: ignore[attr-defined]
    assert c.post("/api/v1/workflows", headers=ks["a"], json={"workflow_id": "w1", "spec": SPEC})
    outside = tmp_path / "elsewhere"
    r = c.post("/api/v1/workflows/w1/export", headers=ks["a"], json={"target_dir": str(outside)})
    assert r.status_code == 400
    assert not outside.exists()
    r = c.post("/api/v1/workflows/w1/export", headers=ks["a"], json={"target_dir": "out1"})
    assert r.status_code == 200
    assert r.json()["bundle_path"] == "out1"
    assert (root / "out1" / "workflow.json").exists()
    again = c.post("/api/v1/workflows/w1/export", headers=ks["a"], json={"target_dir": "out1"})
    assert again.status_code == 409


def test_export_scrubs_header_list_and_url_secrets(tmp_path: Path) -> None:
    c, ks = _local(tmp_path)
    spec = {
        **SPEC,
        "nodes": {
            "n": {
                "id": "n",
                "handler": "synthetic_order_lookup",
                "transitions": {},
                "config": {
                    "headers": {"Authorization": "Bearer LEAK_A", "X-API-Key": "LEAK_B"},
                    "apikey": "LEAK_C",
                    "api_key": ["LEAK_D"],
                    "base_url": "https://user:LEAK_E@db.internal/x",
                    "note": f"token {SECRET}",
                },
            }
        },
    }
    assert c.post("/api/v1/workflows", headers=ks["a"], json={"workflow_id": "w2", "spec": spec})
    d = c.get("/api/v1/workflows/w2/export/download", headers=ks["a"])
    assert d.status_code == 200
    z = zipfile.ZipFile(io.BytesIO(d.content))
    blob = "".join(z.read(n).decode("utf-8", "ignore") for n in z.namelist())
    for leak in ("LEAK_A", "LEAK_B", "LEAK_C", "LEAK_D", "LEAK_E", SECRET):
        assert leak not in blob


def test_import_runs_create_validation_and_conflicts(tmp_path: Path) -> None:
    c, ks = _local(tmp_path, approval_required_handlers=frozenset({"protected_action"}))
    bad = {
        "name": "x",
        "entry_node": "nope",
        "nodes": {"effect": {"id": "effect", "handler": "protected_action"}},
    }
    assert (
        c.post("/api/v1/workflows/import", headers=ks["a"], json={"spec": bad}).status_code == 422
    )
    # Existing id: 409 for the owning workspace and, in local-owner mode, for another local
    # workspace too (one person owns both); nobody's ownership changes.
    assert c.post("/api/v1/workflows", headers=ks["a"], json={"workflow_id": "dup", "spec": SPEC})
    for path in ("/api/v1/workflows", "/api/v1/workflows/import"):
        assert (
            c.post(path, headers=ks["a"], json={"workflow_id": "dup", "spec": SPEC}).status_code
            == 409
        )
        r = c.post(path, headers=ks["b"], json={"workflow_id": "dup", "spec": SPEC})
        assert r.status_code == 409
    assert c.get("/api/v1/workflows/dup", headers=ks["a"]).status_code == 200
    # Local owner: a direct read from the other workspace is denied and names the owner.
    foreign = c.get("/api/v1/workflows/dup", headers=ks["b"])
    assert foreign.status_code == 403
    assert foreign.json()["detail"]["code"] == "CROSS_WORKSPACE_ACCESS_DENIED"
    assert "'wsA'" in foreign.json()["detail"]["message"]
    assert "spec" not in foreign.json()
    assert c.get("/api/v1/workflows/never-created", headers=ks["b"]).status_code == 404


def test_node_config_cannot_disable_destination_policy(tmp_path: Path) -> None:
    c, ks = _local(tmp_path)
    for key, val in (
        ("allow_loopback", True),
        ("allow_private_ranges", True),
        ("allowed_hosts", ["x"]),
    ):
        spec = {**SPEC, "nodes": {"n": {**SPEC["nodes"]["n"], "config": {key: val}}}}
        r = c.post("/api/v1/workflows", headers=ks["a"], json={"spec": spec})
        assert r.status_code == 422
        assert "DESTINATION_POLICY_OVERRIDE_FORBIDDEN" in r.text
        r = c.post("/api/v1/runs", headers=ks["a"], json={"spec": spec})
        assert r.status_code == 422


def test_credential_authz_and_isolation(tmp_path: Path) -> None:
    c, ks = _local(tmp_path)
    body = {"credential_id": "cA", "credential_type": "api_key", "secret": "topsecret"}
    assert c.post("/api/v1/credentials", headers=ks["a"], json=body).status_code == 201
    assert c.post("/api/v1/credentials/cA/verify", headers=ks["v"]).status_code == 200
    assert c.post("/api/v1/credentials/cA/verify", headers=ks["a"]).status_code == 200
    # Ids are workspace-scoped (P9): workspace B can create its own "cA" without learning
    # that A has one and without touching A's credential.
    assert c.post("/api/v1/credentials", headers=ks["a"], json=body).status_code == 409
    other = {"credential_id": "cA", "credential_type": "api_key", "secret": "bobsecret"}
    assert c.post("/api/v1/credentials", headers=ks["b"], json=other).status_code == 201
    assert c.post("/api/v1/credentials/cA/revoke", headers=ks["b"]).status_code == 200
    assert c.delete("/api/v1/credentials/cA", headers=ks["b"]).status_code in (200, 204)
    mine = c.get("/api/v1/credentials/cA", headers=ks["a"])
    assert mine.status_code == 200 and mine.json()["status"] != "revoked"
    assert "topsecret" not in mine.text and "bobsecret" not in mine.text
    # Foreign and missing ids are indistinguishable.
    only_a = {"credential_id": "cOnlyA", "credential_type": "api_key", "secret": "topsecret"}
    assert c.post("/api/v1/credentials", headers=ks["a"], json=only_a).status_code == 201
    foreign = c.get("/api/v1/credentials/cOnlyA", headers=ks["b"])
    missing = c.get("/api/v1/credentials/nope", headers=ks["b"])
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["detail"]["code"] == missing.json()["detail"]["code"]
    for method, path in (
        ("post", "/api/v1/credentials/cOnlyA/verify"),
        ("post", "/api/v1/credentials/cOnlyA/revoke"),
        ("delete", "/api/v1/credentials/cOnlyA"),
    ):
        assert getattr(c, method)(path, headers=ks["b"]).status_code == 404
    assert c.get("/api/v1/credentials/cOnlyA", headers=ks["a"]).json()["status"] != "revoked"
