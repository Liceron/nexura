# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Chat session, saved-agent and skill store contracts on both backends (MVP Postgres WP4).

Every parametrised test runs the same assertions against the SQLite stores
(``SQLiteSessionStore``, ``AgentStore``, ``SkillStore``) and the Postgres ones
(``PostgresSessionStore``, ``PostgresAgentStore``, ``PostgresSkillStore``; skipped without
``NEXURA_TEST_POSTGRES_URI``). Each ``open_*`` call is a new instance: on Postgres it has a pool
of its own, as another service instance has, so the concurrency tests are two instances on one
database, not two handles on one pool. The concurrency guarantees:

* session save is compare-and-swap: of the writers that read one revision exactly one wins,
  for a first save too (the model is ``tests/postgres/test_postgres_session_cas.py``);
* the per-workspace agent limit and the duplicate-name rule hold under concurrent creates;
* concurrent skill publishes never lose a version or reuse a number, and the skill limit holds.

Everything is local: no model, no network beyond the test database.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from nexura_agents.errors import SessionConflictError, StorageError
from nexura_agents.session import (
    delete_session,
    fork_session,
    list_sessions,
    pop_last_turn,
)
from nexura_agents.skills import SkillError
from nexura_agents.types import Message, ToolCall

from nexura_runtime.agents_spec import AgentSpec
from nexura_runtime.agents_store import (
    MAX_AGENTS_PER_WORKSPACE,
    AgentLimitError,
    AgentNameTakenError,
    AgentNotFoundError,
    AgentRevisionConflictError,
    AgentStoreError,
)
from nexura_runtime.skills_store import (
    MAX_LABELS_PER_SKILL,
    MAX_SKILLS_PER_WORKSPACE,
    MAX_VERSIONS_PER_SKILL,
    SkillNotFoundError,
)

Opener = Callable[[], Any]


class _Backend:
    """Opens stores of one kind on this test's storage; each call is a new instance."""

    def __init__(self, backend: str, tmp_path: Path, request: pytest.FixtureRequest) -> None:
        self.backend = backend
        self.path = tmp_path / "service.sqlite3"
        self.url = request.getfixturevalue("service_database_url") if backend == "postgres" else ""
        self.databases: list[Any] = []
        self.sessions_opened: list[Any] = []

    def database(self) -> Any:
        from nexura_postgres import Database

        database = Database(self.url)  # a pool of its own, as another instance has
        self.databases.append(database)
        return database

    def sessions(self) -> Any:
        if self.backend == "sqlite":
            from nexura_agents.session import SQLiteSessionStore

            store: Any = SQLiteSessionStore(self.path)
        else:
            from nexura_postgres import PostgresSessionStore

            store = PostgresSessionStore(self.database())
        self.sessions_opened.append(store)
        return store

    def session_instances(self) -> list[Any]:
        """Two Postgres instances (separate pools). On SQLite one store shared by the racing
        threads: the service opens one ``SQLiteSessionStore`` per database file, and its CAS
        relies on that store's lock (two store objects on one file are not a supported setup)."""
        if self.backend == "sqlite":
            return [self.sessions()] * 2
        return [self.sessions(), self.sessions()]

    def agents(self) -> Any:
        if self.backend == "sqlite":
            from nexura_runtime.agents_store import AgentStore

            return AgentStore(self.path)
        from nexura_postgres import PostgresAgentStore

        return PostgresAgentStore(self.database())

    def skills(self) -> Any:
        if self.backend == "sqlite":
            from nexura_runtime.skills_store import SkillStore

            return SkillStore(self.path)
        from nexura_postgres import PostgresSkillStore

        return PostgresSkillStore(self.database())

    def close(self) -> None:
        for store in self.sessions_opened:
            store.close_sync()
        for database in self.databases:
            database.close()


