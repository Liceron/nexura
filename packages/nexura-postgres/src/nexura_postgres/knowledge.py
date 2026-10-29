# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""The hybrid knowledge index on Postgres (MVP Postgres WP7, store #12).

``PostgresHybridKnowledgeIndex`` has the API, errors, limits and embedding lineage of the SQLite
``nexura_runtime.knowledge.hybrid.HybridKnowledgeIndex``. Tables keep the SQLite names
(``hk_documents``, ``hk_chunks``, ``hk_lineage``); migration component ``knowledge`` creates them
and the ``vector`` extension (``CREATE EXTENSION IF NOT EXISTS vector``).

Keyword search
    Each chunk has a ``tsvector`` column built with the ``'simple'`` configuration and a GIN
    index; it replaces the FTS5 table. A chunk matches when it contains any query term (each
    term a ``phraseto_tsquery('simple', term)``, as each FTS5 term is a quoted phrase, OR-ed).
    Ranking is ``ts_rank_cd`` (cover density, normalised by length) per matched term, weighted
    by the term's bm25-style inverse document frequency in the collection, where SQLite uses
    FTS5 ``bm25``; the order of similar chunks can therefore differ. Both sides are tokenised the
    same way before Postgres sees them: the words of ``hybrid.extract_query_terms`` (letters,
    digits, underscore, Devanagari), lower-cased, with Latin diacritics folded as FTS5's
    ``unicode61`` does. The terms reach SQL only as an array parameter: user text never becomes
    SQL text or ``tsquery`` syntax. Cost: at most ``MAX_KEYWORD_TERMS`` (32) distinct terms;
    each matching chunk is scored once, all document frequencies come from one pass, and the
    statement needs no temporary files (see ``_KEYWORD_SQL``).

Vector search
    Embeddings are pgvector ``vector`` values; search is an exact ``<=>`` (cosine distance)
    scan over the newest ``max_scan`` chunks of one workspace and collection, as on SQLite. There
    is no ANN index yet. The column has no fixed dimension: each collection's dimension is the
    one of its embedding lineage, and rows of another dimension score 0 (SQLite's rule).

Consistency across instances
    Every write is one transaction. An ingest takes the workspace lock (the quota is per
    workspace) and then the collection lock; a reindex and a delete take the collection lock
    (``pg_advisory_xact_lock``, keys below). So concurrent ingests never lose a document or
    double-count bytes against the quota, and a reindex replaces every vector of the collection
    and its lineage in one commit, after checking that nothing changed since its snapshot: every
    ingest, delete and reindex bumps the collection's row in ``hk_collection_versions`` in its
    own transaction, so the check is one row read under the lock.
    Any new writer of ``hk_chunks`` must take the collection lock and bump that version in the
    same transaction, or a concurrent reindex can overwrite its change.
    A search reads lineage, keyword ranks, vector ranks and hit rows in one ``REPEATABLE READ``
    transaction, so a reader on another instance sees the old index or the new one, never a mix.

Postgres-only differences
    ``TEXT`` cannot hold NUL: NUL in document text, title or source URI is stored as U+FFFD (the
    character count, and so citation offsets, is unchanged; byte accounting counts the stored
    text). A workspace or collection id with NUL, or a collection id over
    ``MAX_COLLECTION_ID_CHARS`` characters, is refused on write with
    ``InvalidKnowledgeIdError`` and reads as empty. Non-finite embedding values are refused.

Pooled-endpoint safety (design §2b-5): no session state; locks are transaction-scoped.
Synchronous methods wait on the network (``blocking_io = True``): async callers run them in a
worker thread. Constructing the index applies its migrations (the shared migration lock,
``CREATE EXTENSION``), so the service builds it at startup, never on the event loop. The
asynchronous methods (``ingest``, ``search``, ``reindex``) never block the calling event loop.
"""

from __future__ import annotations

import json
import math
import time
import unicodedata
import uuid
from typing import Any

from nexura_postgres.migrations import MIGRATION_TIMEOUT, Migration, migrate_all, register
from nexura_runtime.knowledge.chunks.chunkers import BaseChunker
from nexura_runtime.knowledge.embeddings.embedder import EmbeddingOrchestrator
from nexura_runtime.knowledge.hybrid import (
    _TERM_RE,
    DEFAULT_MAX_FILE_BYTES,
    DEFAULT_MAX_METADATA_BYTES,
    DEFAULT_MAX_SCAN,
    DEFAULT_MAX_SOURCE_URI_CHARS,
    DEFAULT_MAX_TOP_K,
    DEFAULT_MAX_WORKSPACE_BYTES,
    DEFAULT_MIN_VECTOR_SCORE,
    EmbeddingLineageMismatchError,
    IndexedDocument,
    IngestOutcome,
    KnowledgeError,
    KnowledgeIndexBase,
    SearchHit,
    WorkspaceQuotaExceededError,
    _chunk_pages,
    _stored_size,
    build_hits,
    extract_query_terms,
    fuse_rankings,
)

COMPONENT = "knowledge"
#: ``hk_chunks`` names the extension's schema in its column type, whatever the search path is
#: (a DO block, so the migration text and its checksum stay fixed).
_CHUNKS_DDL = """DO $migration$
DECLARE
    ext_schema TEXT;
BEGIN
    SELECT n.nspname INTO STRICT ext_schema
    FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
    WHERE e.extname = 'vector';
    EXECUTE format($ddl$CREATE TABLE hk_chunks (
        seq BIGINT GENERATED ALWAYS AS IDENTITY,
        chunk_id TEXT NOT NULL,
        document_id TEXT NOT NULL,
        workspace_id TEXT NOT NULL,
        collection_id TEXT NOT NULL,
        chunk_index INTEGER NOT NULL,
        text TEXT NOT NULL,
        start_char INTEGER NOT NULL,
        end_char INTEGER NOT NULL,
        embedding %I.vector NOT NULL,
        page_start INTEGER,
        page_end INTEGER,
        fts tsvector NOT NULL,
        PRIMARY KEY (document_id, chunk_index))$ddl$, ext_schema);
END
$migration$"""
MIGRATIONS: tuple[Migration, ...] = (
    (
        1,
        "hybrid knowledge index",
        (
            "CREATE EXTENSION IF NOT EXISTS vector",
            """CREATE TABLE hk_lineage (
                workspace_id TEXT NOT NULL,
                collection_id TEXT NOT NULL,
                embedder TEXT NOT NULL,
                PRIMARY KEY (workspace_id, collection_id))""",
            """CREATE TABLE hk_documents (
                document_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                collection_id TEXT NOT NULL,
                title TEXT NOT NULL,
                source_uri TEXT,
                mime_type TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                byte_size BIGINT NOT NULL,
                text TEXT NOT NULL,
                chunk_count INTEGER NOT NULL,
                status TEXT NOT NULL,
                created_at DOUBLE PRECISION NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}')""",
            "CREATE INDEX hk_documents_scope ON hk_documents (workspace_id, collection_id)",
            # One document per title and collection (the ingest locks keep it; this backs them).
            "CREATE UNIQUE INDEX hk_documents_title "
            "ON hk_documents (workspace_id, collection_id, md5(title))",
            "CREATE INDEX hk_documents_hash "
            "ON hk_documents (workspace_id, collection_id, content_hash)",
            _CHUNKS_DDL,
            "CREATE INDEX hk_chunks_scope ON hk_chunks (workspace_id, collection_id, seq)",
            "CREATE INDEX hk_chunks_chunk_id ON hk_chunks (chunk_id)",
            "CREATE INDEX hk_chunks_fts ON hk_chunks USING gin (fts)",
        ),
    ),
    (
        2,
        "collection write versions",
        (
            # Bumped in the same transaction as every change of a collection's chunks (ingest,
            # delete, reindex), so a reindex detects a concurrent change in O(1) (review M1).
            """CREATE TABLE hk_collection_versions (
                workspace_id TEXT NOT NULL,
                collection_id TEXT NOT NULL,
                version BIGINT NOT NULL,
                PRIMARY KEY (workspace_id, collection_id))""",
        ),
    ),
)
register(COMPONENT, MIGRATIONS)

