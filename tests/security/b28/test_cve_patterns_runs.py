"""B28 stage 1: regression probes for the n8n / Langflow October 2026 bug classes.

Every test asserts the SAFE behaviour: it passes when Nexura is safe and fails when vulnerable.
Offline only: mock transports, monkeypatched DNS, no real sockets (a global guard enforces it).
Run:  PYTHONPATH=tests/connectors .venv/bin/python -m pytest <this file>
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path
from typing import Any

import pytest
from _local_env import local_headers
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tests" / "connectors"))

from nexura_runtime.service.api.app import create_api_app  # noqa: E402
from nexura_runtime.service.workspaces.repository import WorkspaceRepository  # noqa: E402
from nexura_runtime.workflow import (  # noqa: E402
    ModuleRegistry,
    WorkflowNode,
    WorkflowSpec,
    interrupt,
)

PLAINTEXT_ENV = "NEXURA_INSECURE_ALLOW_PLAINTEXT_SECRETS"
PROTO_KEYS = ["__proto__", "constructor", "__class__", "__init__", "__dict__", "prototype"]


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hard guard: any real socket connect fails the test."""
    monkeypatch.setenv(PLAINTEXT_ENV, "I_UNDERSTAND_THIS_IS_UNSAFE")

    def boom(*a: Any, **k: Any) -> Any:
        raise AssertionError("REAL NETWORK CONNECT ATTEMPTED")

    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket.socket, "connect_ex", boom)


