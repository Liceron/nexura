# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Hybrid knowledge index contract on both backends (MVP Postgres WP7).

Parametrised tests run the same assertions against the SQLite ``HybridKnowledgeIndex`` and
``nexura_postgres.PostgresHybridKnowledgeIndex`` (skipped without ``NEXURA_TEST_POSTGRES_URI``).
On Postgres every ``index()`` is a new instance with a pool of its own, as another service
instance has. The guarantees:

* the same API, limits, byte accounting and tokenisation (a document is found by the same
  words on both backends);
* crafted query text (quotes, operators, tsquery and FTS5 syntax, NUL, very long input) never
  errors, changes nothing, and on Postgres never appears in SQL text (only as a parameter);
* on Postgres, concurrent ingests on several instances never lose a document, never leave two
  documents with one title, and never let the workspace quota be exceeded; a reindex is seen by
  readers on other instances as the old index or the new one, never a mix; writes wait for the
  collection (and, for ingests, workspace) lock, searches never do.

Everything is local: the labelled deterministic fixture embedder, no model, no network beyond the
test database.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Iterator
from concurrent.futures import Future
from typing import Any

import pytest

from nexura_runtime.knowledge.embeddings.embedder import EmbeddingOrchestrator
from nexura_runtime.knowledge.embeddings.provider import DeterministicEmbeddingProvider
from nexura_runtime.knowledge.hybrid import (
    EmbeddingLineageMismatchError,
    HybridKnowledgeIndex,
    WorkspaceQuotaExceededError,
    _stored_size,
)


def _embedder(
    model: str = "deterministic-vector-fixture-non-semantic", dimensions: int = 8
) -> EmbeddingOrchestrator:
    """The labelled fixture embedder; its vectors depend on the text and the dimension only."""
    return EmbeddingOrchestrator(
        DeterministicEmbeddingProvider(model_id=model, dimensions=dimensions)
    )


class _Opener:
    """Opens indexes on one test's storage; on Postgres each is an instance with its own pool."""

    def __init__(self, backend: str, request: pytest.FixtureRequest) -> None:
        self.backend = backend
        self.url = request.getfixturevalue("service_database_url") if backend == "postgres" else ""
        self.databases: list[Any] = []
        self.conn = (
            sqlite3.connect(":memory:", check_same_thread=False) if backend == "sqlite" else None
        )

    def database(self) -> Any:
        from nexura_postgres import Database

        database = Database(self.url)  # a pool of its own, as another instance has
        self.databases.append(database)
        return database

    def index(self, embedder: EmbeddingOrchestrator | None = None, **kw: Any) -> Any:
        if self.backend == "sqlite":
            return HybridKnowledgeIndex(self.conn, embedder=embedder or _embedder(), **kw)
        from nexura_postgres import PostgresHybridKnowledgeIndex

        return PostgresHybridKnowledgeIndex(self.database(), embedder=embedder or _embedder(), **kw)

    def sql(self, sql: str, *params: Any) -> list[tuple[Any, ...]]:
        """One Postgres statement on the test schema (``$n`` placeholders)."""

        async def run(conn: Any) -> Any:
            return await conn.fetch(sql, *params)

        if not self.databases:
            self.database()
        return [tuple(r) for r in self.databases[0].run(run)]

    def close(self) -> None:
        for database in self.databases:
            database.close()
        if self.conn is not None:
            self.conn.close()


@pytest.fixture
def opener(backend: str, request: pytest.FixtureRequest) -> Iterator[_Opener]:
    opened = _Opener(backend, request)
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def pg(request: pytest.FixtureRequest) -> Iterator[_Opener]:
    """Postgres only (skips without the URI unless Postgres tests are required)."""
    from nexura_runtime.testing.pytest_backend import _postgres_uri

    _postgres_uri(request)
    opened = _Opener("postgres", request)
    try:
        yield opened
    finally:
        opened.close()


async def _ingest(index: Any, name: str, text: str, ws: str = "w", **kw: Any) -> Any:
    return await index.ingest(workspace_id=ws, filename=name, data=text.encode(), **kw)


# =============================================================================== contract