#: Serialises quota checks of one workspace (taken first by every ingest).
WORKSPACE_LOCK_SQL = (
    "SELECT pg_advisory_xact_lock(hashtextextended('nexura-knowledge-ws/' || $1, 0))"
)
#: Serialises writes to one collection: ingest (after the workspace lock), reindex, delete.
COLLECTION_LOCK_SQL = (
    "SELECT pg_advisory_xact_lock(hashtextextended("
    "'nexura-knowledge/' || length($1) || '/' || $1 || '/' || $2, 0))"
)
#: Longest collection id stored (index rows have a size limit; longer ids are refused).
MAX_COLLECTION_ID_CHARS = 512
#: ``ts_rank_cd`` normalisation: 1 divides by 1 + log(document length), as bm25 favours
#: shorter chunks (the WP7 report compares the options on the retrieval example dataset).
RANK_NORMALIZATION = 1

_SIZE_SQL = (
    "octet_length(text) + octet_length(metadata) + octet_length(title)"
    " + COALESCE(octet_length(source_uri), 0)"
)
_DOC_COLS = (
    "document_id, workspace_id, collection_id, title, source_uri, mime_type, content_hash, "
    "byte_size, char_length(text), chunk_count, status, created_at, updated_at, metadata"
)
#: Most distinct query terms the keyword query uses (``extract_query_terms`` already stops at 32;
#: the SQL enforces the same bound on the ``$3`` array).
MAX_KEYWORD_TERMS = 32
#: One document-frequency slot per possible term: ``count(*) FILTER (WHERE fts @@ term_i)``.
_DF_SLOTS = ", ".join(
    f"count(*) FILTER (WHERE c.fts @@ qs.arr[{i}])" for i in range(1, MAX_KEYWORD_TERMS + 1)
)
#: Keyword ranking: one ``phraseto_tsquery('simple', term)`` per distinct query term (from the
#: ``$3`` array parameter, never SQL text). A chunk's score is the sum, over the terms it
#: matches, of the term's bm25-style inverse document frequency in the collection times
#: ``ts_rank_cd`` (``ts_rank_cd`` alone has no IDF, so common words would weigh as much as rare
#: ones).
#:
#: Cost (MVP WP7 review H1). The chunks that match any term are found through the GIN index (the
#: OR of all terms). One pass over them counts every term's document frequency into
#: ``MAX_KEYWORD_TERMS`` fixed slots, one aggregate row. A second pass scores each matching
#: chunk once (a sum over at most 32 terms) and keeps the best ``$4`` in a top-N heap; ``N``
#: comes from an index-only count. No row per (chunk, term) exists, nothing wide is sorted or
#: materialised, and memory does not grow with the collection, so the statement writes no
#: temporary files. CPU is linear in (matching chunks) x (distinct terms, at most 32).
_KEYWORD_SQL = (
    "WITH RECURSIVE terms AS MATERIALIZED ("
    "SELECT row_number() OVER (ORDER BY d.first_pos) AS k, d.q FROM ("
    "SELECT phraseto_tsquery('simple', u.t) AS q, min(u.i) AS first_pos "
    "FROM unnest($3::text[]) WITH ORDINALITY AS u(t, i) "
    f"WHERE u.i <= {MAX_KEYWORD_TERMS} GROUP BY 1) AS d WHERE numnode(d.q) > 0), "
    "qs AS MATERIALIZED (SELECT array_agg(q ORDER BY k) AS arr FROM terms), "
    "folded(k, q) AS (SELECT 0::bigint, ''::tsquery UNION ALL "
    "SELECT t.k, f.q || t.q FROM folded f JOIN terms t ON t.k = f.k + 1), "
    "any_term AS MATERIALIZED (SELECT q FROM folded ORDER BY k DESC LIMIT 1), "
    "scope AS MATERIALIZED (SELECT count(*)::float8 AS n FROM hk_chunks "
    "WHERE workspace_id = $1 AND collection_id = $2), "
    f"df AS MATERIALIZED (SELECT ARRAY[{_DF_SLOTS}] AS n FROM hk_chunks c CROSS JOIN qs "
    "WHERE c.workspace_id = $1 AND c.collection_id = $2 "
    "AND c.fts @@ (SELECT q FROM any_term)), "
    "weighted AS MATERIALIZED (SELECT t.q, "
    "ln(1 + (s.n - df.n[t.k] + 0.5) / (df.n[t.k] + 0.5)) AS idf "
    "FROM terms t CROSS JOIN df CROSS JOIN scope s WHERE df.n[t.k] > 0) "
    "SELECT c.chunk_id FROM hk_chunks c "
    "WHERE c.workspace_id = $1 AND c.collection_id = $2 "
    "AND c.fts @@ (SELECT q FROM any_term) "
    f"ORDER BY (SELECT sum(w.idf * ts_rank_cd(c.fts, w.q, {RANK_NORMALIZATION})) "
    "FROM weighted w WHERE c.fts @@ w.q) DESC, c.seq LIMIT $4"
)
_HITS_SQL = (
    "SELECT c.chunk_id, c.document_id, c.chunk_index, c.text, c.start_char, c.end_char, "
    "d.title, d.source_uri, c.page_start, c.page_end FROM hk_chunks c "
    "JOIN hk_documents d ON d.document_id = c.document_id "
    "WHERE c.workspace_id = $1 AND c.collection_id = $2 AND c.chunk_id = ANY($3::text[])"
)
_STATUS_SQL = (
    "SELECT (SELECT embedder FROM hk_lineage WHERE workspace_id = $1 AND collection_id = $2), "
    "EXISTS (SELECT 1 FROM hk_documents WHERE workspace_id = $1 AND collection_id = $2)"
)
#: Every transaction that changes a collection's chunks bumps its version (MVP WP7 review M1).
_BUMP_VERSION_SQL = (
    "INSERT INTO hk_collection_versions (workspace_id, collection_id, version) "
    "VALUES ($1, $2, 1) ON CONFLICT (workspace_id, collection_id) "
    "DO UPDATE SET version = hk_collection_versions.version + 1"
)
_VERSION_SQL = (
    "SELECT COALESCE((SELECT version FROM hk_collection_versions "
    "WHERE workspace_id = $1 AND collection_id = $2), 0)"
)
#: Chunks per vector-update statement in a reindex commit (each statement has the pool's
#: command timeout; all batches commit together).
REINDEX_BATCH = 500


