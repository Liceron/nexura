"""Second-campaign regressions: sessions/chat RBAC, calculator, regex ReDoS, tenant 404s,
integration tool id collisions."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from _local_env import local_headers
from fastapi.testclient import TestClient
from nexura_agents.evals import check_regex_safety

from nexura_runtime.evaluation.graders import RegexGrader
from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.api.sessions import calculate
from nexura_runtime.service.workspaces.repository import WorkspaceRepository
from nexura_runtime.workflow import ModuleRegistry

sys.path.insert(0, str(Path(__file__).parent))

API = "/api/v1"


def _local(tmp_path: Path, registry: ModuleRegistry | None = None):
    repo = WorkspaceRepository()
    keys = {}
    for ws, owner in [("wsA", "alice"), ("wsB", "bob")]:
        repo.create_workspace(ws, ws, owner_id=owner)
    for name, ws, _pid, role in [
        ("a", "wsA", "alice", "admin"),
        ("viewer", "wsA", "vic", "viewer"),
        ("b", "wsB", "bob", "editor"),
    ]:
        keys[name] = local_headers(ws, role)
    kwargs = {"registry": registry} if registry is not None else {}
    app = create_api_app(
        db_path=tmp_path / "s.db",
        workspace_repo=repo,
        profile="local-owner",
        **kwargs,
    )
    return TestClient(app), keys


@pytest.mark.parametrize("expr", ["(-8)**0.5//1", "(-8)**0.5", "(-8)**0.5**2", "((-8)**0.5)**2"])
def test_calculate_rejects_complex(expr: str) -> None:
    out = calculate(expr)
    assert out.startswith("Calculation error")


def test_calculate_still_works() -> None:
    assert calculate("2**10") == "1024"
    assert calculate("(-8)**2") == "64"
    assert calculate("2**-1") == "0.5"


def test_chat_calculate_complex_no_500(tmp_path: Path) -> None:
    c, k = _local(tmp_path)
    c.post(f"{API}/sessions", json={"session_id": "cx"}, headers=k["a"])
    r = c.post(
        f"{API}/sessions/cx/chat", json={"message": "calculate (-8)**0.5//1"}, headers=k["a"]
    )
    assert r.status_code == 200


@pytest.mark.parametrize(
    "pat",
    [".*.*.*x", ".*.*x", "a*a*a*b", "(a{0,31})(a{0,31})b", r"\w+\d+x", "[a-z]*[a-c]*$"]
    + ["a{0,31}" * 8 + "b"],
)
def test_regex_guard_rejects_overlapping_repeats(pat: str) -> None:
    assert check_regex_safety(pat) is not None


@pytest.mark.parametrize("pat", [r"\d{3}-\d{4}", r"^\w+@\w+\.com$", r"a*b*", r"[a-z]+\d+", "hello"])
def test_regex_guard_allows_ordinary(pat: str) -> None:
    assert check_regex_safety(pat) is None


def test_regex_grader_fails_closed_fast() -> None:
    import asyncio

    from nexura_runtime.evaluation.models import DatasetExample

    ex = DatasetExample(
        example_id="e",
        input_data={"query": "x"},
        expected_output="a" * 2048,
        metadata={"regex_pattern": ".*.*.*x"},
    )
    t0 = time.perf_counter()
    res = asyncio.run(RegexGrader().grade(ex, "a" * 2048))
    assert time.perf_counter() - t0 < 2
    assert res.passed is False and "rejected" in (res.reason or "").lower()


def test_cross_workspace_session_and_document_denied(tmp_path: Path) -> None:
    c, k = _local(tmp_path)
    assert c.post(f"{API}/sessions", json={"session_id": "s1"}, headers=k["a"]).status_code == 201
    up = c.post(
        f"{API}/knowledge/documents/upload",
        headers=k["a"],
        json={"filename": "a.txt", "content": "secret"},
    )
    doc = up.json()["document_id"]
    miss_s = c.get(f"{API}/sessions/nope", headers=k["b"])
    # Local owner: another workspace's session is denied (403, owner named); the
    # session itself is not returned, deleted or chatted into.
    for r in (
        c.get(f"{API}/sessions/s1", headers=k["b"]),
        c.delete(f"{API}/sessions/s1", headers=k["b"]),
        c.post(f"{API}/sessions/s1/chat", json={"message": "hi"}, headers=k["b"]),
    ):
        assert r.status_code == 403, r.text
        assert r.json()["detail"]["code"] == "CROSS_WORKSPACE_ACCESS_DENIED"
        assert "'wsA'" in r.json()["detail"]["message"]
        assert "messages" not in r.json()
    assert miss_s.status_code == 404
    own = c.get(f"{API}/sessions/s1", headers=k["a"])
    assert own.status_code == 200
    assert own.json()["messages"] == []
    # Documents: an unknown id is 404; another local workspace's document is denied (local
    # owner: 403 naming the owner), and the delete leaves it in place.
    assert c.get(f"{API}/knowledge/documents/doc_nope", headers=k["b"]).status_code == 404
    for r in (
        c.get(f"{API}/knowledge/documents/{doc}", headers=k["b"]),
        c.delete(f"{API}/knowledge/documents/{doc}", headers=k["b"]),
    ):
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "CROSS_WORKSPACE_ACCESS_DENIED"
    assert c.get(f"{API}/knowledge/documents/{doc}", headers=k["a"]).status_code == 200


def test_integration_register_cannot_replace_operator_handler(tmp_path: Path) -> None:
    def operator_handler(state):  # type: ignore[no-untyped-def]
        return {"ok": True}

    reg = ModuleRegistry()
    reg.register("mcp.ops.math_add", operator_handler)
    c, k = _local(tmp_path, reg)
    r = c.post(
        f"{API}/integrations/register",
        headers=k["b"],
        json={"kind": "mcp", "server_name_or_prefix": "ops", "fixture_id": "math_fixture"},
    )
    assert r.status_code == 409, r.text
    assert reg.get("mcp.ops.math_add") is operator_handler
    # nothing partially registered
    assert not reg.has_handler("mcp.ops.math_multiply")


def test_integration_register_cross_workspace_conflict_is_denied(tmp_path: Path) -> None:
    c, k = _local(tmp_path)
    body = {"kind": "mcp", "server_name_or_prefix": "shared", "fixture_id": "math_fixture"}
    assert c.post(f"{API}/integrations/register", headers=k["a"], json=body).status_code == 200
    assert c.post(f"{API}/integrations/register", headers=k["a"], json=body).status_code == 200
    r = c.post(f"{API}/integrations/register", headers=k["b"], json=body)
    # Local owner: the tool id is refused and the owning workspace is named.
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "CROSS_WORKSPACE_DENIED"
    assert "'wsA'" in r.json()["detail"]["message"]
    repo = c.app.state.workspace_repo  # type: ignore[attr-defined]
    owners = {
        repo.get_resource_workspace("integration_tool", tid)
        for tid in ("mcp.shared.math_add", "mcp.shared.math_multiply")
    }
    assert owners == {"wsA"}


def test_session_list_is_most_recently_active_first(tmp_path: Path) -> None:
    """Studio opens items[0] after a reload, so the latest conversation must come first."""
    from nexura_runtime.service.api.app import create_api_app as _create

    client = TestClient(_create(db_path=tmp_path / "order.sqlite3"), base_url="http://127.0.0.1")
    old = client.post("/api/v1/sessions", json={}).json()["session_id"]
    time.sleep(0.02)
    new = client.post("/api/v1/sessions", json={}).json()["session_id"]
    listed = [i["session_id"] for i in client.get("/api/v1/sessions").json()["items"]]
    assert listed[:2] == [new, old]
    time.sleep(0.02)
    turn = client.post(
        f"/api/v1/sessions/{old}/chat",
        json={"message": "calculate 2 + 2", "model": "fake-deterministic"},
    )
    assert turn.status_code == 200
    listed = [i["session_id"] for i in client.get("/api/v1/sessions").json()["items"]]
    assert listed[:2] == [old, new]  # a new turn moves the conversation to the top