async def test_round_trip_and_byte_accounting(opener: _Opener) -> None:
    index = opener.index()
    text = "नमस्ते दुनिया café 😀 heron wades in the marsh"
    out = await _ingest(
        index, "a.txt", text, source_uri="https://example.test/ü", metadata={"k": "ü", "n": 1}
    )
    doc = out.document
    assert not out.deduplicated and not out.replaced
    assert doc.text_length == len(text) and doc.byte_size == len(text.encode())
    assert [d.document_id for d in index.list_documents("w")] == [doc.document_id]
    assert index.list_documents("w", None)[0] == doc
    assert index.get_document("w", doc.document_id) == doc
    assert index.get_document("other", doc.document_id) is None
    stored = index.get_document_text("w", doc.document_id)
    assert stored == text
    chunk = index.get_chunk("w", doc.document_id, 0)
    assert chunk is not None and chunk["title"] == "a.txt" and chunk["text"] == text
    assert chunk["source_uri"] == "https://example.test/ü"
    expected = _stored_size(text, "a.txt", "https://example.test/ü", '{"k": "ü", "n": 1}')
    assert index.workspace_usage("w") == expected
    assert index.has_documents("w") and not index.has_documents("other")
    again = await _ingest(index, "a.txt", text, metadata={"k": "v"})
    assert again.deduplicated and again.metadata_updated
    assert index.workspace_usage("w") == _stored_size(
        text, "a.txt", "https://example.test/ü", '{"k": "v"}'
    )
    replaced = await _ingest(index, "a.txt", "new heron text")
    assert replaced.replaced and replaced.document.document_id == doc.document_id
    assert replaced.document.created_at == doc.created_at
    assert index.delete_document("w", doc.document_id)
    assert not index.delete_document("w", doc.document_id)
    assert index.workspace_usage("w") == 0 and not index.has_documents("w")
    assert index.list_documents("w") == []


@pytest.mark.parametrize(
    ("query", "title"),
    [
        ("cafe", "fr.txt"),  # Latin diacritics fold (FTS5 unicode61 does the same)
        ("CAFÉ ouvert", "fr.txt"),
        ("txt", "report.md"),  # punctuation splits words: report.final.txt
        ("14", "report.md"),  # 3.14
        ("case", "code.txt"),  # snake_case_name
        ("snake_case_name", "code.txt"),
        ("स्वतंत्र", "hi.txt"),
    ],
)
async def test_keyword_tokenisation_matches_on_both_backends(
    opener: _Opener, query: str, title: str
) -> None:
    index = opener.index()
    await _ingest(index, "fr.txt", "Le café est ouvert le matin")
    await _ingest(index, "report.md", "report.final.txt contains 3.14 values")
    await _ingest(index, "code.txt", "the snake_case_name variable")
    await _ingest(index, "hi.txt", "नेक्सुरा एक स्वतंत्र ढांचा है")
    hits = await index.search(workspace_id="w", query=query, vector_weight=0)
    assert [h.title for h in hits] == [title]
    assert hits[0].keyword_rank == 1 and hits[0].matched_by == ["keyword"]


async def test_keyword_ranking_prefers_the_rare_term(opener: _Opener) -> None:
    """Relevance, not exact order: a chunk with the rare query word ranks above chunks that
    repeat a common one (bm25 on SQLite, IDF-weighted ts_rank_cd on Postgres)."""
    index = opener.index()
    for i in range(6):
        await _ingest(index, f"common{i}.txt", f"the river and the report of the number {i}")
    await _ingest(index, "target.txt", "axolotl notes")
    hits = await index.search(workspace_id="w", query="the axolotl", vector_weight=0, top_k=10)
    assert hits[0].title == "target.txt"
    assert {h.title for h in hits} == {"target.txt", *(f"common{i}.txt" for i in range(6))}


CRAFTED = [
    'zqxmarker" OR "1"="1',
    "zqxmarker NEAR(a b) AND NOT *",
    "zqxmarker'); DROP TABLE hk_chunks; --",
    "zqxmarker & hello | !world <-> x:* ",
    "zqxmarker:* & 'quoted''s' \\\\ backslash \\x00",
    "zqxmarker\x00hello\x00\x00world",
    "\x00zqxmarker",
    "zqxmarker $1 %s %(x)s {0} ${y}",
    "zqxmarker " + "ab " * 40_000,  # very long input
    "zqxmarker " + " ".join(f"t{i}" for i in range(5000)),  # many terms
    "zqxmarker " + "w" * 100_000,  # one very long word
    "zqxmarker \u202e\u200b\ud7ff \ufdfd \U0001f600",
]


