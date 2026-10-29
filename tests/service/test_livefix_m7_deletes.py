"""Live pass M7 (open-source deletes), minor 3 (credential check wording) and minor 18 (trace
redaction of state key names). Local fixtures only; both service backends
(``--service-backend=sqlite|postgres``)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from nexura_runtime.observability.run_diagnostics import is_secret_key, redact_run_content
from nexura_runtime.service.api.app import create_api_app

API = "/api/v1"


@pytest.fixture
def client(tmp_path: Path) -> Any:
    with TestClient(create_api_app(db_path=tmp_path / "s.db", profile="local-owner")) as c:
        yield c


def _upload(c: TestClient, collection: str, name: str, text: str) -> str:
    r = c.post(
        f"{API}/knowledge/documents/upload",
        json={"collection_id": collection, "filename": name, "content": text},
    )
    assert r.status_code in (200, 201), r.text
    return str(r.json()["document_id"])


# ------------------------------------------------------------------ knowledge collections


def test_delete_collection_removes_documents_index_and_collection(client: TestClient) -> None:
    c = client
    assert (
        c.post(
            f"{API}/knowledge/collections", json={"collection_id": "qa-kb", "name": "QA"}
        ).status_code
        == 201
    )
    doc = _upload(c, "qa-kb", "a.txt", "Refunds are processed within fourteen days.")
    keep = _upload(c, "other", "b.txt", "Refunds in the other collection stay.")
    hits = c.post(f"{API}/knowledge/retrieve", json={"query": "refunds", "collection_id": "qa-kb"})
    assert hits.status_code == 200 and hits.json()["citations"], hits.text

    r = c.delete(f"{API}/knowledge/collections/qa-kb")
    assert r.status_code == 200, r.text
    assert r.json() == {"deleted": True, "collection_id": "qa-kb", "documents_deleted": 1}
    assert "qa-kb" not in c.get(f"{API}/knowledge/collections").json()["collections"]
    assert c.get(f"{API}/knowledge/documents/{doc}").status_code == 404
    gone = c.post(f"{API}/knowledge/retrieve", json={"query": "refunds", "collection_id": "qa-kb"})
    assert gone.status_code == 200 and gone.json()["citations"] == []
    # Other collections are untouched.
    assert c.get(f"{API}/knowledge/documents/{keep}").status_code == 200
    assert c.delete(f"{API}/knowledge/collections/qa-kb").status_code == 404
    # The default collection always exists: deleting it empties it.
    _upload(c, "default", "d.txt", "Default document.")
    emptied = c.delete(f"{API}/knowledge/collections/default")
    assert emptied.status_code == 200 and emptied.json()["documents_deleted"] == 1
    assert "default" in c.get(f"{API}/knowledge/collections").json()["collections"]
    # The id can be used again.
    assert (
        c.post(
            f"{API}/knowledge/collections", json={"collection_id": "qa-kb", "name": "QA"}
        ).status_code
        == 201
    )
    assert (
        c.get(f"{API}/knowledge/documents", params={"collection_id": "qa-kb"}).json()["items"] == []
    )


# ------------------------------------------------------------------ benchmark evaluation runs


def test_delete_benchmark_eval_run(client: TestClient) -> None:
    c = client
    datasets = c.get(f"{API}/evaluations/datasets").json()
    dataset_id = (datasets.get("datasets") or datasets.get("items"))[0]["dataset_id"]
    run = c.post(f"{API}/evaluations/run", json={"dataset_id": dataset_id})
    assert run.status_code == 200, run.text
    eval_id = run.json()["run_id"]
    assert c.get(f"{API}/evaluations/runs/{eval_id}").status_code == 200
    r = c.delete(f"{API}/evaluations/runs/{eval_id}")
    assert r.status_code == 200 and r.json() == {"deleted": True, "run_id": eval_id}
    assert c.get(f"{API}/evaluations/runs/{eval_id}").status_code == 404
    assert eval_id not in [x["run_id"] for x in c.get(f"{API}/evaluations/runs").json()["runs"]]
    assert c.delete(f"{API}/evaluations/runs/{eval_id}").status_code == 404


# ------------------------------------------------------------------ "Add to palette" tools


def test_delete_openapi_palette_registration(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    with TestClient(create_api_app(db_path=db, profile="local-owner")) as c:
        reg = c.post(
            f"{API}/integrations/register",
            json={
                "kind": "openapi",
                "fixture_id": "petstore_fixture",
                "server_name_or_prefix": "qa_petstore",
            },
        )
        assert reg.status_code == 200, reg.text
        ids = reg.json()["registered_modules"]
        assert ids
        listed = c.get(f"{API}/integrations/registrations").json()["registrations"]
        assert sorted(r["module_id"] for r in listed) == sorted(ids)
        palette = {m["module_id"] for m in c.get(f"{API}/modules").json()["items"]}
        assert set(ids) <= palette
        bad = c.delete(f"{API}/integrations/registrations")
        assert bad.status_code == 400
        one = c.delete(f"{API}/integrations/registrations", params={"module_id": ids[0]})
        assert one.status_code == 200 and one.json()["module_ids"] == [ids[0]]
        rest = c.delete(
            f"{API}/integrations/registrations", params={"server_name_or_prefix": "qa_petstore"}
        )
        assert rest.status_code == 200 and sorted(rest.json()["module_ids"]) == sorted(ids[1:])
        assert c.get(f"{API}/integrations/registrations").json()["registrations"] == []
        palette = {m["module_id"] for m in c.get(f"{API}/modules").json()["items"]}
        assert not set(ids) & palette
        missing = c.delete(
            f"{API}/integrations/registrations", params={"server_name_or_prefix": "qa_petstore"}
        )
        assert missing.status_code == 404
    # Not restored when the service starts again.
    with TestClient(create_api_app(db_path=db, profile="local-owner")) as c:
        assert c.get(f"{API}/integrations/registrations").json()["registrations"] == []
        palette = {m["module_id"] for m in c.get(f"{API}/modules").json()["items"]}
        assert not set(ids) & palette


# ------------------------------------------------------------------ minor 3 and 18


def test_credential_check_does_not_claim_the_key_is_valid(client: TestClient) -> None:
    c = client
    made = c.post(
        f"{API}/credentials",
        json={
            "credential_id": "fake-key",
            "provider": "gemini",
            "credential_type": "api_key",
            "secret": "definitely-not-a-real-key",
        },
    )
    assert made.status_code in (200, 201), made.text
    body = c.post(f"{API}/credentials/fake-key/verify").json()
    assert body["status"] == "stored" and body["checked_with_provider"] is False
    assert "not checked with the provider" in body["message"].lower()
    assert "valid" not in body["status"]


def test_state_key_names_are_not_redacted_but_secrets_are() -> None:
    config = {"agent_id": "agt_1", "input_key": "input", "output_key": "output"}
    assert redact_run_content(config) == config
    for name in ("input_key", "output_key", "reply_key", "step_key", "inputKey"):
        assert not is_secret_key(name), name
    for name in ("api_key", "signing_key", "secret_key", "access_token", "password"):
        assert is_secret_key(name), name
    assert redact_run_content({"api_key": "x", "input_key": "q"}) == {
        "api_key": "[REDACTED]",
        "input_key": "q",
    }


# Review repro (m1): moved from .autopilot-livefix/repros/test_review_registrations.py.
def test_a_tool_registered_twice_leaves_the_palette_when_deleted(tmp_path: Path) -> None:
    """Registering the same tools again keeps the first module object in the versioned
    registry and puts a new one in the handler registry; the delete unregistered the
    versioned entry only when it was that new object, so the tool stayed in the palette
    (/modules) and runnable after a 200 "deleted"."""
    app = create_api_app(db_path=tmp_path / "s.db", profile="local-owner")
    with TestClient(app) as c:
        body = {
            "kind": "openapi",
            "fixture_id": "petstore_fixture",
            "server_name_or_prefix": "review_repeat",
        }
        first = c.post(f"{API}/integrations/register", json=body)
        assert first.status_code == 200, first.text
        again = c.post(f"{API}/integrations/register", json=body)
        assert again.status_code == 200, again.text
        ids = set(first.json()["registered_modules"])
        assert ids
        gone = c.delete(
            f"{API}/integrations/registrations", params={"server_name_or_prefix": "review_repeat"}
        )
        assert gone.status_code == 200, gone.text
        palette = {m["module_id"] for m in c.get(f"{API}/modules").json()["items"]}
        assert not ids & palette, f"deleted tools still offered in the palette: {ids & palette}"
        registry = app.state.registry
        for mod_id in ids:
            assert not registry.has_module(mod_id) and not registry.has_handler(mod_id), mod_id