def _dns(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, Any]) -> list[str]:
    """Fake getaddrinfo. A mapping value may be a str or a list (successive answers)."""
    seen: list[str] = []

    def fake(host: str, port: Any, *a: Any, **k: Any) -> list[Any]:
        seen.append(host)
        if host not in mapping:
            raise socket.gaierror("no such host")
        val = mapping[host]
        if isinstance(val, list):
            ip = val[min(len([s for s in seen if s == host]) - 1, len(val) - 1)]
        else:
            ip = val
        fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(fam, socket.SOCK_STREAM, 6, "", (ip, port or 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    return seen


# ======================================================================================
# 1. Cross-user resume / approve / fork / cancel
# ======================================================================================
def _registry() -> ModuleRegistry:
    reg = ModuleRegistry()

    async def prep(state: dict[str, Any]) -> dict[str, Any]:
        return {"base": 1}

    async def ask(state: dict[str, Any]) -> dict[str, Any]:
        return {"answer": interrupt({"question": "ok?"})}

    async def send(state: dict[str, Any]) -> dict[str, Any]:
        return {"sent": True}

    async def fin(state: dict[str, Any]) -> dict[str, Any]:
        return {"done": True}

    for n, f in (("prep", prep), ("ask", ask), ("send", send), ("fin", fin)):
        reg.register(n, f)
    return reg


def _chain(name: str, *nodes: WorkflowNode) -> dict[str, Any]:
    for i, n in enumerate(nodes[:-1]):
        n.default_transition = nodes[i + 1].id
    return WorkflowSpec(
        name=name,
        entry_node=nodes[0].id,
        nodes={n.id: n for n in nodes},
        terminal_nodes=[nodes[-1].id],
    ).to_dict()


ASK = _chain(
    "ask",
    WorkflowNode(id="prep", handler="prep"),
    WorkflowNode(id="ask", handler="ask"),
    WorkflowNode(id="fin", handler="fin"),
)
APPR = _chain(
    "appr",
    WorkflowNode(id="prep", handler="prep"),
    WorkflowNode(id="send", handler="send", requires_approval=True),
    WorkflowNode(id="fin", handler="fin"),
)


@pytest.fixture
def local_env(tmp_path: Path) -> tuple[TestClient, dict[str, dict[str, str]]]:
    repo = WorkspaceRepository()
    repo.create_workspace("wsA", "A", owner_id="local-owner")
    repo.create_workspace("wsB", "B", owner_id="local-owner")
    keys: dict[str, dict[str, str]] = {}
    for name, ws, _pid, role in [
        ("alice", "wsA", "alice", "admin"),  # same workspace, admin
        ("alex", "wsA", "alex", "editor"),  # same workspace, editor
        ("vic", "wsA", "vic", "viewer"),  # same workspace, viewer
        ("bob", "wsB", "bob", "admin"),  # other workspace, admin
    ]:
        keys[name] = local_headers(ws, role)
    app = create_api_app(
        registry=_registry(),
        db_path=tmp_path / "s.db",
        workspace_repo=repo,
        profile="local-owner",
    )
    return TestClient(app), keys


def _start(c: TestClient, h: dict[str, str], spec: dict[str, Any]) -> dict[str, Any]:
    r = c.post("/api/v1/runs", headers=h, json={"spec": spec, "initial_state": {}})
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


def _run_status(c: TestClient, h: dict[str, str], run_id: str) -> str:
    r = c.get(f"/api/v1/runs/{run_id}", headers=h)
    assert r.status_code == 200, r.text
    return str(r.json()["status"])


def test_cross_workspace_cannot_decide_input_fork_cancel_read(local_env: Any) -> None:
    c, k = local_env
    appr = _start(c, k["alice"], APPR)
    assert appr["status"] == "pending_approval", appr
    ask = _start(c, k["alice"], ASK)
    assert ask["status"] == "waiting_for_input", ask
    aid = appr["pending_approval"]["approval_id"]
    iid = ask["pending_input"]["interrupt_id"]
    h = k["bob"]
    # Local owner: another workspace's run is denied (403) and nothing is returned or changed.
    denied = {403}
    for path in ("", "/events", "/history"):
        r = c.get(f"/api/v1/runs/{ask['run_id']}{path}", headers=h)
        assert r.status_code in denied, r.text
        assert r.json()["detail"]["code"] == "CROSS_WORKSPACE_ACCESS_DENIED"
        assert "state" not in r.json() and "items" not in r.json()
    r = c.post(
        f"/api/v1/runs/{appr['run_id']}/decide",
        headers=h,
        json={"approval_id": aid, "decision": "approve"},
    )
    assert r.status_code in denied, r.text
    r = c.post(
        f"/api/v1/runs/{ask['run_id']}/input", headers=h, json={"interrupt_id": iid, "value": "yes"}
    )
    assert r.status_code in denied, r.text
    r = c.post(f"/api/v1/runs/{ask['run_id']}/fork", headers=h, json={"checkpoint_index": 0})
    assert r.status_code in denied, r.text
    r = c.post(f"/api/v1/runs/{appr['run_id']}/cancel", headers=h, json={})
    assert r.status_code in denied, r.text
    # nothing changed for the owner
    assert _run_status(c, k["alice"], appr["run_id"]) == "pending_approval"
    assert _run_status(c, k["alice"], ask["run_id"]) == "waiting_for_input"


def test_cross_workspace_reject_is_denied_and_wrong_ids_are_404(local_env: Any) -> None:
    c, k = local_env
    appr = _start(c, k["alice"], APPR)
    aid = appr["pending_approval"]["approval_id"]
    real = c.post(
        f"/api/v1/runs/{appr['run_id']}/decide",
        headers=k["bob"],
        json={"approval_id": aid, "decision": "reject"},
    )
    fake = c.post(
        "/api/v1/runs/run-doesnotexist/decide",
        headers=k["bob"],
        json={"approval_id": aid, "decision": "reject"},
    )
    # Local owner: one person owns every local workspace, so the foreign run is denied and
    # named (403) while an unknown id is 404. The shared profile's 404-for-foreign rule
    # (n8n CVE-2026-110841 class existence oracle) is enforced by the closed edition.
    assert real.status_code == 403, real.text
    assert real.json()["detail"]["code"] == "CROSS_WORKSPACE_ACCESS_DENIED"
    assert "'wsA'" in real.json()["detail"]["message"]
    assert fake.status_code == 404
    assert fake.json()["detail"]["code"] == "RUN_NOT_FOUND"
    # the reject was not applied
    assert _run_status(c, k["alice"], appr["run_id"]) == "pending_approval"


def test_same_workspace_admin_may_decide_by_design(local_env: Any) -> None:
    """Control: an admin of the same workspace can approve (by design, roles permit it)."""
    c, k = local_env
    appr = _start(c, k["alice"], APPR)
    aid = appr["pending_approval"]["approval_id"]
    r = c.post(
        f"/api/v1/runs/{appr['run_id']}/decide",
        headers=k["alice"],
        json={"approval_id": aid, "decision": "approve"},
    )
    assert r.status_code == 200, r.text


def test_decide_binds_approval_id_to_run(local_env: Any) -> None:
    """An approval id from run X must not approve run Y (same workspace)."""
    c, k = local_env
    a1 = _start(c, k["alice"], APPR)
    a2 = _start(c, k["alice"], APPR)
    id1 = a1["pending_approval"]["approval_id"]
    r = c.post(
        f"/api/v1/runs/{a2['run_id']}/decide",
        headers=k["alice"],
        json={"approval_id": id1, "decision": "approve"},
    )
    assert r.status_code == 404, r.text
    assert r.json()["detail"]["code"] == "APPROVAL_NOT_FOUND"
    assert _run_status(c, k["alice"], a2["run_id"]) == "pending_approval"


def test_approval_cannot_be_replayed_after_decision(local_env: Any) -> None:
    c, k = local_env
    a = _start(c, k["alice"], APPR)
    aid = a["pending_approval"]["approval_id"]
    body = {"approval_id": aid, "decision": "approve"}
    assert (
        c.post(f"/api/v1/runs/{a['run_id']}/decide", headers=k["alice"], json=body).status_code
        == 200
    )
    again = c.post(f"/api/v1/runs/{a['run_id']}/decide", headers=k["alice"], json=body)
    assert again.status_code == 409, again.text


def test_fork_state_update_cannot_smuggle_foreign_credential(local_env: Any) -> None:
    c, k = local_env
    repo: WorkspaceRepository = c.app.state.workspace_repo
    repo.record_resource_ownership("credential", "bobs-cred", "wsB", "bob")
    ask = _start(c, k["alice"], ASK)
    r = c.post(
        f"/api/v1/runs/{ask['run_id']}/fork",
        headers=k["alice"],
        json={"checkpoint_index": 0, "state_update": {"credential_ref": "bobs-cred"}},
    )
    assert r.status_code == 403, r.text
    pend = ask["pending_input"]["interrupt_id"]
    r = c.post(
        f"/api/v1/runs/{ask['run_id']}/input",
        headers=k["alice"],
        json={"interrupt_id": pend, "value": {"credential_id": "bobs-cred"}},
    )
    assert r.status_code == 403, r.text