class _Recorder:
    """Proxy connection that records every statement's SQL text."""

    def __init__(self, conn: Any, log: list[str]) -> None:
        self._conn, self._log = conn, log

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._conn, name)
        if name not in ("execute", "fetch", "fetchrow", "fetchval"):
            return attr

        def call(sql: str, *args: Any, **kw: Any) -> Any:
            self._log.append(sql)
            return attr(sql, *args, **kw)

        return call


class _RecordingDatabase:
    def __init__(self, database: Any, log: list[str]) -> None:
        self._db, self._log = database, log

    def run(self, fn: Any, **kw: Any) -> Any:
        return self._db.run(lambda conn: fn(_Recorder(conn, self._log)), **kw)

    async def arun(self, fn: Any, **kw: Any) -> Any:
        return await self._db.arun(lambda conn: fn(_Recorder(conn, self._log)), **kw)


@pytest.mark.parametrize("query", CRAFTED, ids=range(len(CRAFTED)))
async def test_crafted_queries_are_inert_and_never_sql_text(opener: _Opener, query: str) -> None:
    index = opener.index()
    await _ingest(index, "a.txt", "ordinary content about hello world zqxmarker")
    log: list[str] = []
    if opener.backend == "postgres":
        index.database = _RecordingDatabase(index.database, log)
    for weights in ((1.0, 1.0), (0.0, 1.0), (1.0, 0.0)):
        hits = await index.search(
            workspace_id="w", query=query, vector_weight=weights[0], keyword_weight=weights[1]
        )
        assert isinstance(hits, list)
        if weights[1] > 0:
            assert hits and hits[0].title == "a.txt"  # the marker word is still found
    assert len(index.list_documents("w")) == 1 and index.has_documents("w")
    if opener.backend == "postgres":
        assert log, "statements were not recorded"
        assert not [sql for sql in log if "zqxmarker" in sql.lower()]
        assert not [sql for sql in log if "\x00" in sql or "drop table" in sql.lower()]


async def test_collections_and_workspaces_are_isolated(opener: _Opener) -> None:
    index = opener.index()
    await _ingest(index, "a.txt", "walrus facts", collection_id="c1")
    await _ingest(index, "a.txt", "walrus facts", ws="other")
    assert await index.search(workspace_id="w", query="walrus", collection_id="c2") == []
    assert len(await index.search(workspace_id="w", query="walrus", collection_id="c1")) == 1
    assert [d.collection_id for d in index.list_documents("w", None)] == ["c1"]
    assert index.list_documents("w") == []
    assert index.embedding_status("w", "c1")["indexed_embedder"] is not None
    assert index.embedding_status("w")["indexed_embedder"] is None


# ========================================================================= Postgres only


def test_postgres_schema_tables_types_and_migration(pg: _Opener) -> None:
    pg.index()
    pg.index()  # a second instance finds the schema migrated
    columns = dict(
        pg.sql(
            "SELECT column_name, udt_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'hk_chunks'"
        )
    )
    assert columns["embedding"] == "vector" and columns["fts"] == "tsvector"
    tables = {
        r[0]
        for r in pg.sql(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_name LIKE 'hk%'"
        )
    }
    assert tables == {"hk_documents", "hk_chunks", "hk_lineage", "hk_collection_versions"}
    indexes = dict(
        pg.sql("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = current_schema()")
    )
    assert "USING gin (fts)" in indexes["hk_chunks_fts"]
    assert "UNIQUE" in indexes["hk_documents_title"]
    assert not [n for n, d in indexes.items() if "hnsw" in d or "ivfflat" in d]  # no ANN yet
    ledger = pg.sql(
        "SELECT version, name FROM nexura_schema_migrations WHERE component = 'knowledge'"
    )
    assert sorted(ledger) == [(1, "hybrid knowledge index"), (2, "collection write versions")]
    assert pg.sql("SELECT count(*) FROM pg_extension WHERE extname = 'vector'") == [(1,)]


