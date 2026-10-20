"""Security regressions for sessions, knowledge and evaluations routes (group C)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from _local_env import local_headers
from fastapi.testclient import TestClient

from nexura_runtime.evaluation.graders import check_regex_safety
from nexura_runtime.knowledge.ingest.pipeline import IngestionPipeline
from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.service.api.sessions import calculate
from nexura_runtime.service.workspaces.repository import WorkspaceRepository

API = "/api/v1"


def _local(tmp_path: Path, registry: Any = None):
    repo = WorkspaceRepository()
    repo.create_workspace("wsA", "A", owner_id="local-owner")
    repo.create_workspace("wsB", "B", owner_id="local-owner")
    keys: dict[str, dict[str, str]] = {}
    for name, ws, _pid, role in [
        ("a", "wsA", "alice", "admin"),
        ("b", "wsB", "bob", "admin"),
        ("viewer", "wsA", "vic", "viewer"),
        ("editor", "wsA", "eve", "editor"),
    ]:
        keys[name] = local_headers(ws, role)
    app = create_api_app(
        db_path=tmp_path / "s.db",
        registry=registry,
        workspace_repo=repo,
        profile="local-owner",
    )
    return TestClient(app), keys


# Finding 4: session ownership hijack ------------------------------------------------
def test_session_create_conflicts_when_owned_elsewhere(tmp_path: Path) -> None:
    c, k = _local(tmp_path)
    assert c.post(f"{API}/sessions", json={"session_id": "s1"}, headers=k["a"]).status_code == 201
    c.post(f"{API}/sessions/s1/chat", json={"message": "secret plan"}, headers=k["a"])
    r = c.post(f"{API}/sessions", json={"session_id": "s1"}, headers=k["b"])
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "SESSION_ID_CONFLICT"
    assert "secret plan" not in r.text
    # Local owner: a direct read from the other workspace is denied and names the owner.
    foreign = c.get(f"{API}/sessions/s1", headers=k["b"])
    assert foreign.status_code == 403
    assert foreign.json()["detail"]["code"] == "CROSS_WORKSPACE_ACCESS_DENIED"
    assert "'wsA'" in foreign.json()["detail"]["message"]
    assert "secret plan" not in foreign.text
    assert c.get(f"{API}/sessions/never-created", headers=k["b"]).status_code == 404
    assert c.get(f"{API}/sessions/s1", headers=k["a"]).status_code == 200
    # same-workspace re-create stays idempotent
    assert c.post(f"{API}/sessions", json={"session_id": "s1"}, headers=k["a"]).status_code == 201


# Finding 14: knowledge dedup per workspace ------------------------------------------
def test_knowledge_dedup_is_per_workspace(tmp_path: Path) -> None:
    c, k = _local(tmp_path)
    body = {"filename": "p.txt", "content": "launch plan alpha 12345", "metadata": {"n": "A"}}
    ra = c.post(f"{API}/knowledge/documents/upload", headers=k["a"], json=body)
    rb = c.post(
        f"{API}/knowledge/documents/upload",
        headers=k["b"],
        json={**body, "filename": "b.txt", "metadata": None},
    )
    assert ra.status_code == rb.status_code == 201
    assert ra.json()["document_id"] != rb.json()["document_id"]
    assert rb.json()["filename"] == "b.txt"
    assert (
        c.get(f"{API}/knowledge/documents/{ra.json()['document_id']}", headers=k["a"]).status_code
        == 200
    )
    # Local owner: another local workspace's document is denied, naming its owner.
    foreign_doc = c.get(f"{API}/knowledge/documents/{ra.json()['document_id']}", headers=k["b"])
    assert foreign_doc.status_code == 403
    assert foreign_doc.json()["detail"]["code"] == "CROSS_WORKSPACE_ACCESS_DENIED"
    assert "chunks" not in foreign_doc.json() and "content" not in foreign_doc.json()
    # same workspace still dedups
    ra2 = c.post(f"{API}/knowledge/documents/upload", headers=k["a"], json=body)
    assert ra2.json()["document_id"] == ra.json()["document_id"]


def test_pipeline_hash_index_scoped_by_workspace() -> None:
    p = IngestionPipeline()
    d1 = p.ingest_bytes(b"same", filename="a.txt", mime_type="text/plain", workspace_id="w1")
    d2 = p.ingest_bytes(b"same", filename="b.txt", mime_type="text/plain", workspace_id="w2")
    assert d1.document_id != d2.document_id
    assert p.find_by_hash("default", d1.content_hash, "w1") is d1
    assert p.find_by_hash("default", d1.content_hash, "w3") is None


# Findings 28/31: collections per workspace ------------------------------------------
def test_collections_are_workspace_scoped_and_viewer_header_is_ignored(tmp_path: Path) -> None:
    c, k = _local(tmp_path)
    r = c.post(
        f"{API}/knowledge/collections",
        headers=k["a"],
        json={"collection_id": "acme-merger", "name": "n"},
    )
    assert r.status_code == 201
    assert (
        "acme-merger" in c.get(f"{API}/knowledge/collections", headers=k["a"]).json()["collections"]
    )
    assert (
        "acme-merger"
        not in c.get(f"{API}/knowledge/collections", headers=k["b"]).json()["collections"]
    )
    assert (
        c.post(
            f"{API}/knowledge/collections",
            headers=k["viewer"],
            json={"collection_id": "x", "name": "n"},
        ).status_code
        == 201
    )


# Finding 21: calculate tool ----------------------------------------------------------
def test_calculate_is_bounded_and_correct() -> None:
    assert calculate("2 + 3 * 4") == "14"
    assert calculate("2**10") == "1024"
    assert calculate("-(1+2)/4") == "-0.75"
    for bad in ("9**9**7", "9**9**9**9", "2**100000", "9" * 100, "(" * 150 + "1" + ")" * 150):
        t = time.perf_counter()
        out = calculate(bad)
        assert time.perf_counter() - t < 0.5
        assert out.startswith(("Error", "Calculation error")), out
    assert calculate("__import__('os')").startswith("Error")


def test_chat_calculate_bomb_is_fast(tmp_path: Path) -> None:
    c, k = _local(tmp_path)
    c.post(f"{API}/sessions", json={"session_id": "bomb"}, headers=k["a"])
    t = time.perf_counter()
    r = c.post(f"{API}/sessions/bomb/chat", json={"message": "calculate 9**9**7"}, headers=k["a"])
    assert r.status_code == 200, r.text
    assert time.perf_counter() - t < 2.0


# Finding 35: regex grader ------------------------------------------------------------
@pytest.mark.parametrize("pat", ["(a+)+$", "(a*)*", "(a|a)*b", "(.*)*x", r"(a)\1", "x" * 300])
def test_unsafe_regex_rejected(pat: str) -> None:
    assert check_regex_safety(pat) is not None


@pytest.mark.parametrize("pat", [r"^\d+\s+\w+$", r"^30 days", r"(\d{1,3}\.){3}\d+", "(a|b)+"])
def test_ordinary_regex_allowed(pat: str) -> None:
    assert check_regex_safety(pat) is None


def test_redos_dataset_run_is_fast(tmp_path: Path) -> None:
    c, k = _local(tmp_path)
    ex: dict[str, Any] = {
        "input_data": {"query": "x"},
        "expected_output": "a" * 25 + "!",
        "metadata": {"regex_pattern": "(a+)+$"},
    }
    assert c.post(
        f"{API}/evaluations/datasets",
        headers=k["a"],
        json={"dataset_id": "evil", "examples": [ex]},
    ).status_code in (200, 201)
    t = time.perf_counter()
    r = c.post(
        f"{API}/evaluations/run", headers=k["a"], json={"dataset_id": "evil", "graders": ["regex"]}
    )
    assert r.status_code == 200, r.text
    assert time.perf_counter() - t < 1.0


# Finding 27: target workflow workspace ----------------------------------------------
def test_eval_target_workflow_cross_workspace_404(tmp_path: Path) -> None:
    from nexura_runtime.workflow import ModuleRegistry

    reg = ModuleRegistry()
    reg.register("finish", lambda state: {"done": True})
    c, k = _local(tmp_path, reg)
    spec = {
        "name": "Secret",
        "entry_node": "a",
        "nodes": {"a": {"id": "a", "handler": "finish"}},
        "terminal_nodes": ["a"],
    }
    created = c.post(f"{API}/workflows", headers=k["b"], json={"workflow_id": "wf-b", "spec": spec})
    assert created.status_code == 201, created.text
    ds = {"dataset_id": "d1", "examples": [{"input_data": {"query": "q"}, "expected_output": "o"}]}
    assert c.post(f"{API}/evaluations/datasets", headers=k["a"], json=ds).status_code in (200, 201)
    r = c.post(
        f"{API}/evaluations/run",
        headers=k["a"],
        json={"dataset_id": "d1", "target_workflow_id": "wf-b"},
    )
    assert r.status_code == 404
    assert "spec_hash" not in r.text


# Finding 6: RBAC ---------------------------------------------------------------------


def test_local_owner_mode_still_works(tmp_path: Path) -> None:
    app = create_api_app(db_path=tmp_path / "s.db", profile="local-owner")
    c = TestClient(app)
    assert c.post(f"{API}/sessions", json={"session_id": "l1"}).status_code == 201
    assert c.post(f"{API}/sessions/l1/chat", json={"message": "hello"}).status_code == 200
    assert (
        c.post(
            f"{API}/knowledge/documents/upload", json={"filename": "a.txt", "content": "hello"}
        ).status_code
        == 201
    )
