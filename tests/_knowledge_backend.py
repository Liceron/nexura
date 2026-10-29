# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Test helpers: run the hybrid knowledge index tests on the backend of the test run.

Without ``NEXURA_DATABASE_URL`` the index is the SQLite ``HybridKnowledgeIndex`` (as the tests
always built it). Under ``--service-backend=postgres`` the variable points at a per-test schema
(MVP WP7), and the same tests build ``nexura_postgres.PostgresHybridKnowledgeIndex`` there. The
helpers that look at stored rows run the equivalent statement on either backend, so the
assertions of the tests do not change.
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Any

from nexura_runtime.knowledge.embeddings.embedder import EmbeddingOrchestrator
from nexura_runtime.knowledge.hybrid import (
    HybridKnowledgeIndex,
    KnowledgeIndexBase,
    extract_query_terms,
)


def database_url() -> str | None:
    """The Postgres service database of this test run, if any."""
    return os.environ.get("NEXURA_DATABASE_URL") or None


def on_postgres(index: KnowledgeIndexBase) -> bool:
    return getattr(index, "database", None) is not None


def new_index(embedder: EmbeddingOrchestrator | None = None, **kw: Any) -> KnowledgeIndexBase:
    """A fresh index: in-memory SQLite, or the test's Postgres schema."""
    url = database_url()
    if url is None:
        return HybridKnowledgeIndex(embedder=embedder, **kw)
    from nexura_postgres import Database, PostgresHybridKnowledgeIndex

    return PostgresHybridKnowledgeIndex(Database.from_url(url), embedder=embedder, **kw)


def sibling(
    index: KnowledgeIndexBase, embedder: EmbeddingOrchestrator | None = None, **kw: Any
) -> KnowledgeIndexBase:
    """Another index on the same storage (another configuration of the same service)."""
    if on_postgres(index):
        from nexura_postgres import PostgresHybridKnowledgeIndex

        return PostgresHybridKnowledgeIndex(index.database, embedder=embedder, **kw)  # type: ignore[attr-defined]
    return HybridKnowledgeIndex(index._conn, embedder=embedder, **kw)  # type: ignore[attr-defined]


def persistent_index(
    tmp_path: Path, embedder: EmbeddingOrchestrator | None = None
) -> KnowledgeIndexBase:
    """An index on durable storage: a new connection to one SQLite file, or the test schema."""
    if database_url() is None:
        return HybridKnowledgeIndex(
            sqlite3.connect(tmp_path / "knowledge.sqlite"), embedder=embedder
        )
    return new_index(embedder)


def close(index: KnowledgeIndexBase) -> None:
    """Close the SQLite connection (the Postgres pool belongs to the test schema fixture)."""
    if not on_postgres(index):
        index._conn.close()  # type: ignore[attr-defined]


def _pg(index: KnowledgeIndexBase, sql: str, *params: Any) -> list[tuple[Any, ...]]:
    async def run(conn: Any) -> Any:
        return await conn.fetch(sql, *params)

    return [tuple(r) for r in index.database.run(run)]  # type: ignore[attr-defined]


def _pg_execute(index: KnowledgeIndexBase, sql: str) -> None:
    async def run(conn: Any) -> None:
        await conn.execute(sql)

    index.database.run(run)  # type: ignore[attr-defined]


def count_chunks(index: KnowledgeIndexBase) -> int:
    if on_postgres(index):
        return int(_pg(index, "SELECT COUNT(*) FROM hk_chunks")[0][0])
    return int(index._conn.execute("SELECT COUNT(*) FROM hk_chunks").fetchone()[0])  # type: ignore[attr-defined]


def fts_count(index: KnowledgeIndexBase, needle: str) -> int:
    """Keyword-index entries containing the word ``needle``."""
    if on_postgres(index):
        from nexura_postgres.knowledge import fts_tokens

        query = " ".join(fts_tokens(needle))
        rows = _pg(
            index,
            "SELECT COUNT(*) FROM hk_chunks WHERE fts @@ phraseto_tsquery('simple', $1)",
            query,
        )
        return int(rows[0][0])
    row = index._conn.execute(  # type: ignore[attr-defined]
        "SELECT COUNT(*) FROM hk_fts WHERE hk_fts MATCH ?", (f'"{needle}"',)
    ).fetchone()
    return int(row[0])


def all_chunk_rows(index: KnowledgeIndexBase) -> list[tuple[Any, ...]]:
    """Every stored chunk column (vectors and keyword entries included), in a stable order."""
    if on_postgres(index):
        return _pg(
            index,
            "SELECT seq, chunk_id, document_id, workspace_id, collection_id, chunk_index, text, "
            "start_char, end_char, embedding::text, page_start, page_end, fts::text "
            "FROM hk_chunks ORDER BY document_id, chunk_index",
        )
    return list(
        index._conn.execute(  # type: ignore[attr-defined]
            "SELECT * FROM hk_chunks ORDER BY document_id, chunk_index"
        ).fetchall()
    )


def chunk_document_ids(index: KnowledgeIndexBase) -> list[str]:
    """The document id of every chunk, ordered by document id and chunk index."""
    if on_postgres(index):
        rows = _pg(index, "SELECT document_id FROM hk_chunks ORDER BY document_id, chunk_index")
    else:
        rows = index._conn.execute(  # type: ignore[attr-defined]
            "SELECT document_id FROM hk_chunks ORDER BY document_id, chunk_index"
        ).fetchall()
    return [str(r[0]) for r in rows]