async def test_postgres_nul_and_oversized_ids(pg: _Opener) -> None:
    from nexura_postgres.knowledge import MAX_COLLECTION_ID_CHARS, InvalidKnowledgeIdError

    index = pg.index()
    text = "heron\x00wades in\x00the marsh"
    out = await index.ingest(
        workspace_id="w", filename="n\x00.txt", data=text.encode(), source_uri="u\x00ri"
    )
    stored = index.get_document_text("w", out.document.document_id)
    parsed = HybridKnowledgeIndex()._parse(text.encode(), "n.txt", None)[0]  # what SQLite stores
    assert stored == parsed.replace("\x00", "\ufffd") and len(stored) == len(parsed)
    assert index._storable_text("a\x00b\x00") == "a\ufffdb\ufffd"  # text a parser keeps NUL in
    assert out.document.title == "n\ufffd.txt" and out.document.source_uri == "u\ufffdri"
    assert index.workspace_usage("w") == _stored_size(stored, "n\ufffd.txt", "u\ufffdri", "{}")
    hits = await index.search(workspace_id="w", query="wades\x00 marsh")
    assert hits and hits[0].citation.end_char <= len(text)
    for ws, coll in (
        ("w\x00", "default"),
        ("w", "c\x00"),
        ("w", "c" * (MAX_COLLECTION_ID_CHARS + 1)),
    ):
        with pytest.raises(InvalidKnowledgeIdError) as refused:
            await index.ingest(workspace_id=ws, collection_id=coll, filename="x.txt", data=b"x")
        assert refused.value.code == "INVALID_COLLECTION_ID"
        with pytest.raises(InvalidKnowledgeIdError):
            await index.reindex(ws, coll)
    assert await index.search(workspace_id="w\x00", query="heron") == []
    assert await index.search(workspace_id="w", query="heron", collection_id="d\x00") == []
    assert index.list_documents("w\x00") == [] and index.list_documents("w", "d\x00") == []
    assert index.get_document("w", "doc\x00") is None and index.get_chunk("w\x00", "d", 0) is None
    assert index.get_document_text("w", "doc\x00") is None
    assert not index.delete_document("w", "doc\x00") and not index.has_documents("w\x00")
    assert index.workspace_usage("w\x00") == 0
    assert index.embedding_status("w\x00")["requires_reindex"] is False
    assert index.get_chunk("w", out.document.document_id, 2**40) is None
    assert len(index.list_documents("w")) == 1


class _Held:
    """Holds one advisory lock in another transaction (its own pool) until ``release``."""

    def __init__(self, database: Any, sql: str, *args: Any) -> None:
        self.taken: Future[None] = Future()
        self.done: Future[None] = Future()

        async def hold(conn: Any) -> None:
            await conn.execute(sql, *args)
            self.taken.set_result(None)
            await asyncio.wait_for(asyncio.wrap_future(self.done), 60)

        self.outer = database.submit(hold)
        self.taken.result(30)

    def release(self) -> None:
        if not self.done.done():
            self.done.set_result(None)
        self.outer.result(30)


async def _waits(held: _Held, work: Any) -> Any:
    """Assert ``work`` (a coroutine) is still waiting 0.5 s into ``held``, then release it."""
    task = asyncio.ensure_future(work)
    try:
        await asyncio.sleep(0.5)
        assert not task.done(), "the write did not wait for the lock"
    finally:
        await asyncio.to_thread(held.release)
    return await asyncio.wait_for(task, 30)


async def test_postgres_writes_wait_for_their_locks_and_reads_never_do(pg: _Opener) -> None:
    from nexura_postgres.knowledge import COLLECTION_LOCK_SQL, WORKSPACE_LOCK_SQL

    index, other = pg.index(), pg.index()
    doc = (await _ingest(index, "a.txt", "heron marsh")).document
    holder = pg.database()

    held = _Held(holder, COLLECTION_LOCK_SQL, "w", "default")
    try:  # reads and another collection's writes go on while one collection is locked
        assert await other.search(workspace_id="w", query="heron")
        assert other.list_documents("w") and other.embedding_status("w")
        await _ingest(other, "c2.txt", "other collection", collection_id="c2")
    except BaseException:
        held.release()
        raise
    out = await _waits(held, _ingest(other, "b.txt", "egret pond"))
    assert out.document.title == "b.txt"
    held = _Held(holder, COLLECTION_LOCK_SQL, "w", "default")
    result = await _waits(held, other.reindex("w"))
    assert result["reindexed_chunks"] == 2
    held = _Held(holder, COLLECTION_LOCK_SQL, "w", "default")
    assert await _waits(held, asyncio.to_thread(other.delete_document, "w", doc.document_id))

    held = _Held(holder, WORKSPACE_LOCK_SQL, "w")
    try:  # the workspace lock (quota) does not hold up a reindex
        await asyncio.wait_for(other.reindex("w"), 10)
    except BaseException:
        held.release()
        raise
    out = await _waits(held, _ingest(other, "d.txt", "kingfisher", collection_id="c3"))
    assert out.document.collection_id == "c3"