class InvalidKnowledgeIdError(KnowledgeError):
    """A workspace or collection id that Postgres cannot store (NUL, or too long)."""

    code = "INVALID_COLLECTION_ID"


def quote_ident(name: str) -> str:
    """A Postgres identifier quoted for SQL text (``"`` doubled)."""
    return '"' + name.replace('"', '""') + '"'


def _storable(value: str) -> bool:
    return "\x00" not in value


def _id_ok(workspace_id: str, collection_id: str) -> bool:
    return (
        _storable(workspace_id)
        and _storable(collection_id)
        and len(collection_id) <= MAX_COLLECTION_ID_CHARS
    )


def _require_ids(workspace_id: str, collection_id: str) -> None:
    if not _id_ok(workspace_id, collection_id):
        raise InvalidKnowledgeIdError(
            "Workspace and collection ids cannot contain NUL, and a collection id is at most "
            f"{MAX_COLLECTION_ID_CHARS} characters."
        )


def _no_nul(value: str) -> str:
    return value.replace("\x00", "\ufffd")


def _fold(token: str) -> str:
    """Fold Latin diacritics (``café`` -> ``cafe``), as FTS5 ``unicode61`` does by default."""
    if token.isascii():
        return token
    out = []
    for ch in token:
        if "\u00c0" <= ch <= "\u024f":  # Latin-1 Supplement to Latin Extended-B
            ch = unicodedata.normalize("NFD", ch)[0]
        out.append(ch)
    return "".join(out)


def fts_tokens(text: str) -> list[str]:
    """The words of ``text`` as the keyword index stores them (lower-case, folded)."""
    return [_fold(t) for t in _TERM_RE.findall(text.lower())]


def vector_text(values: list[float]) -> str:
    """A pgvector input literal (``[1.0,2.0]``), sent as a parameter, never as SQL text."""
    if not values:
        raise KnowledgeError("The embedder returned an empty vector.")
    if not all(math.isfinite(v) for v in values):
        raise KnowledgeError("The embedder returned a non-finite value.")
    return "[" + ",".join(repr(float(v)) for v in values) + "]"


