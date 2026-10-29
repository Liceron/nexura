# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""MVP Postgres WP4: service wiring of the Postgres chat session, agent and skill stores.

* With ``NEXURA_DATABASE_URL`` (or ``database_url=``) the service keeps chat sessions, saved
  agents and skills in Postgres (``PostgresSessionStore``, ``PostgresAgentStore``,
  ``PostgresSkillStore``) on the app's shared database; none of their tables reaches the SQLite
  file. Without it, SQLite exactly as before.
* Two service instances on one database (each with its own SQLite file, as two Cloud Run
  instances): a chat session, an agent and a skill written through one are read and changed
  through the other; a stale agent edit is refused; two people creating an agent of the same
  name at the same moment, one through each instance, get one 201 and one 409.

Postgres tests skip without ``NEXURA_TEST_POSTGRES_URI``. The chat uses the server's offline
fixture model; nothing calls a model or the network beyond the test database.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import ExitStack, closing
from pathlib import Path
from typing import Any

import pytest
from _local_env import local_headers
from fastapi.testclient import TestClient

from nexura_runtime.agents_store import AgentStore
from nexura_runtime.service.api.app import create_api_app
from nexura_runtime.skills_store import SkillStore

API = "/api/v1"
WP4_TABLES = {
    "nexura_sessions",
    "nexura_session_tombstones",
    "nexura_service_agents",
    "nexura_service_skills",
    "nexura_service_skill_counters",
}
SKILL = "---\nname: refunds\ndescription: Refund rules.\n---\n{body}\n"