async def test_postgres_concurrent_ingests_on_two_instances_lose_nothing(pg: _Opener) -> None:
    a, b = pg.index(), pg.index()
    for round_ in range(3):
        ws = f"w{round_}"
        texts = {f"doc{i}.txt": f"shared topic {i} " + "word " * (i * 50) for i in range(24)}
        outcomes = await asyncio.gather(
            *[
                _ingest(a if i % 2 else b, name, text, ws=ws)
                for i, (name, text) in enumerate(texts.items())
            ]
        )
        assert all(not o.deduplicated and not o.replaced for o in outcomes)
        docs = a.list_documents(ws)
        assert sorted(d.title for d in docs) == sorted(texts)
        assert (
            sum(d.chunk_count for d in docs)
            == pg.sql("SELECT count(*) FROM hk_chunks WHERE workspace_id = $1", ws)[0][0]
        )
        expected = sum(_stored_size(t, n, None, "{}") for n, t in texts.items())
        assert a.workspace_usage(ws) == b.workspace_usage(ws) == expected


async def test_postgres_concurrent_ingests_never_exceed_the_workspace_quota(pg: _Opener) -> None:
    size = _stored_size("q" * 100, "f00.txt", None, "{}")
    a = pg.index(max_workspace_bytes=5 * size + size // 2)
    b = pg.index(max_workspace_bytes=5 * size + size // 2)
    for round_ in range(3):
        ws = f"quota{round_}"
        results = await asyncio.gather(
            *[
                _ingest(
                    a if i % 2 else b,
                    f"f{i:02d}.txt",
                    chr(ord("a") + i) * 100,
                    ws=ws,
                    collection_id=f"c{i % 3}",  # three collections, one quota
                )
                for i in range(12)
            ],
            return_exceptions=True,
        )
        ok = [r for r in results if not isinstance(r, BaseException)]
        refused = [r for r in results if isinstance(r, WorkspaceQuotaExceededError)]
        assert len(ok) == 5 and len(refused) == 7, results
        assert a.workspace_usage(ws) == 5 * size <= a.max_workspace_bytes


async def test_postgres_racing_same_title_ingests_leave_one_document(pg: _Opener) -> None:
    a, b = pg.index(), pg.index()
    for round_ in range(3):
        ws = f"title{round_}"
        await asyncio.gather(
            *[
                _ingest(a if i % 2 else b, "same.txt", f"version {i} " * (i + 1), ws=ws)
                for i in range(8)
            ]
        )
        docs = a.list_documents(ws)
        assert len(docs) == 1
        chunks = pg.sql(
            "SELECT document_id, count(*) FROM hk_chunks WHERE workspace_id = $1 "
            "GROUP BY document_id",
            ws,
        )
        assert chunks == [(docs[0].document_id, docs[0].chunk_count)]
        text = a.get_document_text(ws, docs[0].document_id) or ""
        assert a.workspace_usage(ws) == _stored_size(text, "same.txt", None, "{}")


async def _vector_view(index: Any, ws: str) -> Any:
    """("mismatch", None) or ("ok", [(chunk_id, rounded vector score), ...])."""
    try:
        hits = await index.search(
            workspace_id=ws,
            query="alpha shared",
            keyword_weight=0,
            top_k=50,
            min_vector_score=-2.0,
        )
    except EmbeddingLineageMismatchError:
        return ("mismatch", None)
    return ("ok", [(h.chunk_id, round(h.vector_score or 0.0, 6)) for h in hits])


async def test_postgres_reindex_is_atomic_for_readers_on_other_instances(pg: _Opener) -> None:
    """Writers flip a collection between two embedders (E1, E2) while readers configured with
    each embedder search on their own instances. Every read is either refused (lineage of the
    other embedder) or exactly the baseline result of its own embedder: never a mix of vectors,
    and never new vectors under old lineage or the reverse."""

    def e1() -> EmbeddingOrchestrator:
        return _embedder("fixture-e1", 8)

    def e2() -> EmbeddingOrchestrator:
        return _embedder("fixture-e2", 12)  # other vectors (the fixture ignores the model id)

    writer1, writer2 = pg.index(e1()), pg.index(e2())
    reader1, reader2 = pg.index(e1()), pg.index(e2())
    ws = "atomic"
    for i in range(120):
        await _ingest(writer1, f"d{i:03d}.txt", f"alpha shared item{i} " * 3, ws=ws)
    baseline1 = await _vector_view(reader1, ws)
    await writer2.reindex(ws)
    baseline2 = await _vector_view(reader2, ws)
    assert baseline1[0] == baseline2[0] == "ok" and baseline1[1] != baseline2[1]
    assert len(baseline1[1]) == 50
    assert (await _vector_view(reader1, ws))[0] == "mismatch"
    await writer1.reindex(ws)

    stop = asyncio.Event()
    seen: dict[str, list[str]] = {"r1": [], "r2": []}
    bad: list[Any] = []

    async def read(name: str, reader: Any, baseline: Any) -> None:
        while not stop.is_set():
            view = await _vector_view(reader, ws)
            seen[name].append(view[0])
            if view[0] == "ok" and view != baseline:
                bad.append((name, view))

    readers = [
        asyncio.ensure_future(read("r1", reader1, baseline1)),
        asyncio.ensure_future(read("r2", reader2, baseline2)),
    ]
    try:
        for _ in range(8):
            await writer2.reindex(ws)
            await asyncio.sleep(0.01)
            await writer1.reindex(ws)
            await asyncio.sleep(0.01)
    finally:
        stop.set()
        await asyncio.gather(*readers)
    assert not bad, bad[:2]
    for name in ("r1", "r2"):  # both outcomes were observed by both readers
        assert "ok" in seen[name] and "mismatch" in seen[name], (name, len(seen[name]))


@pytest.mark.parametrize("change", ["ingest", "replace", "delete"])
async def test_postgres_an_ingest_during_a_reindex_refuses_the_reindex(
    pg: _Opener, change: str
) -> None:
    """A change on another instance between a reindex's snapshot and its commit (a new
    document, a replaced document or a deleted one) refuses the reindex; a retry succeeds."""
    a1 = pg.index(_embedder("fixture-e1"))
    b1 = pg.index(_embedder("fixture-e1"))  # same configuration, another instance
    first = await _ingest(a1, "a.txt", "alpha shared one")
    await _ingest(a1, "c.txt", "alpha shared three")
    a2 = pg.index(_embedder("fixture-e2", 12))
    original = a2.embedder.embed_texts
    started, go_on = asyncio.Event(), asyncio.Event()

    async def slow(texts: Any) -> Any:
        result = await original(texts)
        started.set()
        await go_on.wait()
        return result

    a2.embedder.embed_texts = slow  # type: ignore[method-assign]
    reindex = asyncio.ensure_future(a2.reindex("w"))
    await started.wait()
    # Another instance, between snapshot and commit.
    if change == "ingest":
        await _ingest(b1, "b.txt", "alpha shared two")
        expected = {"a.txt", "b.txt", "c.txt"}
    elif change == "replace":
        await _ingest(b1, "a.txt", "alpha shared one, revised")
        expected = {"a.txt", "c.txt"}
    else:
        assert await asyncio.to_thread(b1.delete_document, "w", first.document.document_id)
        expected = {"c.txt"}
    go_on.set()
    with pytest.raises(EmbeddingLineageMismatchError, match="changed during reindex"):
        await reindex
    status = b1.embedding_status("w")
    assert status["requires_reindex"] is False  # still E1 everywhere
    hits = await b1.search(workspace_id="w", query="alpha shared")
    assert {h.title for h in hits} == expected
    a2.embedder.embed_texts = original  # type: ignore[method-assign]
    assert (await a2.reindex("w"))["reindexed_chunks"] == len(expected)  # a retry succeeds


async def test_postgres_keyword_query_writes_no_temporary_files(pg: _Opener) -> None:
    """Review H1 regression: on a corpus where every chunk contains the query words, the
    keyword query must not spill. ``work_mem`` is lowered to 1 MB and (superuser only)
    ``temp_file_limit`` to 8 MB, so a shape that materialises a row per (chunk, term) and sorts
    them either fails or reports temp blocks under EXPLAIN (ANALYZE, BUFFERS)."""
    import json

    from nexura_postgres import knowledge as pgk

    index = pg.index()
    vocab = [f"w{i:03d}" for i in range(200)]

    async def fill(conn: Any) -> None:
        await conn.execute("SELECT setseed(0.42)")
        await conn.execute(
            "INSERT INTO hk_chunks (chunk_id, document_id, workspace_id, collection_id, "
            "chunk_index, text, start_char, end_char, embedding, fts) "
            "SELECT 'c' || g, 'd' || g, 'w', 'default', 0, x.t, 0, length(x.t), "
            f"('[1,0,0,0,0,0,0,0]'::text){index._vector_cast}, to_tsvector('simple', x.t) "
            "FROM generate_series(1, 2000) AS g CROSS JOIN LATERAL (SELECT string_agg("
            "($1::text[])[1 + floor(power(random(), 3) * 200)::int], ' ') AS t "
            "FROM generate_series(1, 40) AS w WHERE g > 0) AS x",
            vocab,
        )
        await conn.execute("ANALYZE hk_chunks")

    pg.databases[0].run(fill, timeout=120)
    terms = vocab[:16]  # the most common words: nearly every chunk matches several

    def temp_blocks(node: dict[str, Any]) -> int:
        own = int(node.get("Temp Written Blocks", 0))
        return own + sum(temp_blocks(child) for child in node.get("Plans", []))

    async def explain(conn: Any) -> Any:
        await conn.execute("SET LOCAL work_mem = '1MB'")
        if await conn.fetchval("SELECT rolsuper FROM pg_roles WHERE rolname = current_user"):
            await conn.execute("SET LOCAL temp_file_limit = '8MB'")
        raw = await conn.fetchval(
            "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + pgk._KEYWORD_SQL,
            "w",
            "default",
            terms,
            20,
        )
        return json.loads(raw)[0]

    plan = pg.databases[0].run(explain, timeout=60)
    assert temp_blocks(plan["Plan"]) == 0, "the keyword query wrote temporary files"
    assert plan["Plan"]["Actual Rows"] == 20
    ids = pg.sql(pgk._KEYWORD_SQL, "w", "default", terms, 20)
    assert len(ids) == 20 and len({r[0] for r in ids}) == 20  # each chunk ranked once


async def test_postgres_search_waits_for_no_writer(pg: _Opener) -> None:
    """A long ingest commit (holding both locks) never delays searches on another instance."""
    from nexura_postgres.knowledge import COLLECTION_LOCK_SQL, WORKSPACE_LOCK_SQL

    index, reader = pg.index(), pg.index()
    await _ingest(index, "a.txt", "heron marsh")
    holder = pg.database()
    held_ws = _Held(holder, WORKSPACE_LOCK_SQL, "w")
    held_coll = _Held(pg.database(), COLLECTION_LOCK_SQL, "w", "default")
    try:
        started = time.monotonic()
        for _ in range(5):
            assert await reader.search(workspace_id="w", query="heron")
        assert time.monotonic() - started < 5
    finally:
        held_ws.release()
        held_coll.release()


def test_postgres_index_runs_its_waits_off_the_calling_loop(pg: _Opener) -> None:
    """``ingest`` waiting for a held lock leaves the caller's event loop free."""
    from nexura_postgres.knowledge import WORKSPACE_LOCK_SQL

    index = pg.index()
    held = _Held(pg.database(), WORKSPACE_LOCK_SQL, "w")
    ticks: list[float] = []

    async def main() -> None:
        task = asyncio.ensure_future(_ingest(index, "a.txt", "heron"))
        for _ in range(10):
            ticks.append(time.monotonic())
            await asyncio.sleep(0.05)
        assert not task.done()
        await asyncio.to_thread(held.release)
        await asyncio.wait_for(task, 30)

    try:
        asyncio.run(main())
    finally:
        held.release()
    gaps = [b - a for a, b in zip(ticks, ticks[1:], strict=False)]
    assert max(gaps) < 0.5, gaps