def citation_rows(index: KnowledgeIndexBase, workspace_id: str) -> list[tuple[Any, ...]]:
    """(chunk_id, text, start_char, end_char) of a workspace's chunks."""
    if on_postgres(index):
        return _pg(
            index,
            "SELECT chunk_id, text, start_char, end_char FROM hk_chunks WHERE workspace_id = $1 "
            "ORDER BY seq",
            workspace_id,
        )
    return list(
        index._conn.execute(  # type: ignore[attr-defined]
            "SELECT chunk_id, text, start_char, end_char FROM hk_chunks WHERE workspace_id=?",
            (workspace_id,),
        ).fetchall()
    )


def delete_lineage(index: KnowledgeIndexBase) -> None:
    """Forget every collection's lineage (a database from before lineage tracking)."""
    if on_postgres(index):
        _pg_execute(index, "DELETE FROM hk_lineage")
    else:
        index._conn.execute("DELETE FROM hk_lineage")  # type: ignore[attr-defined]


def database_error(index: KnowledgeIndexBase) -> type[BaseException]:
    """The driver's base error class of the index's backend."""
    if on_postgres(index):
        import asyncpg  # type: ignore[import-untyped]

        return asyncpg.PostgresError  # type: ignore[no-any-return]
    return sqlite3.Error


def break_document_inserts(index: KnowledgeIndexBase) -> None:
    """Make every new document row fail (a NOT NULL column no insert fills); keep the chunks."""
    if on_postgres(index):
        _pg_execute(
            index,
            "ALTER TABLE hk_documents ADD COLUMN bogus_not_null TEXT NOT NULL DEFAULT 'x'; "
            "ALTER TABLE hk_documents ALTER COLUMN bogus_not_null DROP DEFAULT",
        )
        return
    conn = index._conn  # type: ignore[attr-defined]
    conn.execute("DROP TABLE hk_documents")
    conn.execute(
        "CREATE TABLE hk_documents (document_id TEXT PRIMARY KEY, workspace_id TEXT, "
        "collection_id TEXT, title TEXT, source_uri TEXT, mime_type TEXT, content_hash TEXT, "
        "byte_size INTEGER, text TEXT, chunk_count INTEGER, status TEXT, created_at REAL, "
        "updated_at REAL, metadata TEXT, bogus_not_null TEXT NOT NULL)"
    )


def fail_embedding_updates(index: KnowledgeIndexBase, document_id: str) -> type[BaseException]:
    """Abort any embedding update of ``document_id``'s chunks with 'local test write failure';
    returns the error class the backend raises for it."""
    assert re.fullmatch(r"doc_[0-9a-f]{12}", document_id)  # it is put into trigger SQL below
    if on_postgres(index):
        import asyncpg

        _pg_execute(
            index,
            "CREATE FUNCTION hk_test_fail_reindex() RETURNS trigger LANGUAGE plpgsql AS "
            "$$ BEGIN RAISE EXCEPTION 'local test write failure'; END $$; "
            "CREATE TRIGGER fail_reindex BEFORE UPDATE OF embedding ON hk_chunks FOR EACH ROW "
            f"WHEN (OLD.document_id = '{document_id}') EXECUTE FUNCTION hk_test_fail_reindex()",
        )
        return asyncpg.RaiseError  # type: ignore[no-any-return]
    index._conn.execute(  # type: ignore[attr-defined]
        "CREATE TRIGGER fail_reindex BEFORE UPDATE OF embedding ON hk_chunks "
        "WHEN OLD.document_id = '" + document_id + "' "
        "BEGIN SELECT RAISE(ABORT, 'local test write failure'); END"
    )
    return sqlite3.IntegrityError


async def pin_vector_scores(
    index: KnowledgeIndexBase, *, query: str, similar: str, dissimilar: list[str]
) -> None:
    """Postgres: store the query's own vector for the chunks of document ``similar`` (cosine 1)
    and a vector orthogonal to it for those of ``dissimilar`` (cosine 0, below the floor)."""
    from nexura_postgres.knowledge import vector_text

    terms = extract_query_terms(query)
    qvec = (await index.embedder.embed_texts([" ".join(terms)])).embedded_chunks[0].embedding
    # Gram-Schmidt: the first basis vector minus its projection on the query vector.
    norm2 = sum(x * x for x in qvec)
    ortho = [(1.0 if i == 0 else 0.0) - qvec[0] * x / norm2 for i, x in enumerate(qvec)]
    cast = index._vector_cast  # type: ignore[attr-defined]  # the extension's schema
    for title, vec in [(similar, qvec), *[(t, ortho) for t in dissimilar]]:

        async def run(conn: Any, title: str = title, vec: list[float] = vec) -> None:
            await conn.execute(
                f"UPDATE hk_chunks c SET embedding = $2::text{cast} FROM hk_documents d "
                "WHERE d.document_id = c.document_id AND d.title = $1",
                title,
                vector_text(vec),
            )

        index.database.run(run)  # type: ignore[attr-defined]
