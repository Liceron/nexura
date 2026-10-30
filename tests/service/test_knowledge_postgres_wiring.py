# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""The service's knowledge index on Postgres (MVP Postgres WP7): wiring and two instances.

* ``database_url`` selects ``PostgresHybridKnowledgeIndex`` for every ``/knowledge/*`` route and
  for chat retrieval; no ``SqliteVectorStore`` is created and no knowledge table reaches the
  SQLite file (#13). Without a URL the SQLite index is unchanged.
* Two app instances on one schema share documents, deletions and the embedding lineage; chat on
  one instance cites a document uploaded through the other.
* A route waiting for a collection lock keeps the event loop free.
* Crafted retrieval queries never produce a 500 on either backend.

Postgres tests skip without ``NEXURA_TEST_POSTGRES_URI``. The embedder is the labelled
deterministic fixture; nothing calls a model or the network beyond the test database.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
from pathlib import Path
from typing import Any

import pytest
from _local_env import local_headers
from fastapi.testclient import TestClient

from nexura_runtime.knowledge.embeddings.embedder import EmbeddingOrchestrator
from nexura_runtime.knowledge.embeddings.provider import DeterministicEmbeddingProvider
from nexura_runtime.knowledge.hybrid import HybridKnowledgeIndex
from nexura_runtime.secrets.keys import generate_master_key
from nexura_runtime.service.api.app import create_api_app

API = "/api/v1"
KNOWLEDGE_SQLITE_TABLES = {
    "hk_documents",
    "hk_chunks",
    "hk_lineage",
    "hk_fts",
    "vector_collections",
    "vector_generations",
    "vector_records",
}


def _sqlite_tables(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with closing(sqlite3.connect(path)) as conn:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


@pytest.fixture
def partial(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXURA_ALLOW_PARTIAL_POSTGRES", "1")
    monkeypatch.setenv("NEXURA_MASTER_KEY", generate_master_key())
    monkeypatch.delenv("NEXURA_INSECURE_ALLOW_PLAINTEXT_SECRETS", raising=False)
    for name in ("K_SERVICE", "CLOUD_RUN_JOB", "NEXURA_WORKFLOW_STATE_URL"):
        monkeypatch.delenv(name, raising=False)


def _upload(c: TestClient, name: str, text: str, **extra: Any) -> dict[str, Any]:
    r = c.post(
        f"{API}/knowledge/documents/upload",
        headers=local_headers("default", "admin"),
        json={"filename": name, "content": text, **extra},
    )
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


def _retrieve(c: TestClient, query: str, **extra: Any) -> Any:
    return c.post(
        f"{API}/knowledge/retrieve",
        headers=local_headers("default", "admin"),
        json={"query": query, **extra},
    )


def test_postgres_mode_uses_the_postgres_index_and_no_sqlite_vector_store(
    service_database_url: str, tmp_path: Path, partial: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    import nexura_runtime.knowledge.vector.sqlite as sqlite_vectors

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("SqliteVectorStore must not be created in Postgres mode")

    monkeypatch.setattr(sqlite_vectors.SqliteVectorStore, "__init__", refuse)
    db = tmp_path / "svc.sqlite3"
    app = create_api_app(db_path=db, database_url=service_database_url, profile="local-owner")
    assert app.state.vector_store is None and app.state.rag_pipeline is None
    h = local_headers("default", "admin")
    with TestClient(app) as c:
        doc = _upload(c, "heron.md", "The heron wades in marshes.", source_uri="https://x/h")
        index = app.state.hybrid_knowledge_index
        assert type(index).__name__ == "PostgresHybridKnowledgeIndex"
        assert index.database is app.state.service_stores.database  # the shared pool
        hits = _retrieve(c, "heron marshes").json()
        assert hits["citations"][0]["document_id"] == doc["document_id"]
        assert c.get(f"{API}/knowledge/limits", headers=h).json()["workspace_used_bytes"] > 0
        assert c.get(f"{API}/knowledge/collections", headers=h).status_code == 200
        assert c.get(f"{API}/knowledge/documents/{doc['document_id']}", headers=h).json()["text"]
        assert c.post(f"{API}/knowledge/collections/default/reindex", headers=h).status_code == 200
        assert c.delete(f"{API}/knowledge/documents/{doc['document_id']}", headers=h).json()[
            "deleted"
        ]
    assert not (_sqlite_tables(db) & KNOWLEDGE_SQLITE_TABLES)


def test_sqlite_default_is_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEXURA_DATABASE_URL", raising=False)
    db = tmp_path / "svc.sqlite3"
    app = create_api_app(db_path=db, profile="local-owner")
    assert type(app.state.vector_store).__name__ == "SqliteVectorStore"
    assert app.state.rag_pipeline is not None
    with TestClient(app) as c:
        _upload(c, "a.txt", "alpacas eat grass")
        assert type(app.state.hybrid_knowledge_index) is HybridKnowledgeIndex
        assert _retrieve(c, "grass").json()["citations"]
    assert {"hk_documents", "hk_chunks", "hk_fts", "vector_records"} <= _sqlite_tables(db)


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
        assert not (_sqlite_tables(tmp_path / name / "svc.sqlite3") & KNOWLEDGE_SQLITE_TABLES)


def test_two_instances_share_documents_deletes_and_chat_retrieval(
    two_instances: tuple[TestClient, TestClient],
) -> None:
    a, b = two_instances
    h = local_headers("default", "admin")
    doc = _upload(a, "wombat.txt", "Wombats sleep sixteen hours a day in burrows.")
    listed = b.get(f"{API}/knowledge/documents", headers=h).json()
    assert [d["document_id"] for d in listed["items"]] == [doc["document_id"]]
    assert _retrieve(b, "wombats burrows").json()["citations"][0]["title"] == "wombat.txt"
    # Chat retrieval (sessions.py) on B cites the document uploaded through A.
    assert b.post(f"{API}/sessions", json={"session_id": "s1"}, headers=h).status_code == 201
    chat = b.post(
        f"{API}/sessions/s1/chat", json={"message": "How long do wombats sleep?"}, headers=h
    )
    assert chat.status_code == 200, chat.text
    assert "[1]" in chat.json()["reply"] and "wombats" in chat.json()["reply"].lower()
    # A re-upload with the same content through B deduplicates against A's document.
    again = b.post(
        f"{API}/knowledge/documents/upload",
        headers=h,
        json={"filename": "wombat.txt", "content": "Wombats sleep sixteen hours a day in burrows."},
    )
    assert again.status_code == 200 and again.json()["deduplicated"] is True
    assert b.delete(f"{API}/knowledge/documents/{doc['document_id']}", headers=h).status_code == 200
    assert a.get(f"{API}/knowledge/documents/{doc['document_id']}", headers=h).status_code == 404
    assert _retrieve(a, "wombats").json()["citations"] == []
    assert a.get(f"{API}/knowledge/limits", headers=h).json()["workspace_used_bytes"] == 0


def test_two_instances_share_the_embedding_lineage(
    two_instances: tuple[TestClient, TestClient],
) -> None:
    a, b = two_instances
    h = local_headers("default", "admin")
    doc = _upload(a, "test.txt", "alpacas eat grass")
    # B restarts with another embedder configuration on the same database.
    changed = EmbeddingOrchestrator(DeterministicEmbeddingProvider(model_id="fixture-v2"))
    state = b.app.state  # type: ignore[attr-defined]
    state.knowledge_embedder = changed
    state.hybrid_knowledge_index = state.service_stores.knowledge_index(None, changed)
    assert b.get(f"{API}/knowledge/status", headers=h).json()["requires_reindex"] is True
    refused = _retrieve(b, "grass")
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "EMBEDDING_LINEAGE_MISMATCH"
    reindexed = b.post(f"{API}/knowledge/collections/default/reindex", headers=h)
    assert reindexed.status_code == 200 and reindexed.json()["reindexed_chunks"] == 1
    assert _retrieve(b, "grass").json()["citations"][0]["document_id"] == doc["document_id"]
    # A still runs the old configuration: it now has to reindex too.
    assert a.get(f"{API}/knowledge/status", headers=h).json()["requires_reindex"] is True
    assert _retrieve(a, "grass").status_code == 409


def test_concurrent_uploads_through_both_instances_keep_every_document(
    two_instances: tuple[TestClient, TestClient],
) -> None:
    a, b = two_instances
    h = local_headers("default", "admin")

    def upload(i: int) -> int:
        c = a if i % 2 else b
        r = c.post(
            f"{API}/knowledge/documents/upload",
            headers=h,
            json={"filename": f"n{i}.txt", "content": f"note {i} about otters " * (i + 1)},
        )
        return r.status_code

    with ThreadPoolExecutor(8) as pool:
        codes = list(pool.map(upload, range(16)))
    assert codes == [201] * 16
    for c in (a, b):
        items = c.get(f"{API}/knowledge/documents?limit=100", headers=h).json()["items"]
        assert sorted(d["filename"] for d in items) == sorted(f"n{i}.txt" for i in range(16))


def test_a_delete_waiting_for_the_collection_lock_keeps_the_loop_free(
    two_instances: tuple[TestClient, TestClient],
) -> None:
    import asyncio
    from concurrent.futures import Future

    from nexura_postgres import Database
    from nexura_postgres.knowledge import COLLECTION_LOCK_SQL

    a, _ = two_instances
    h = local_headers("default", "admin")
    doc = _upload(a, "gone.txt", "ephemeral zeppelin")
    state = a.app.state  # type: ignore[attr-defined]
    holder = Database(state.service_stores.database_url)
    taken: Future[None] = Future()
    done: Future[None] = Future()

    async def hold(conn: Any) -> None:
        await conn.execute(COLLECTION_LOCK_SQL, "default", "default")
        taken.set_result(None)
        await asyncio.wait_for(asyncio.wrap_future(done), 60)

    outer = holder.submit(hold)
    outcome: list[Any] = []
    try:
        taken.result(30)
        worker = threading.Thread(
            target=lambda: outcome.append(
                a.delete(f"{API}/knowledge/documents/{doc['document_id']}", headers=h)
            )
        )
        worker.start()
        worker.join(0.5)
        assert worker.is_alive() and not outcome, "the delete did not wait for the lock"
        started = time.monotonic()
        assert a.get("/healthz").status_code == 200
        assert a.get(f"{API}/knowledge/limits", headers=h).status_code == 200
        assert time.monotonic() - started < 2, "the waiting delete blocked the event loop"
    finally:
        done.set_result(None)
        outer.result(30)
        holder.close()
    worker.join(30)
    assert outcome and outcome[0].status_code == 200


CRAFTED = [
    'x" OR "1"="1',
    "'; DROP TABLE hk_chunks; --",
    "a & b | !c <-> d:* ",
    "\\'' \\\\ $1 %s",
    "heron\x00marsh\x00",
    "\x00",
    "heron " * 20_000,
    "w" * 50_000,
    " ".join(f"t{i}" for i in range(3000)),
    "NEAR(a b) AND NOT * col:text ^start",
]


@pytest.mark.parametrize("backend_kind", ["sqlite", "postgres"])
def test_crafted_retrieval_queries_never_fail_with_500(
    backend_kind: str,
    tmp_path: Path,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if backend_kind == "postgres":
        url = request.getfixturevalue("service_database_url")
        request.getfixturevalue("partial")
    else:
        monkeypatch.delenv("NEXURA_DATABASE_URL", raising=False)
        url = None
    app = create_api_app(db_path=tmp_path / "svc.sqlite3", database_url=url, profile="local-owner")
    h = local_headers("default", "admin")
    with TestClient(app) as c:
        _upload(c, "heron.txt", "The heron wades in the marsh.")
        for query in CRAFTED:
            for route in ("retrieve", "answer"):
                r = c.post(f"{API}/knowledge/{route}", headers=h, json={"query": query})
                assert r.status_code == 200, (route, query[:40], r.status_code)
                assert isinstance(r.json()["citations"], list)
            weird = c.post(
                f"{API}/knowledge/retrieve",
                headers=h,
                json={"query": query, "collection_id": query[:600]},
            )
            assert weird.status_code in (200, 403), weird.status_code
        assert _retrieve(c, "heron marsh").json()["citations"]
        assert len(c.get(f"{API}/knowledge/documents", headers=h).json()["items"]) == 1


def test_postgres_index_is_built_when_the_app_is_created(
    service_database_url: str, tmp_path: Path, partial: None
) -> None:
    """Review M2: the knowledge migration (CREATE EXTENSION, migration lock) and the catalog
    query run at app creation, so no knowledge or chat request ever builds the index."""
    app = create_api_app(
        db_path=tmp_path / "svc.sqlite3", database_url=service_database_url, profile="local-owner"
    )
    index = app.state.hybrid_knowledge_index  # before any request
    assert type(index).__name__ == "PostgresHybridKnowledgeIndex"
    assert index.embedder is app.state.knowledge_embedder

    async def ledger(conn: Any) -> Any:
        return await conn.fetch(
            "SELECT version FROM nexura_schema_migrations WHERE component = 'knowledge'"
        )

    assert len(app.state.service_stores.database.run(ledger)) == 2
    with TestClient(app) as c:
        _upload(c, "a.txt", "alpacas eat grass")
        assert app.state.hybrid_knowledge_index is index  # never rebuilt by a request
    assert not (_sqlite_tables(tmp_path / "svc.sqlite3") & KNOWLEDGE_SQLITE_TABLES)


def test_a_lazy_index_build_waiting_for_the_migration_lock_keeps_the_loop_free(
    service_database_url: str, tmp_path: Path, partial: None
) -> None:
    """Review M2: if the index has to be built during a request (it was dropped, for example
    after an embedder change), the build waits for the shared migration lock in a worker
    thread, and the service keeps answering; two concurrent first requests build it once."""
    import asyncio
    from concurrent.futures import Future

    from nexura_postgres import Database
    from nexura_postgres.migrations import MIGRATION_LOCK

    app = create_api_app(
        db_path=tmp_path / "svc.sqlite3", database_url=service_database_url, profile="local-owner"
    )
    h = local_headers("default", "admin")
    with TestClient(app) as c:
        state = app.state
        built: list[Any] = []
        factory = state.service_stores.knowledge_index

        def counting(*args: Any, **kwargs: Any) -> Any:
            built.append(1)
            return factory(*args, **kwargs)

        state.service_stores.knowledge_index = counting
        state.hybrid_knowledge_index = None
        holder = Database(state.service_stores.database_url)
        taken: Future[None] = Future()
        done: Future[None] = Future()

        async def hold(conn: Any) -> None:
            await conn.execute("SELECT pg_advisory_xact_lock($1)", MIGRATION_LOCK)
            taken.set_result(None)
            await asyncio.wait_for(asyncio.wrap_future(done), 60)

        outer = holder.submit(hold)
        outcomes: list[Any] = []
        workers = [
            threading.Thread(
                target=lambda path=path: outcomes.append(c.get(f"{API}{path}", headers=h))
            )
            for path in ("/knowledge/limits", "/knowledge/status")
        ]
        try:
            taken.result(30)
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(0.5)
            assert all(w.is_alive() for w in workers) and not outcomes, "the build did not wait"
            health: list[int] = []
            probe = threading.Thread(target=lambda: health.append(c.get("/healthz").status_code))
            started = time.monotonic()
            probe.start()
            probe.join(2)
            assert health == [200], "the event loop was blocked by the index build"
            assert time.monotonic() - started < 2
        finally:
            done.set_result(None)
            outer.result(30)
            holder.close()
        for worker in workers:
            worker.join(30)
        assert sorted(r.status_code for r in outcomes) == [200, 200]
        assert built == [1]  # built once for both first requests
        assert type(state.hybrid_knowledge_index).__name__ == "PostgresHybridKnowledgeIndex"