def _sqlite_tables(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with closing(sqlite3.connect(path)) as conn:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


@pytest.fixture
def partial(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXURA_ALLOW_PARTIAL_POSTGRES", "1")
    for name in ("K_SERVICE", "CLOUD_RUN_JOB", "NEXURA_WORKFLOW_STATE_URL"):
        monkeypatch.delenv(name, raising=False)


def _exercise(client: TestClient, headers: dict[str, str]) -> dict[str, Any]:
    """One chat session with a turn, one agent and one skill, through the API."""
    session = client.post(f"{API}/sessions", headers=headers, json={"title": "wp4"})
    assert session.status_code == 201, session.text
    session_id = session.json()["session_id"]
    chat = client.post(
        f"{API}/sessions/{session_id}/chat", headers=headers, json={"message": "Hello there!"}
    )
    assert chat.status_code == 200, chat.text
    agent = client.post(
        f"{API}/agents", headers=headers, json={"name": "helper", "instructions": "Help."}
    )
    assert agent.status_code == 201, agent.text
    skill = client.post(
        f"{API}/skills", headers=headers, json={"markdown": SKILL.format(body="Refund in 30 days.")}
    )
    assert skill.status_code == 201, skill.text
    return {"session_id": session_id, "agent_id": agent.json()["agent_id"]}


@pytest.mark.parametrize("source", ["argument", "environment"])
def test_postgres_mode_keeps_sessions_agents_and_skills_in_postgres(
    service_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    partial: None,
    source: str,
) -> None:
    from nexura_postgres import PostgresAgentStore, PostgresSessionStore, PostgresSkillStore

    db = tmp_path / "svc.sqlite3"
    if source == "environment":
        monkeypatch.setenv("NEXURA_DATABASE_URL", service_database_url)
        app = create_api_app(db_path=db, profile="local-owner")
    else:
        monkeypatch.delenv("NEXURA_DATABASE_URL", raising=False)
        app = create_api_app(db_path=db, database_url=service_database_url, profile="local-owner")
    stores = app.state.service_stores
    assert isinstance(app.state.session_store, PostgresSessionStore)
    assert isinstance(app.state.agent_store, PostgresAgentStore)
    assert isinstance(app.state.skills_store, PostgresSkillStore)
    # Every store runs on the app's one shared database (one pool per instance).
    for store in (app.state.session_store, app.state.agent_store, app.state.skills_store):
        assert store.database is stores.database
    assert app.state.agent_runtime.store is app.state.agent_store
    with TestClient(app) as client:
        made = _exercise(client, local_headers("default", "admin"))
        transcript = client.get(f"{API}/sessions/{made['session_id']}")
        assert transcript.status_code == 200 and len(transcript.json()["messages"]) >= 2
    assert not (_sqlite_tables(db) & WP4_TABLES)  # nothing of WP4's reached the SQLite file


def test_sqlite_default_is_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from nexura_agents.session import SQLiteSessionStore

    monkeypatch.delenv("NEXURA_DATABASE_URL", raising=False)
    db = tmp_path / "svc.sqlite3"
    app = create_api_app(db_path=db, profile="local-owner")
    assert type(app.state.session_store) is SQLiteSessionStore
    assert type(app.state.agent_store) is AgentStore
    assert type(app.state.skills_store) is SkillStore
    with TestClient(app) as client:
        _exercise(client, local_headers("default", "admin"))
    assert _sqlite_tables(db) >= WP4_TABLES


@pytest.fixture
def two_instances(
    service_database_url: str, tmp_path: Path, partial: None
) -> Iterator[tuple[TestClient, TestClient]]:
    def make(name: str) -> Any:
        return create_api_app(
            db_path=tmp_path / name / "svc.sqlite3",  # each instance has its own file
            database_url=service_database_url,
            blob_url=(tmp_path / "blobs").as_uri(),
            profile="local-owner",
        )

    with ExitStack() as stack:
        a = stack.enter_context(TestClient(make("a")))
        b = stack.enter_context(TestClient(make("b")))
        yield a, b
    for name in ("a", "b"):
        assert not (_sqlite_tables(tmp_path / name / "svc.sqlite3") & WP4_TABLES)


def test_two_instances_share_sessions_agents_and_skills(
    two_instances: tuple[TestClient, TestClient],
) -> None:
    a, b = two_instances
    h = local_headers("default", "admin")
    made = _exercise(a, h)

    # The chat session continues on the other instance, from the transcript A saved.
    seen = b.get(f"{API}/sessions/{made['session_id']}", headers=h)
    assert seen.status_code == 200, seen.text
    before = len(seen.json()["messages"])
    turn = b.post(
        f"{API}/sessions/{made['session_id']}/chat", headers=h, json={"message": "calculate 2 * 3"}
    )
    assert turn.status_code == 200, turn.text
    after = a.get(f"{API}/sessions/{made['session_id']}", headers=h).json()
    assert len(after["messages"]) > before
    assert made["session_id"] in [
        s["session_id"] for s in b.get(f"{API}/sessions", headers=h).json()["items"]
    ]

    # Agents: B edits what A created; A's stale edit is refused.
    agent_id = made["agent_id"]
    listed = b.get(f"{API}/agents", headers=h).json()
    assert [x["agent_id"] for x in listed["items"]] == [agent_id], listed
    edited = b.put(
        f"{API}/agents/{agent_id}",
        headers=h,
        json={"name": "helper", "instructions": "Help more.", "expected_revision": 1},
    )
    assert edited.status_code == 200, edited.text
    stale = a.put(
        f"{API}/agents/{agent_id}",
        headers=h,
        json={"name": "helper", "instructions": "Stale.", "expected_revision": 1},
    )
    assert stale.status_code == 409, stale.text
    assert "REVISION_CONFLICT" in stale.text
    assert a.get(f"{API}/agents/{agent_id}", headers=h).json()["revision"] == 2

    # Skills: B publishes version 2 of A's skill; A reads it.
    v2 = b.post(f"{API}/skills", headers=h, json={"markdown": SKILL.format(body="Refund in 14.")})
    assert v2.status_code == 201 and v2.json()["version"] == 2, v2.text
    got = a.get(f"{API}/skills/refunds", headers=h).json()
    assert got["version"] == 2 and "14" in got["instructions"]

    # A session deleted on B is gone on A.
    assert b.delete(f"{API}/sessions/{made['session_id']}", headers=h).status_code == 200
    assert a.get(f"{API}/sessions/{made['session_id']}", headers=h).status_code == 404


def _race(workers: list[Callable[[], Any]]) -> list[Any]:
    barrier = threading.Barrier(len(workers))
    results: list[Any] = [None] * len(workers)

    def run(index: int, work: Callable[[], Any]) -> None:
        barrier.wait()
        results[index] = work()

    threads = [threading.Thread(target=run, args=(i, w)) for i, w in enumerate(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    return results


def test_same_agent_name_created_on_two_instances_at_once(
    two_instances: tuple[TestClient, TestClient],
) -> None:
    a, b = two_instances
    h = local_headers("default", "admin")
    for round_ in range(3):
        body = {"name": f"twin{round_}", "instructions": "Help."}
        responses = _race(
            [lambda c=c, j=body: c.post(f"{API}/agents", headers=h, json=j) for c in (a, b, a, b)]
        )
        codes = sorted(r.status_code for r in responses)
        assert codes == [201, 409, 409, 409], [r.text for r in responses]
        assert all("AGENT_NAME_TAKEN" in r.text for r in responses if r.status_code == 409)
    names = [x["name"] for x in a.get(f"{API}/agents", headers=h).json()["items"]]
    assert names == ["twin0", "twin1", "twin2"]