@pytest.fixture
def stores(backend: str, tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[_Backend]:
    opened = _Backend(backend, tmp_path, request)
    try:
        yield opened
    finally:
        opened.close()
    if backend == "postgres":
        assert not opened.path.exists()  # nothing touches SQLite


def _race(workers: list[Callable[[], Any]]) -> list[Any]:
    """Run ``workers`` in threads released together; results or exceptions, in order."""
    barrier = threading.Barrier(len(workers))
    results: list[Any] = [None] * len(workers)

    def run(index: int, work: Callable[[], Any]) -> None:
        barrier.wait()
        try:
            results[index] = work()
        except BaseException as exc:  # collected for the assertions
            results[index] = exc

    threads = [threading.Thread(target=run, args=(i, w)) for i, w in enumerate(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(120)
    return results


def _msgs(*texts: str) -> list[Message]:
    return [Message(role="user", content=text) for text in texts]


# =============================================================================== sessions


async def test_session_save_get_round_trip(stores: _Backend) -> None:
    store = stores.sessions()
    assert await store.get("s1") is None
    messages = [
        Message(role="system", content="Be brief."),
        Message(role="user", content="नमस्ते ☃ \x00 nul"),
        Message(
            role="assistant",
            content=None,
            tool_calls=[
                ToolCall(id="c1", name="calc", arguments={"x": 1}, raw_arguments='{"x":1}')
            ],
        ),
        Message(role="tool", content="1", tool_call_id="c1", name="calc"),
    ]
    saved = await store.save("s1", messages, expected_revision=0, state={"k": [1, "v"]})
    assert saved.revision == 1 and saved.state == {"k": [1, "v"]}
    got = await store.get("s1")
    assert got is not None
    assert got.revision == 1 and got.session_id == "s1"
    assert got.messages == messages
    assert got.state == {"k": [1, "v"]}
    second = await store.save("s1", [*messages, *_msgs("more")], expected_revision=1)
    assert second.revision == 2 and second.state == {}
    got = await store.get("s1")
    assert got is not None and got.revision == 2 and len(got.messages) == 5 and got.state == {}
    # Another instance reads the same session.
    other = stores.sessions()
    seen = await other.get("s1")
    assert seen is not None and seen.revision == 2 and seen.messages == got.messages


async def test_session_cas_conflicts(stores: _Backend) -> None:
    store = stores.sessions()
    await store.save("s", _msgs("a"), expected_revision=0)
    await store.save("s", _msgs("a", "b"), expected_revision=1)
    for expected, actual in ((1, 2), (0, 2), (3, 2), (-1, 2)):
        with pytest.raises(SessionConflictError) as caught:
            await store.save("s", _msgs("lost"), expected_revision=expected)
        assert (caught.value.expected_revision, caught.value.actual_revision) == (expected, actual)
        assert caught.value.session_id == "s"
    with pytest.raises(SessionConflictError) as missing:
        await store.save("nope", _msgs("x"), expected_revision=4)
    assert missing.value.actual_revision == 0
    assert await store.get("nope") is None
    got = await store.get("s")
    assert got is not None and got.revision == 2 and [m.content for m in got.messages] == ["a", "b"]


async def test_session_delete_keeps_a_tombstone(stores: _Backend) -> None:
    store = stores.sessions()
    await store.save("s", _msgs("a"), expected_revision=0)
    await store.save("s", _msgs("a", "b"), expected_revision=1)
    await store.save("s", _msgs("a", "b", "c"), expected_revision=2)
    assert await store.delete("s") is True
    assert await store.get("s") is None
    assert await store.delete("s") is False
    assert await store.delete("never") is False
    # A snapshot taken before the delete (revision 3) can never match the new session.
    with pytest.raises(SessionConflictError):
        await store.save("s", _msgs("stale"), expected_revision=3)
    recreated = await store.save("s", _msgs("new"), expected_revision=0)
    assert recreated.revision == 4  # continues the count
    assert await delete_session(store, "s") is True
    again = await stores.sessions().save("s", [], expected_revision=0)
    assert again.revision == 5


async def test_session_listing_and_updated_at(stores: _Backend) -> None:
    store = stores.sessions()
    for sid in ("b", "a", "B", "a-c", "ab"):
        await store.save(sid, [], expected_revision=0)
    expected = sorted(["b", "a", "B", "a-c", "ab"])  # Python string order, as SQLite
    assert await store.list_sessions() == expected
    assert await list_sessions(store) == expected
    times = await store.updated_at(["a", "zzz", "a", "b", "nul\x00"])
    assert set(times) == {"a", "b"} and all(isinstance(v, float) and v > 0 for v in times.values())
    assert await store.updated_at([]) == {}
    before = times["a"]
    await asyncio.sleep(0.01)
    await store.save("a", _msgs("x"), expected_revision=1)
    assert (await store.updated_at(["a"]))["a"] > before


async def test_session_helpers_fork_and_pop(stores: _Backend) -> None:
    store = stores.sessions()
    messages = [Message(role="system", content="sys"), *_msgs("one"), *_msgs("two")]
    await store.save("src", messages, expected_revision=0, state={"n": 1})
    forked = await fork_session(store, "src", "dst", at=2)
    assert forked.revision == 1 and forked.state == {"n": 1}
    with pytest.raises(SessionConflictError):
        await fork_session(store, "src", "dst")  # a fork never overwrites
    popped = await pop_last_turn(store, "src")
    assert popped is not None and [m.content for m in popped.messages] == ["sys", "one"]
    assert popped.revision == 2 and popped.state == {"n": 1}


async def test_session_store_refuses_calls_after_close(stores: _Backend) -> None:
    store = stores.sessions()
    await store.save("s", [], expected_revision=0)
    await store.close()
    with pytest.raises(StorageError):
        await store.get("s")
    with pytest.raises(StorageError):
        await store.save("s", [], expected_revision=1)


def test_concurrent_first_save_has_one_winner(stores: _Backend) -> None:
    """Two instances create the same session at once: one wins, the other gets the same
    ``SessionConflictError`` a later CAS miss gives (model: test_postgres_session_cas.py)."""
    instances = stores.session_instances()
    for round_ in range(5):
        sid = f"new-{round_}"
        outcomes = _race(
            [
                lambda s=s, i=i, sid=sid: asyncio.run(s.save(sid, _msgs(f"from {i}"), 0))
                for i, s in enumerate(instances * 2)
            ]
        )
        winners = [o for o in outcomes if not isinstance(o, BaseException)]
        losers = [o for o in outcomes if isinstance(o, BaseException)]
        assert len(winners) == 1 and winners[0].revision == 1, outcomes
        assert len(losers) == 3 and all(isinstance(o, SessionConflictError) for o in losers)
        assert all((o.expected_revision, o.actual_revision) == (0, 1) for o in losers)
        stored = asyncio.run(instances[1].get(sid))
        assert stored is not None and stored.messages == winners[0].messages


def test_concurrent_cas_update_has_one_winner(stores: _Backend) -> None:
    instances = stores.session_instances()
    asyncio.run(instances[0].save("s", _msgs("base"), 0))
    for revision in range(1, 6):
        outcomes = _race(
            [
                lambda s=s, i=i, r=revision: asyncio.run(s.save("s", _msgs(f"w{i}"), r))
                for i, s in enumerate(instances * 3)
            ]
        )
        winners = [o for o in outcomes if not isinstance(o, BaseException)]
        assert len(winners) == 1 and winners[0].revision == revision + 1, outcomes
        losers = [o for o in outcomes if isinstance(o, BaseException)]
        assert len(losers) == 5 and all(isinstance(o, SessionConflictError) for o in losers)
        stored = asyncio.run(instances[0].get("s"))
        assert stored is not None and stored.revision == revision + 1
        assert stored.messages == winners[0].messages


def test_read_modify_write_with_retry_loses_no_update(stores: _Backend) -> None:
    """Writers on two instances append through get + CAS save with retry: every append lands
    exactly once and the revision counts every save."""
    instances = stores.session_instances()
    asyncio.run(instances[0].save("log", [], 0))
    per_writer = 6

    def writer(store: Any, name: str) -> int:
        async def go() -> int:
            conflicts = 0
            for n in range(per_writer):
                while True:
                    snap = await store.get("log")
                    try:
                        await store.save(
                            "log", [*snap.messages, *_msgs(f"{name}-{n}")], snap.revision
                        )
                        break
                    except SessionConflictError:
                        conflicts += 1
            return conflicts

        return asyncio.run(go())

    results = _race([lambda s=s, i=i: writer(s, f"w{i}") for i, s in enumerate(instances * 2)])
    assert not any(isinstance(r, BaseException) for r in results), results
    final = asyncio.run(instances[1].get("log"))
    assert final is not None
    contents = [m.content for m in final.messages]
    assert sorted(contents) == sorted(f"w{i}-{n}" for i in range(4) for n in range(per_writer))
    assert final.revision == 1 + 4 * per_writer


def test_delete_and_recreate_race_never_reuses_a_revision(stores: _Backend) -> None:
    """One instance deletes while others re-create: every revision handed out is new."""
    instances = stores.session_instances()
    asyncio.run(instances[0].save("t", [], 0))
    seen: list[int] = [1]
    lock = threading.Lock()

    def deleter() -> None:
        for _ in range(15):
            asyncio.run(instances[0].delete("t"))

    def creator(store: Any) -> None:
        for _ in range(15):
            try:
                snap = asyncio.run(store.save("t", [], 0))
            except SessionConflictError:
                continue
            with lock:
                seen.append(snap.revision)

    results = _race([deleter, lambda: creator(instances[1]), lambda: creator(instances[0])])
    assert not any(isinstance(r, BaseException) for r in results), results
    assert len(seen) == len(set(seen)), sorted(seen)


# ================================================================================= agents


def _agent(name: str, **kw: Any) -> AgentSpec:
    return AgentSpec.model_validate({"name": name, "instructions": "Help.", **kw})


def test_agent_create_get_list(stores: _Backend) -> None:
    store = stores.agents()
    created = store.create("ws", _agent("helper", description="d"), actor="alice")
    assert created["revision"] == 1 and created["agent_id"].startswith("agt_")
    assert created["created_by"] == created["updated_by"] == "alice"
    assert created["spec"]["name"] == "helper" and created["workspace_id"] == "ws"
    assert isinstance(created["created_at"], float)
    again = store.get("ws", created["agent_id"])
    assert again == created
    spec, revision = store.get_spec("ws", created["agent_id"])
    assert spec.name == "helper" and revision == 1
    for name in ("ab", "a-c", "a_b", "a0", "b"):
        store.create("ws", _agent(name))
    names = [a["name"] for a in store.list_agents("ws")]
    assert names == sorted(names, key=lambda n: n.encode())  # byte order, as SQLite
    assert names == ["a-c", "a0", "a_b", "ab", "b", "helper"]
    summary = store.list_agents("ws")[-1]
    assert set(summary) == {
        "agent_id",
        "name",
        "description",
        "model",
        "tool_count",
        "tools",
        "revision",
        "updated_at",
    }
    assert summary["description"] == "d"
    # Another instance sees the same rows; other workspaces see nothing.
    assert stores.agents().list_agents("ws") == store.list_agents("ws")
    assert store.list_agents("other") == []
    with pytest.raises(AgentNotFoundError):
        store.get("other", created["agent_id"])
    with pytest.raises(AgentNotFoundError):
        store.get("ws", "agt_missing")
    with pytest.raises(AgentNotFoundError):
        store.get("ws", "agt_\x00nul")
    assert store.list_agents("ws\x00") == []


def test_agent_names_are_unique_per_workspace(stores: _Backend) -> None:
    store = stores.agents()
    first = store.create("ws", _agent("one"))
    second = store.create("ws", _agent("two"))
    with pytest.raises(AgentNameTakenError) as taken:
        store.create("ws", _agent("one"))
    assert taken.value.code == "AGENT_NAME_TAKEN" and taken.value.status_code == 409
    store.create("ws2", _agent("one"))  # another workspace may use it
    with pytest.raises(AgentNameTakenError):
        store.update("ws", second["agent_id"], _agent("one"), expected_revision=1)
    renamed = store.update(
        "ws", first["agent_id"], _agent("one", description="x"), expected_revision=1
    )
    assert renamed["revision"] == 2
    moved = store.update("ws", first["agent_id"], _agent("uno"), expected_revision=2)
    assert moved["revision"] == 3 and moved["spec"]["name"] == "uno"
    store.create("ws", _agent("one"))  # the old name is free again
    # An id already stored: Postgres raises AgentStoreError; SQLite's store lets the database's
    # IntegrityError through (unchanged; no caller passes agent_id).
    with pytest.raises((AgentStoreError, sqlite3.IntegrityError)):
        store.create("ws", _agent("dup-id"), agent_id=first["agent_id"])
    assert store.get("ws", first["agent_id"])["spec"]["name"] == "uno"


def test_agent_revision_cas(stores: _Backend) -> None:
    store = stores.agents()
    agent_id = store.create("ws", _agent("cas"))["agent_id"]
    same = store.update("ws", agent_id, _agent("cas"), expected_revision=1)
    assert same["revision"] == 1  # same spec: no-op
    updated = store.update(
        "ws", agent_id, _agent("cas", description="v2"), expected_revision=1, actor="bob"
    )
    assert updated["revision"] == 2 and updated["updated_by"] == "bob"
    assert updated["updated_at"] >= updated["created_at"]
    with pytest.raises(AgentRevisionConflictError) as stale:
        store.update("ws", agent_id, _agent("cas", description="v3"), expected_revision=1)
    assert stale.value.current_revision == 2 and stale.value.status_code == 409
    anyway = store.update("ws", agent_id, _agent("cas", description="v3"), expected_revision=None)
    assert anyway["revision"] == 3
    with pytest.raises(AgentRevisionConflictError):
        store.delete("ws", agent_id, expected_revision=2)
    with pytest.raises(AgentNotFoundError):
        store.update("other", agent_id, _agent("cas"), expected_revision=None)
    with pytest.raises(AgentNotFoundError):
        store.delete("other", agent_id)
    store.delete("ws", agent_id, expected_revision=3)
    with pytest.raises(AgentNotFoundError):
        store.get("ws", agent_id)
    with pytest.raises(AgentNotFoundError):
        store.delete("ws", agent_id)


def test_agent_limit(stores: _Backend) -> None:
    store = stores.agents()
    for n in range(MAX_AGENTS_PER_WORKSPACE):
        store.create("ws", _agent(f"a{n}"))
    with pytest.raises(AgentLimitError) as full:
        store.create("ws", _agent("one-more"))
    assert full.value.code == "TOO_MANY_AGENTS" and full.value.status_code == 413
    store.create("ws2", _agent("one-more"))  # the limit is per workspace
    victim = store.list_agents("ws")[0]["agent_id"]
    store.delete("ws", victim)
    store.create("ws", _agent("one-more"))
    assert len(store.list_agents("ws")) == MAX_AGENTS_PER_WORKSPACE


def test_concurrent_creates_respect_the_agent_limit(stores: _Backend) -> None:
    """Two instances, eight concurrent creates into a workspace two below the limit: exactly two
    succeed (a read-then-write count check would let more through)."""
    instances = [stores.agents(), stores.agents()]
    for n in range(MAX_AGENTS_PER_WORKSPACE - 2):
        instances[0].create("ws", _agent(f"base{n}"))
    outcomes = _race(
        [lambda s=s, i=i: s.create("ws", _agent(f"racer{i}")) for i, s in enumerate(instances * 4)]
    )
    created = [o for o in outcomes if isinstance(o, dict)]
    refused = [o for o in outcomes if isinstance(o, BaseException)]
    assert len(created) == 2, outcomes
    assert len(refused) == 6 and all(isinstance(o, AgentLimitError) for o in refused), refused
    assert len(instances[1].list_agents("ws")) == MAX_AGENTS_PER_WORKSPACE


def test_concurrent_creates_of_one_name_have_one_winner(stores: _Backend) -> None:
    instances = [stores.agents(), stores.agents()]
    for round_ in range(3):
        name = f"same{round_}"
        outcomes = _race([lambda s=s, n=name: s.create("ws", _agent(n)) for s in instances * 4])
        created = [o for o in outcomes if isinstance(o, dict)]
        refused = [o for o in outcomes if isinstance(o, BaseException)]
        assert len(created) == 1, outcomes
        assert len(refused) == 7 and all(isinstance(o, AgentNameTakenError) for o in refused)
    assert [a["name"] for a in instances[0].list_agents("ws")] == ["same0", "same1", "same2"]


def test_concurrent_renames_to_one_name_have_one_winner(stores: _Backend) -> None:
    instances = [stores.agents(), stores.agents()]
    ids = [instances[0].create("ws", _agent(f"r{i}"))["agent_id"] for i in range(4)]
    outcomes = _race(
        [
            lambda s=instances[i % 2], a=a: s.update("ws", a, _agent("target"), expected_revision=1)
            for i, a in enumerate(ids)
        ]
    )
    assert sum(isinstance(o, dict) for o in outcomes) == 1, outcomes
    assert all(isinstance(o, AgentNameTakenError) for o in outcomes if not isinstance(o, dict))
    assert [a["name"] for a in instances[1].list_agents("ws")].count("target") == 1


def test_concurrent_updates_of_one_revision_have_one_winner(stores: _Backend) -> None:
    instances = [stores.agents(), stores.agents()]
    agent_id = instances[0].create("ws", _agent("edit"))["agent_id"]
    outcomes = _race(
        [
            lambda s=s, i=i: s.update(
                "ws", agent_id, _agent("edit", description=f"v{i}"), expected_revision=1
            )
            for i, s in enumerate(instances * 3)
        ]
    )
    winners = [o for o in outcomes if isinstance(o, dict)]
    assert len(winners) == 1 and winners[0]["revision"] == 2, outcomes
    assert all(
        isinstance(o, AgentRevisionConflictError) and o.current_revision == 2
        for o in outcomes
        if not isinstance(o, dict)
    )
    assert instances[1].get("ws", agent_id)["spec"] == winners[0]["spec"]


# ================================================================================= skills


def _skill(name: str, body: str = "Do the thing.", description: str = "A skill.") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n{body}\n"


def test_skill_versions_labels_and_reads(stores: _Backend) -> None:
    store = stores.skills()
    v1 = store.create("ws", _skill("greet", "Say hi."), labels=["prod"], actor="alice")
    assert v1["version"] == 1 and set(v1["labels"]) == {"prod", "latest"}
    same = store.create("ws", _skill("greet", "Say hi."))
    assert same["version"] == 1  # same text: no new version
    v2 = store.create("ws", _skill("greet", "Say hello."), resources={"notes.md": "n"})
    assert (
        v2["version"] == 2 and v2["labels"] == ["latest"] and v2["resources"] == {"notes.md": "n"}
    )
    assert store.get("ws", "greet")["version"] == 2
    assert store.get("ws", "greet", label="prod")["instructions"].strip() == "Say hi."
    assert store.get("ws", "greet", version=1)["labels"] == ["prod"]
    with pytest.raises(SkillNotFoundError):
        store.get("ws", "greet", label="nope")
    with pytest.raises(SkillNotFoundError):
        store.get("ws", "greet", version=9)
    with pytest.raises(SkillNotFoundError):
        store.get("ws", "missing")
    with pytest.raises(SkillNotFoundError):
        store.get("other", "greet")
    with pytest.raises(SkillNotFoundError):
        store.get("ws", "nul\x00")
    summary = store.set_label("ws", "greet", "prod", 2)
    assert summary["labels"] == {"prod": 2, "latest": 2}
    with pytest.raises(SkillNotFoundError):
        store.set_label("ws", "greet", "prod", 7)
    view = store.versions("ws", "greet")
    assert view["version_count"] == 2 and [v["version"] for v in view["versions"]] == [1, 2]
    for name in ("ab", "a-c", "a_b", "b"):
        store.create("ws", _skill(name))
    listed = store.list_skills("ws")
    assert [s["name"] for s in listed] == ["a-c", "a_b", "ab", "b", "greet"]  # byte order
    assert [s["name"] for s in store.list_skills("ws", label="prod")] == ["greet"]
    assert stores.skills().list_skills("ws") == listed  # another instance
    assert store.list_skills("other") == [] and store.list_skills("ws\x00") == []


def test_skill_remove_keeps_numbering(stores: _Backend) -> None:
    store = stores.skills()
    for n in range(3):
        store.create("ws", _skill("s", f"v{n}"))
    assert store.remove("ws", "s", 3) == 1
    assert store.get("ws", "s")["version"] == 2  # latest moved back
    assert store.create("ws", _skill("s", "v-new"))["version"] == 4  # never reuses 3
    assert store.remove("ws", "s") == 3
    with pytest.raises(SkillNotFoundError):
        store.versions("ws", "s")
    with pytest.raises(SkillNotFoundError):
        store.remove("ws", "s")
    assert store.create("ws", _skill("s", "reborn"))["version"] == 5  # counter kept
    with pytest.raises(SkillNotFoundError):
        store.remove("ws", "s", 1)


def test_skill_limits(stores: _Backend) -> None:
    store = stores.skills()
    too_many = [f"l{n}" for n in range(MAX_LABELS_PER_SKILL)]
    with pytest.raises(SkillError, match="labels per request"):
        store.create("ws", _skill("lab"), labels=too_many)
    store.create("ws", _skill("lab"), labels=too_many[: MAX_LABELS_PER_SKILL - 1])
    with pytest.raises(SkillError, match="may carry at most"):
        store.create("ws", _skill("lab", "v2"), labels=["extra"])
    with pytest.raises(SkillError, match="already has"):
        store.set_label("ws", "lab", "another", 1)
    for n in range(1, MAX_VERSIONS_PER_SKILL):
        store.create("ws", _skill("many", f"v{n}"))
    store.create("ws", _skill("many", f"v{MAX_VERSIONS_PER_SKILL - 1}"))  # same text: no-op
    store.create("ws", _skill("many", "v100"))
    with pytest.raises(SkillError, match="remove old versions first"):
        store.create("ws", _skill("many", "v101"))
    for n in range(MAX_SKILLS_PER_WORKSPACE - 2):
        store.create("ws", _skill(f"k{n}"))
    with pytest.raises(SkillError, match="at most"):
        store.create("ws", _skill("overflow"))
    store.create("ws", _skill("lab", "v2"))  # a new version of a stored skill still works
    store.create("ws2", _skill("overflow"))
    with pytest.raises(SkillError):
        store.create("ws", "not a skill")


def test_skill_library_for_runs(stores: _Backend) -> None:
    store = stores.skills()
    store.create("ws", _skill("one"), labels=["prod"])
    store.create("ws", _skill("two"))
    library = store.library_for("ws", ["one"], "prod")
    assert library.names() == ["one"]
    assert store.library_for("ws", ["one", "two"], "latest").names() == ["one", "two"]
    with pytest.raises(SkillNotFoundError, match="no label"):
        store.library_for("ws", ["one", "two"], "prod")
    with pytest.raises(SkillNotFoundError, match="Unknown skill 'three'"):
        store.library_for("ws", ["one", "three"], "latest")
    with pytest.raises(SkillNotFoundError):
        store.library_for("other", ["one"], "latest")


def test_concurrent_publishes_lose_no_version(stores: _Backend) -> None:
    """Eight publishes of one skill from two instances at once: eight versions, numbers 1..8,
    each text stored once (a lost update would drop versions)."""
    instances = [stores.skills(), stores.skills()]
    outcomes = _race(
        [
            lambda s=s, i=i: s.create("ws", _skill("hot", f"text {i}"))
            for i, s in enumerate(instances * 4)
        ]
    )
    assert not any(isinstance(o, BaseException) for o in outcomes), outcomes
    assert sorted(o["version"] for o in outcomes) == list(range(1, 9))
    view = instances[1].versions("ws", "hot")
    assert view["version_count"] == 8
    assert sorted(v["instructions"].strip() for v in view["versions"]) == sorted(
        f"text {i}" for i in range(8)
    )


def test_concurrent_creates_respect_the_skill_limit(stores: _Backend) -> None:
    instances = [stores.skills(), stores.skills()]
    for n in range(MAX_SKILLS_PER_WORKSPACE - 2):
        instances[0].create("ws", _skill(f"base{n}"))
    outcomes = _race(
        [lambda s=s, i=i: s.create("ws", _skill(f"racer{i}")) for i, s in enumerate(instances * 4)]
    )
    created = [o for o in outcomes if isinstance(o, dict)]
    refused = [o for o in outcomes if isinstance(o, BaseException)]
    assert len(created) == 2, outcomes
    assert all(isinstance(o, SkillError) and "at most" in str(o) for o in refused), refused
    assert len(instances[1].list_skills("ws")) == MAX_SKILLS_PER_WORKSPACE


def test_concurrent_label_moves_and_removes_stay_consistent(stores: _Backend) -> None:
    instances = [stores.skills(), stores.skills()]
    for n in range(4):
        instances[0].create("ws", _skill("lbl", f"v{n}"))
    outcomes = _race(
        [
            lambda: instances[0].set_label("ws", "lbl", "prod", 1),
            lambda: instances[1].set_label("ws", "lbl", "prod", 2),
            lambda: instances[0].remove("ws", "lbl", 4),
            lambda: instances[1].create("ws", _skill("lbl", "v-new")),
        ]
    )
    assert not any(isinstance(o, BaseException) for o in outcomes), outcomes
    view = instances[1].versions("ws", "lbl")
    numbers = [v["version"] for v in view["versions"]]
    assert 4 not in numbers and numbers == sorted(numbers) and len(numbers) == 4
    assert view["labels"]["prod"] in (1, 2)
    assert view["labels"]["latest"] == numbers[-1] == 5


# ======================================================================== Postgres only


@pytest.fixture
def pg(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[_Backend]:
    opened = _Backend("postgres", tmp_path, request)
    try:
        yield opened
    finally:
        opened.close()


def _sql(database: Any, sql: str, *args: Any) -> list[Any]:
    async def run(conn: Any) -> Any:
        return await conn.fetch(sql, *args)

    return list(database.run(run))


def test_postgres_tables_and_migrations(pg: _Backend) -> None:
    for _ in range(2):  # two instances: migrations apply once
        pg.sessions(), pg.agents(), pg.skills()
    database = pg.databases[0]
    ledger = _sql(
        database,
        "SELECT component, version FROM nexura_schema_migrations "
        "WHERE component = ANY($1::text[]) ORDER BY component",
        ["sessions", "agents", "skills"],
    )
    assert [(r["component"], r["version"]) for r in ledger] == [
        ("agents", 1),
        ("sessions", 1),
        ("skills", 1),
    ]
    columns = {
        (r["table_name"], r["column_name"]): r["data_type"]
        for r in _sql(
            database,
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = ANY($1::text[])",
            [
                "nexura_sessions",
                "nexura_session_tombstones",
                "nexura_service_agents",
                "nexura_service_skills",
                "nexura_service_skill_counters",
            ],
        )
    }
    # JSON is kept as text exactly as written (never JSONB, which reorders keys).
    assert columns[("nexura_sessions", "data")] == "text"
    assert columns[("nexura_service_agents", "spec")] == "text"
    assert columns[("nexura_service_skills", "data")] == "text"
    assert columns[("nexura_sessions", "revision")] == "bigint"
    assert columns[("nexura_session_tombstones", "revision")] == "bigint"
    constraints = {
        r["conname"]
        for r in _sql(
            database,
            "SELECT conname FROM pg_constraint WHERE conrelid = 'nexura_service_agents'::regclass",
        )
    }
    assert "uq_service_agents_name" in constraints


async def test_postgres_session_nul_id_is_refused_not_stored(pg: _Backend) -> None:
    store = pg.sessions()
    assert await store.get("a\x00b") is None
    assert await store.delete("a\x00b") is False
    with pytest.raises(StorageError, match="NUL"):
        await store.save("a\x00b", [], expected_revision=0)
    assert await store.list_sessions() == []


def test_postgres_name_constraint_is_the_backstop(
    pg: _Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the in-transaction name check skipped, the unique constraint still refuses a
    duplicate, and it is reported as ``AgentNameTakenError`` (not a database error)."""
    from nexura_postgres import PostgresAgentStore

    store = pg.agents()
    store.create("ws", _agent("one"))
    other = store.create("ws", _agent("two"))

    async def skip(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(PostgresAgentStore, "_check_name_pg", staticmethod(skip))
    with pytest.raises(AgentNameTakenError):
        store.create("ws", _agent("one"))
    with pytest.raises(AgentNameTakenError):
        store.update("ws", other["agent_id"], _agent("one"), expected_revision=1)
    assert len(store.list_agents("ws")) == 2


def test_postgres_agent_and_skill_writes_refuse_nul(pg: _Backend) -> None:
    agents = pg.agents()
    with pytest.raises(AgentStoreError):
        agents.create("ws\x00", _agent("x"))
    with pytest.raises(AgentStoreError):
        agents.create("ws", _agent("x"), actor="a\x00")
    with pytest.raises(AgentNotFoundError):
        agents.update("ws", "agt_\x00", _agent("x"), expected_revision=None)
    with pytest.raises(AgentNotFoundError):
        agents.delete("ws", "agt_\x00")
    skills = pg.skills()
    with pytest.raises(SkillError):
        skills.create("ws\x00", _skill("x"))
    with pytest.raises(SkillNotFoundError):
        skills.remove("ws", "x\x00")
    with pytest.raises(SkillNotFoundError):
        skills.library_for("ws", ["x\x00"], "latest")
    assert agents.list_agents("ws") == [] and skills.list_skills("ws") == []