class PostgresHybridKnowledgeIndex(KnowledgeIndexBase):
    """The hybrid knowledge index in the shared Postgres ``Database`` (see the module docstring).

    ``database``: a ``nexura_postgres.Database`` (the service's shared pool); the index never
    closes it. Opening the index applies its migrations (component ``knowledge``).
    """

    blocking_io = True

    def __init__(
        self,
        database: Any,
        *,
        embedder: EmbeddingOrchestrator | None = None,
        chunker: BaseChunker | None = None,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        max_workspace_bytes: int = DEFAULT_MAX_WORKSPACE_BYTES,
        max_top_k: int = DEFAULT_MAX_TOP_K,
        max_metadata_bytes: int = DEFAULT_MAX_METADATA_BYTES,
        max_source_uri_chars: int = DEFAULT_MAX_SOURCE_URI_CHARS,
        max_scan: int = DEFAULT_MAX_SCAN,
        migrate: bool = True,
    ) -> None:
        super().__init__(
            embedder=embedder,
            chunker=chunker,
            max_file_bytes=max_file_bytes,
            max_workspace_bytes=max_workspace_bytes,
            max_top_k=max_top_k,
            max_metadata_bytes=max_metadata_bytes,
            max_source_uri_chars=max_source_uri_chars,
            max_scan=max_scan,
        )
        self.database = database
        if migrate:
            components = {COMPONENT: MIGRATIONS}

            async def apply(conn: Any) -> None:
                await migrate_all(conn, components)

            database.run(apply, timeout=MIGRATION_TIMEOUT + 30)

        async def extension_schema(conn: Any) -> Any:
            return await conn.fetchval(
                "SELECT n.nspname FROM pg_extension e "
                "JOIN pg_namespace n ON n.oid = e.extnamespace WHERE e.extname = 'vector'"
            )

        schema = database.run(extension_schema, readonly=True)
        if schema is None:
            raise RuntimeError(
                "The pgvector extension is not installed in this database: run "
                "'nexura-runtime db migrate' with a role allowed to CREATE EXTENSION vector"
            )
        # The type, operator and function are named with the extension's schema (read from the
        # catalog and quoted), so the queries work whatever the search path is.
        ext = quote_ident(str(schema))
        self._vector_cast = f"::{ext}.vector"
        self._vector_sql = (
            "SELECT chunk_id, score FROM (SELECT chunk_id, "
            f"CASE WHEN {ext}.vector_dims(embedding) <> $4 THEN 0::float8 ELSE "
            f"COALESCE(NULLIF(1 - (embedding OPERATOR({ext}.<=>) ($3::text){self._vector_cast}),"
            " 'NaN'::float8), 0::float8) END AS score FROM (SELECT chunk_id, embedding "
            "FROM hk_chunks WHERE workspace_id = $1 AND collection_id = $2 "
            "ORDER BY seq DESC LIMIT $5) AS newest) AS scored WHERE score > $6 "
            'ORDER BY score DESC, chunk_id COLLATE "C" LIMIT $7'
        )
        self._insert_chunks_sql = (
            "INSERT INTO hk_chunks (chunk_id, document_id, workspace_id, collection_id, "
            "chunk_index, text, start_char, end_char, embedding, page_start, page_end, fts) "
            "SELECT u.chunk_id, $1, $2, $3, u.chunk_index, u.text, u.start_char, u.end_char, "
            f"(u.embedding){self._vector_cast}, u.page_start, u.page_end, "
            "to_tsvector('simple', u.tokens) FROM unnest($4::text[], $5::int[], $6::text[], "
            "$7::int[], $8::int[], $9::text[], $10::int[], $11::int[], $12::text[]) "
            "WITH ORDINALITY AS u(chunk_id, chunk_index, text, start_char, end_char, embedding, "
            "page_start, page_end, tokens, ord) ORDER BY u.ord"
        )
        self._reindex_sql = (
            f"UPDATE hk_chunks c SET embedding = (v.embedding){self._vector_cast} "
            "FROM unnest($3::text[], $4::int[], $5::text[]) AS v(document_id, chunk_index, "
            "embedding) WHERE c.workspace_id = $1 AND c.collection_id = $2 "
            "AND c.document_id = v.document_id AND c.chunk_index = v.chunk_index"
        )

    # -- helpers --------------------------------------------------------------------------------

    def _storable_text(self, text: str) -> str:
        return _no_nul(text)

    def _run(self, fn: Any, *, readonly: bool = False) -> Any:
        return self.database.run(fn, readonly=readonly)

    async def _arun(self, fn: Any, *, isolation: str | None = None, readonly: bool = False) -> Any:
        return await self.database.arun(fn, isolation=isolation, readonly=readonly)

    async def _status(self, conn: Any, workspace_id: str, collection_id: str) -> dict[str, Any]:
        row = await conn.fetchrow(_STATUS_SQL, workspace_id, collection_id)
        return self._status_dict(row[0], bool(row[1]))

    async def _check_lineage_tx(self, conn: Any, workspace_id: str, collection_id: str) -> None:
        if (await self._status(conn, workspace_id, collection_id))["requires_reindex"]:
            raise self._lineage_error()

    async def _lock_collection(self, conn: Any, workspace_id: str, collection_id: str) -> None:
        await conn.execute(COLLECTION_LOCK_SQL, workspace_id, collection_id)

    async def _write_lineage(self, conn: Any, workspace_id: str, collection_id: str) -> None:
        await conn.execute(
            "INSERT INTO hk_lineage (workspace_id, collection_id, embedder) VALUES ($1, $2, $3) "
            "ON CONFLICT (workspace_id, collection_id) DO UPDATE SET embedder = EXCLUDED.embedder",
            workspace_id,
            collection_id,
            self._lineage_json(),
        )

    @staticmethod
    def _row_to_doc(row: Any) -> IndexedDocument:
        return IndexedDocument(
            document_id=row[0],
            workspace_id=row[1],
            collection_id=row[2],
            title=row[3],
            source_uri=row[4],
            mime_type=row[5],
            content_hash=row[6],
            byte_size=row[7],
            text_length=row[8],
            chunk_count=row[9],
            status=row[10],
            created_at=row[11],
            updated_at=row[12],
            metadata=json.loads(row[13]),
        )

    @staticmethod
    async def _find_duplicate(
        conn: Any, workspace_id: str, collection_id: str, title: str, content_hash: str
    ) -> Any:
        """The same-title document if its content is equal, else any same-content document.

        A same-title document always wins: re-uploading changed content replaces it.
        """
        titled = await conn.fetchrow(
            f"SELECT {_DOC_COLS} FROM hk_documents WHERE workspace_id = $1 "
            "AND collection_id = $2 AND md5(title) = md5($3) AND title = $3",
            workspace_id,
            collection_id,
            title,
        )
        if titled is not None:
            return titled if titled[6] == content_hash else None
        return await conn.fetchrow(
            f"SELECT {_DOC_COLS} FROM hk_documents WHERE workspace_id = $1 "
            "AND collection_id = $2 AND content_hash = $3 ORDER BY created_at, document_id "
            'COLLATE "C" LIMIT 1',
            workspace_id,
            collection_id,
            content_hash,
        )

    @staticmethod
    async def _title_doc(conn: Any, workspace_id: str, collection_id: str, title: str) -> Any:
        return await conn.fetchrow(
            f"SELECT document_id, created_at, {_SIZE_SQL} FROM hk_documents "
            "WHERE workspace_id = $1 AND collection_id = $2 AND md5(title) = md5($3) "
            "AND title = $3",
            workspace_id,
            collection_id,
            title,
        )

    async def _check_quota(
        self, conn: Any, workspace_id: str, replaced_bytes: int, size: int
    ) -> None:
        used = await conn.fetchval(
            f"SELECT COALESCE(SUM({_SIZE_SQL}), 0)::bigint FROM hk_documents "
            "WHERE workspace_id = $1",
            workspace_id,
        )
        if int(used) - replaced_bytes + size > self.max_workspace_bytes:
            raise WorkspaceQuotaExceededError(
                f"Workspace storage limit of {self.max_workspace_bytes} bytes would be exceeded."
            )

    async def _dedup_tx(
        self,
        conn: Any,
        workspace_id: str,
        collection_id: str,
        title: str,
        content_hash: str,
        source_uri: str | None,
        metadata: dict[str, Any] | None,
    ) -> IngestOutcome | None:
        """Dedup hit inside a locked transaction (None when no duplicate exists any more)."""
        await self._check_lineage_tx(conn, workspace_id, collection_id)
        row = await self._find_duplicate(conn, workspace_id, collection_id, title, content_hash)
        if row is None:
            return None
        doc = self._row_to_doc(row)
        new_uri = source_uri if source_uri is not None else doc.source_uri
        new_meta = metadata if metadata is not None else doc.metadata
        if new_uri == doc.source_uri and new_meta == doc.metadata:
            return IngestOutcome(document=doc, deduplicated=True)
        meta_json = json.dumps(new_meta, ensure_ascii=False, default=str)
        current = await conn.fetchrow(
            f"SELECT text, {_SIZE_SQL} FROM hk_documents WHERE document_id = $1",
            doc.document_id,
        )
        await self._check_quota(
            conn,
            workspace_id,
            int(current[1]),
            _stored_size(current[0], doc.title, new_uri, meta_json),
        )
        fresh = await conn.fetchrow(
            "UPDATE hk_documents SET source_uri = $2, metadata = $3, updated_at = $4 "
            f"WHERE document_id = $1 RETURNING {_DOC_COLS}",
            doc.document_id,
            new_uri,
            meta_json,
            time.time(),
        )
        return IngestOutcome(
            document=self._row_to_doc(fresh), deduplicated=True, metadata_updated=True
        )

    async def _lock_for_ingest(self, conn: Any, workspace_id: str, collection_id: str) -> None:
        # Lock order: workspace, then collection. Reindex and delete take only the collection
        # lock, so no path waits for a workspace lock while holding a collection lock.
        await conn.execute(WORKSPACE_LOCK_SQL, workspace_id)
        await self._lock_collection(conn, workspace_id, collection_id)

    # -- lineage and limits ---------------------------------------------------------------------

    def embedding_status(self, workspace_id: str, collection_id: str = "default") -> dict[str, Any]:
        """Configured and indexed embedder identities (see ``HybridKnowledgeIndex``)."""
        if not (_storable(workspace_id) and _storable(collection_id)):
            return self._status_dict(None, False)

        async def run(conn: Any) -> dict[str, Any]:
            return await self._status(conn, workspace_id, collection_id)

        status: dict[str, Any] = self._run(run, readonly=True)
        return status

    def workspace_usage(self, workspace_id: str) -> int:
        """Bytes (UTF-8, ``octet_length``) stored by ``workspace_id``: text, metadata JSON,
        source URI and title."""
        if not _storable(workspace_id):
            return 0

        async def run(conn: Any) -> int:
            return int(
                await conn.fetchval(
                    f"SELECT COALESCE(SUM({_SIZE_SQL}), 0)::bigint FROM hk_documents "
                    "WHERE workspace_id = $1",
                    workspace_id,
                )
            )

        usage: int = self._run(run, readonly=True)
        return usage

    def has_documents(self, workspace_id: str) -> bool:
        """Whether ``workspace_id`` has stored at least one document."""
        if not _storable(workspace_id):
            return False

        async def run(conn: Any) -> bool:
            return bool(
                await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM hk_documents WHERE workspace_id = $1)",
                    workspace_id,
                )
            )

        found: bool = self._run(run, readonly=True)
        return found

    async def reindex(self, workspace_id: str, collection_id: str = "default") -> dict[str, Any]:
        """Replace all collection vectors and lineage in one commit, keeping text and citations.

        A provider failure leaves the old index intact. A change to the collection between the
        snapshot and the commit (on any instance) refuses the replacement; retry against the new
        snapshot. Readers see the old vectors and lineage or the new ones, never a mix.
        """
        _require_ids(workspace_id, collection_id)

        async def snapshot(conn: Any) -> tuple[list[Any], Any, dict[str, Any]]:
            rows = await conn.fetch(
                "SELECT document_id, chunk_index, text FROM hk_chunks WHERE workspace_id = $1 "
                'AND collection_id = $2 ORDER BY document_id COLLATE "C", chunk_index',
                workspace_id,
                collection_id,
            )
            version = await conn.fetchval(_VERSION_SQL, workspace_id, collection_id)
            status = await self._status(conn, workspace_id, collection_id)
            return list(rows), version, status

        before, version, old_status = await self._arun(
            snapshot, isolation="repeatable_read", readonly=True
        )
        result = await self.embedder.embed_texts([r[2] for r in before])
        vectors = [
            vector_text(chunk.embedding)
            for _, chunk in zip(before, result.embedded_chunks, strict=True)
        ]

        async def commit(conn: Any) -> dict[str, Any]:
            await self._lock_collection(conn, workspace_id, collection_id)
            # O(1) under the lock: every chunk change bumps the version in its own transaction.
            now_version = await conn.fetchval(_VERSION_SQL, workspace_id, collection_id)
            if (
                now_version != version
                or await self._status(conn, workspace_id, collection_id) != old_status
            ):
                raise EmbeddingLineageMismatchError(
                    "Collection changed during reindex; retry reindex."
                )
            for start in range(0, len(before), REINDEX_BATCH):
                batch = before[start : start + REINDEX_BATCH]
                await conn.execute(
                    self._reindex_sql,
                    workspace_id,
                    collection_id,
                    [r[0] for r in batch],
                    [r[1] for r in batch],
                    vectors[start : start + REINDEX_BATCH],
                )
            await conn.execute(_BUMP_VERSION_SQL, workspace_id, collection_id)
            await self._write_lineage(conn, workspace_id, collection_id)
            return await self._status(conn, workspace_id, collection_id)

        status = await self._arun(commit)
        return {"reindexed_chunks": len(before), **status}

    # -- ingestion ------------------------------------------------------------------------------

    async def ingest(
        self,
        *,
        workspace_id: str,
        filename: str,
        data: bytes,
        collection_id: str = "default",
        mime_type: str | None = None,
        source_uri: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> IngestOutcome:
        """Parse, chunk, embed and index ``data``; dedup by hash, replace same-title documents."""
        _require_ids(workspace_id, collection_id)
        title = _no_nul(filename)
        uri = None if source_uri is None else _no_nul(source_uri)

        async def first_look(conn: Any) -> Any:
            await self._check_lineage_tx(conn, workspace_id, collection_id)
            return await self._find_duplicate(
                conn, workspace_id, collection_id, title, content_hash
            )

        # The lineage is checked before the limits, as on SQLite.
        async def lineage(conn: Any) -> None:
            await self._check_lineage_tx(conn, workspace_id, collection_id)

        await self._arun(lineage, readonly=True)
        meta_json, content_hash = self._check_upload(data, metadata, uri)
        if await self._arun(first_look, readonly=True) is not None:

            async def dedup(conn: Any) -> IngestOutcome | None:
                await self._lock_for_ingest(conn, workspace_id, collection_id)
                return await self._dedup_tx(
                    conn, workspace_id, collection_id, title, content_hash, uri, metadata
                )

            deduped: IngestOutcome | None = await self._arun(dedup)
            if deduped is not None:
                return deduped
            # The duplicate was deleted meanwhile: ingest as new.

        text, mime, pages = self._extract(data, filename, mime_type)
        new_size = _stored_size(text, title, uri, meta_json)

        async def precheck(conn: Any) -> None:
            old = await self._title_doc(conn, workspace_id, collection_id, title)
            await self._check_quota(conn, workspace_id, int(old[2]) if old else 0, new_size)

        await self._arun(precheck, readonly=True)

        doc_id = f"doc_{uuid.uuid4().hex[:12]}"
        chunks = self.chunker.chunk_text(text, document_id=doc_id)
        embedded = await self.embedder.embed_chunks(chunks)
        vectors = {e.chunk_id: vector_text(e.embedding) for e in embedded.embedded_chunks}
        now = time.time()
        spans = [_chunk_pages(pages, c.start_char, c.end_char) for c in chunks]

        async def commit(conn: Any) -> IngestOutcome:
            await self._lock_for_ingest(conn, workspace_id, collection_id)
            # Re-check under the locks: another ingest may have committed while embedding.
            await self._check_lineage_tx(conn, workspace_id, collection_id)
            dup = await self._dedup_tx(
                conn, workspace_id, collection_id, title, content_hash, uri, metadata
            )
            if dup is not None:
                return dup
            old = await self._title_doc(conn, workspace_id, collection_id, title)
            await self._check_quota(conn, workspace_id, int(old[2]) if old else 0, new_size)
            final_id = old[0] if old else doc_id
            created = old[1] if old else now
            if old:
                await conn.execute("DELETE FROM hk_chunks WHERE document_id = $1", final_id)
            await self._write_lineage(conn, workspace_id, collection_id)
            await conn.execute(_BUMP_VERSION_SQL, workspace_id, collection_id)
            if chunks:
                await conn.execute(
                    self._insert_chunks_sql,
                    final_id,
                    workspace_id,
                    collection_id,
                    [c.chunk_id for c in chunks],
                    [c.chunk_index for c in chunks],
                    [c.text for c in chunks],
                    [c.start_char for c in chunks],
                    [c.end_char for c in chunks],
                    [vectors[c.chunk_id] for c in chunks],
                    [s[0] for s in spans],
                    [s[1] for s in spans],
                    [" ".join(fts_tokens(c.text)) for c in chunks],
                )
            row = await conn.fetchrow(
                "INSERT INTO hk_documents (document_id, workspace_id, collection_id, title, "
                "source_uri, mime_type, content_hash, byte_size, text, chunk_count, status, "
                "created_at, updated_at, metadata) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'indexed',$11,$12,$13) "
                "ON CONFLICT (document_id) DO UPDATE SET workspace_id = EXCLUDED.workspace_id, "
                "collection_id = EXCLUDED.collection_id, title = EXCLUDED.title, "
                "source_uri = EXCLUDED.source_uri, mime_type = EXCLUDED.mime_type, "
                "content_hash = EXCLUDED.content_hash, byte_size = EXCLUDED.byte_size, "
                "text = EXCLUDED.text, chunk_count = EXCLUDED.chunk_count, "
                "status = EXCLUDED.status, created_at = EXCLUDED.created_at, "
                "updated_at = EXCLUDED.updated_at, metadata = EXCLUDED.metadata "
                f"RETURNING {_DOC_COLS}",
                final_id,
                workspace_id,
                collection_id,
                title,
                uri,
                mime,
                content_hash,
                len(data),
                text,
                len(chunks),
                created,
                now,
                meta_json,
            )
            return IngestOutcome(document=self._row_to_doc(row), replaced=old is not None)

        outcome: IngestOutcome = await self._arun(commit)
        return outcome

    # -- documents ------------------------------------------------------------------------------

    def list_documents(
        self, workspace_id: str, collection_id: str | None = "default"
    ) -> list[IndexedDocument]:
        """Documents of a workspace (one collection, or all when ``collection_id`` is None)."""
        if not _storable(workspace_id) or (
            collection_id is not None and not _storable(collection_id)
        ):
            return []

        async def run(conn: Any) -> list[Any]:
            if collection_id is None:
                return list(
                    await conn.fetch(
                        f"SELECT {_DOC_COLS} FROM hk_documents WHERE workspace_id = $1 "
                        'ORDER BY created_at, document_id COLLATE "C"',
                        workspace_id,
                    )
                )
            return list(
                await conn.fetch(
                    f"SELECT {_DOC_COLS} FROM hk_documents WHERE workspace_id = $1 "
                    'AND collection_id = $2 ORDER BY created_at, document_id COLLATE "C"',
                    workspace_id,
                    collection_id,
                )
            )

        return [self._row_to_doc(r) for r in self._run(run, readonly=True)]

    def get_document(self, workspace_id: str, document_id: str) -> IndexedDocument | None:
        """One document of the workspace, or None (also None for another workspace's document)."""
        if not (_storable(workspace_id) and _storable(document_id)):
            return None

        async def run(conn: Any) -> Any:
            return await conn.fetchrow(
                f"SELECT {_DOC_COLS} FROM hk_documents "
                "WHERE workspace_id = $1 AND document_id = $2",
                workspace_id,
                document_id,
            )

        row = self._run(run, readonly=True)
        return self._row_to_doc(row) if row else None

    def get_document_text(self, workspace_id: str, document_id: str) -> str | None:
        """Normalized text of a workspace document, or None."""
        if not (_storable(workspace_id) and _storable(document_id)):
            return None

        async def run(conn: Any) -> Any:
            return await conn.fetchval(
                "SELECT text FROM hk_documents WHERE workspace_id = $1 AND document_id = $2",
                workspace_id,
                document_id,
            )

        text = self._run(run, readonly=True)
        return None if text is None else str(text)

    def get_chunk(
        self, workspace_id: str, document_id: str, chunk_index: int
    ) -> dict[str, Any] | None:
        """One chunk with its citation fields (used by footnote links), or None."""
        if not (_storable(workspace_id) and _storable(document_id)):
            return None
        if not -(2**31) <= int(chunk_index) < 2**31:
            return None

        async def run(conn: Any) -> Any:
            return await conn.fetchrow(
                "SELECT c.chunk_id, c.text, c.start_char, c.end_char, d.title, d.source_uri "
                "FROM hk_chunks c JOIN hk_documents d ON d.document_id = c.document_id "
                "WHERE c.workspace_id = $1 AND c.document_id = $2 AND c.chunk_index = $3",
                workspace_id,
                document_id,
                int(chunk_index),
            )

        row = self._run(run, readonly=True)
        if row is None:
            return None
        return {
            "chunk_id": row[0],
            "document_id": document_id,
            "chunk_index": chunk_index,
            "text": row[1],
            "start_char": row[2],
            "end_char": row[3],
            "title": row[4],
            "source_uri": row[5],
        }

    def delete_document(self, workspace_id: str, document_id: str) -> bool:
        """Remove a document, its chunks, vectors and keyword entries atomically (it waits for
        the collection lock, so it never interleaves with an ingest or reindex there)."""
        if not (_storable(workspace_id) and _storable(document_id)):
            return False

        async def run(conn: Any) -> bool:
            collection = await conn.fetchval(
                "SELECT collection_id FROM hk_documents "
                "WHERE workspace_id = $1 AND document_id = $2",
                workspace_id,
                document_id,
            )
            if collection is None:
                return False
            await self._lock_collection(conn, workspace_id, collection)
            gone = await conn.fetchval(
                "DELETE FROM hk_documents WHERE workspace_id = $1 AND document_id = $2 RETURNING 1",
                workspace_id,
                document_id,
            )
            if gone is None:
                return False  # deleted by another caller while this one waited
            await conn.execute("DELETE FROM hk_chunks WHERE document_id = $1", document_id)
            await conn.execute(_BUMP_VERSION_SQL, workspace_id, collection)
            return True

        deleted: bool = self._run(run)
        return deleted

    def delete_collection(self, workspace_id: str, collection_id: str) -> list[str]:
        """Remove every document of one collection, its chunks, vectors, keyword entries and
        embedding lineage in one transaction, under the collection lock (never interleaved
        with an ingest, reindex or delete there); returns the deleted document ids."""
        if not (_storable(workspace_id) and _storable(collection_id)):
            return []

        async def run(conn: Any) -> list[str]:
            await self._lock_collection(conn, workspace_id, collection_id)
            rows = await conn.fetch(
                "DELETE FROM hk_documents WHERE workspace_id = $1 AND collection_id = $2 "
                "RETURNING document_id",
                workspace_id,
                collection_id,
            )
            await conn.execute(
                "DELETE FROM hk_chunks WHERE workspace_id = $1 AND collection_id = $2",
                workspace_id,
                collection_id,
            )
            await conn.execute(
                "DELETE FROM hk_lineage WHERE workspace_id = $1 AND collection_id = $2",
                workspace_id,
                collection_id,
            )
            await conn.execute(_BUMP_VERSION_SQL, workspace_id, collection_id)
            return sorted(str(r[0]) for r in rows)

        deleted: list[str] = self._run(run)
        return deleted

    # -- search ---------------------------------------------------------------------------------

    async def search(
        self,
        *,
        workspace_id: str,
        query: str,
        collection_id: str = "default",
        top_k: int = 5,
        vector_weight: float = 1.0,
        keyword_weight: float = 1.0,
        rrf_k: int = 60,
        min_vector_score: float = DEFAULT_MIN_VECTOR_SCORE,
        min_score: float = 0.0,
    ) -> list[SearchHit]:
        """Hybrid search in one workspace and collection, capped at ``max_top_k`` hits.

        Same scoring as ``HybridKnowledgeIndex.search`` except the keyword ranker
        (``ts_rank_cd`` instead of ``bm25``). Everything after the query embedding is read in
        one ``REPEATABLE READ`` snapshot.
        """
        top_k = max(1, min(int(top_k), self.max_top_k))
        if not (_storable(workspace_id) and _storable(collection_id)):
            return []

        async def lineage(conn: Any) -> None:
            await self._check_lineage_tx(conn, workspace_id, collection_id)

        await self._arun(lineage, readonly=True)
        terms = extract_query_terms(query)
        if not terms:
            return []
        pool = max(top_k * 4, 20)
        qvec: str | None = None
        dim = 0
        if vector_weight > 0:
            embedding = (
                (await self.embedder.embed_texts([" ".join(terms)])).embedded_chunks[0].embedding
            )
            qvec, dim = vector_text(embedding), len(embedding)
        folded = [_fold(t) for t in terms]

        async def read(conn: Any) -> list[SearchHit]:
            await self._check_lineage_tx(conn, workspace_id, collection_id)
            keyword_ids: list[str] = []
            if keyword_weight > 0:
                rows = await conn.fetch(_KEYWORD_SQL, workspace_id, collection_id, folded, pool)
                keyword_ids = [r[0] for r in rows]
            vector_ids: list[str] = []
            vector_scores: dict[str, float] = {}
            if qvec is not None:
                rows = await conn.fetch(
                    self._vector_sql,
                    workspace_id,
                    collection_id,
                    qvec,
                    dim,
                    self.max_scan,
                    float(min_vector_score),
                    pool,
                )
                for r in rows:
                    vector_ids.append(r[0])
                    vector_scores[r[0]] = float(r[1])
            ordered, fused = fuse_rankings(
                vector_ids=vector_ids,
                vector_scores=vector_scores,
                keyword_ids=keyword_ids,
                vector_weight=vector_weight,
                keyword_weight=keyword_weight,
                rrf_k=rrf_k,
                min_score=min_score,
                top_k=top_k,
            )
            if not ordered:
                return []
            rows = await conn.fetch(_HITS_SQL, workspace_id, collection_id, ordered)
            return build_hits(
                ordered,
                rows,
                terms=terms,
                fused=fused,
                vector_ids=vector_ids,
                keyword_ids=keyword_ids,
                vector_scores=vector_scores,
            )

        hits: list[SearchHit] = await self._arun(read, isolation="repeatable_read", readonly=True)
        return hits
