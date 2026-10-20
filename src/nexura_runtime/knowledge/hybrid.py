# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Workspace-scoped hybrid knowledge index: vector similarity plus SQLite FTS5 keyword search.

``HybridKnowledgeIndex`` ingests files (text, Markdown, HTML, PDF), chunks them, embeds the chunks
with an ``EmbeddingOrchestrator`` and stores chunks, vectors and an FTS5 keyword index in one
SQLite database. Search ranks candidates with a vector ranker and a BM25 keyword ranker and fuses
them with weighted reciprocal rank fusion. Every hit carries a citation (document id, title, chunk
index, character span, source URI). The default embedder is deterministic and NOT semantic, so
keyword matching carries most of the relevance signal unless a real embedder is supplied.
"""

from __future__ import annotations

import array
import hashlib
import json
import math
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any, cast

try:  # numpy is optional: it only makes the vector scan faster
    import numpy as _np
except ImportError:  # pragma: no cover - exercised when numpy is absent
    _np = None  # type: ignore[assignment]

from nexura_agents.tools import Tool
from pydantic import BaseModel, Field

from nexura_runtime.knowledge.chunks.chunkers import BaseChunker, FixedWindowChunker
from nexura_runtime.knowledge.embeddings.config import describe_embedder
from nexura_runtime.knowledge.embeddings.embedder import EmbeddingOrchestrator
from nexura_runtime.knowledge.ingest.detector import detect_mime_type
from nexura_runtime.knowledge.ingest.errors import IngestionError
from nexura_runtime.knowledge.ingest.parsers import get_parser_for_mime

DEFAULT_MAX_FILE_BYTES = 5_000_000
DEFAULT_MAX_WORKSPACE_BYTES = 50_000_000
DEFAULT_MAX_TOP_K = 50
DEFAULT_MAX_METADATA_BYTES = 1_048_576  # JSON-encoded metadata per document
DEFAULT_MAX_SOURCE_URI_CHARS = 131_072  # source_uri length per document
DEFAULT_MAX_SCAN = 20_000  # newest chunks scored per vector search
DEFAULT_MIN_VECTOR_SCORE = 0.05  # cosine floor: drops hash-collision noise from the vector ranker
_MAX_QUERY_TERMS = 32
_MAX_TERM_LEN = 256  # indexing (FTS5) is not truncated, so queries may use tokens up to this length
_MAX_QUERY_CHARS = 2000
_SNIPPET_CHARS = 500
_ARCHIVE_MIMES = frozenset({"application/zip", "application/gzip", "application/x-zip-compressed"})
_TERM_RE = re.compile(r"[\wऀ-ॿ]+", re.UNICODE)


class KnowledgeError(Exception):
    """Base error for hybrid knowledge operations; ``code`` is a stable machine-readable string."""

    code = "KNOWLEDGE_ERROR"


class FileTooLargeError(KnowledgeError):
    """Raised when one file exceeds the per-file size limit."""

    code = "OVERSIZED_DOCUMENT"


class EmbeddingLineageMismatchError(KnowledgeError):
    """A collection must be explicitly reindexed before this embedder can use it."""

    code = "EMBEDDING_LINEAGE_MISMATCH"


class WorkspaceQuotaExceededError(KnowledgeError):
    """Raised when ingesting would push a workspace over its total size limit."""

    code = "WORKSPACE_QUOTA_EXCEEDED"


class DocumentLimitError(KnowledgeError):
    """Raised when a document's metadata or source URI exceeds its per-document limit."""

    code = "DOCUMENT_FIELD_TOO_LARGE"


class EmptyDocumentError(KnowledgeError):
    """Raised when a file has no extractable text."""

    code = "EMPTY_DOCUMENT"


class DocumentParseError(KnowledgeError):
    """Raised when a file cannot be parsed (unsupported, encrypted, malformed)."""

    code = "DOCUMENT_PARSE_FAILED"


class HybridCitation(BaseModel):
    """Verifiable pointer from a search hit back to its source chunk."""

    citation_id: str
    document_id: str
    title: str
    chunk_index: int
    chunk_id: str
    start_char: int
    end_char: int
    source_uri: str | None = None
    page_start: int | None = None  # only set when the parser knew the page (PDF)
    page_end: int | None = None


class SearchHit(BaseModel):
    """One ranked search result with fused score, per-ranker ranks, snippet and citation."""

    chunk_id: str
    document_id: str
    title: str
    chunk_index: int
    text: str
    snippet: str
    highlights: list[tuple[int, int]] = Field(default_factory=list)  # UTF-16 code units
    score: float
    highlights_cp: list[tuple[int, int]] = Field(default_factory=list)
    vector_rank: int | None = None
    keyword_rank: int | None = None
    vector_score: float | None = None
    matched_by: list[str] = Field(default_factory=list)
    citation: HybridCitation


class IndexedDocument(BaseModel):
    """Stored document record (status is always ``indexed`` once ingest succeeds)."""

    document_id: str
    workspace_id: str
    collection_id: str
    title: str
    source_uri: str | None = None
    mime_type: str
    content_hash: str
    byte_size: int
    text_length: int
    chunk_count: int
    status: str = "indexed"
    created_at: float
    updated_at: float
    metadata: dict[str, Any] = Field(default_factory=dict)


class IngestOutcome(BaseModel):
    """Result of an ingest call: the document plus whether it was new, deduplicated or replaced."""

    document: IndexedDocument
    deduplicated: bool = False
    replaced: bool = False
    metadata_updated: bool = False  # identical content re-ingested with new source_uri/metadata


def extract_query_terms(query: str) -> list[str]:
    """Split free text into safe lowercase search terms (no FTS syntax can survive this)."""
    terms: list[str] = []
    seen: set[str] = set()
    for raw in _TERM_RE.findall(query[:_MAX_QUERY_CHARS]):
        term = raw.lower()[:_MAX_TERM_LEN]
        if term and term not in seen:
            seen.add(term)
            terms.append(term)
        if len(terms) >= _MAX_QUERY_TERMS:
            break
    return terms


def _fts_expression(terms: list[str]) -> str:
    """Quote every term as an FTS5 string literal and OR them together."""
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)


def _pack(vector: list[float]) -> bytes:
    """Pack an embedding as a little-endian-native float32 blob (compact, no JSON decoding)."""
    return array.array("f", vector).tobytes()


def _unpack(blob: bytes | str) -> list[float]:
    """Decode a stored embedding (float32 blob, or legacy JSON text from older databases)."""
    if isinstance(blob, str):
        return [float(x) for x in json.loads(blob)]
    values = array.array("f")
    values.frombytes(blob)
    return list(values)


def _cosine_scores(qvec: list[float], blobs: list[bytes | str]) -> list[float]:
    """Cosine of ``qvec`` against every stored embedding (0.0 on dimension mismatch)."""
    dim = len(qvec)
    scores = [0.0] * len(blobs)
    fast = [i for i, b in enumerate(blobs) if isinstance(b, bytes) and len(b) == dim * 4]
    for i, b in enumerate(blobs):
        if not (isinstance(b, bytes) and len(b) == dim * 4):
            vec = _unpack(b)
            scores[i] = _cosine_py(qvec, vec) if len(vec) == dim else 0.0
    if not fast:
        return scores
    if _np is not None:
        matrix = _np.frombuffer(
            b"".join(cast(bytes, blobs[i]) for i in fast), dtype=_np.float32
        ).reshape(len(fast), dim)
        q = _np.asarray(qvec, dtype=_np.float32)
        norms = _np.linalg.norm(matrix, axis=1) * float(_np.linalg.norm(q))
        dots = matrix @ q
        out = _np.divide(dots, norms, out=_np.zeros_like(dots), where=norms > 0)
        for i, v in zip(fast, out.tolist(), strict=True):
            scores[i] = v
    else:
        for i in fast:
            scores[i] = _cosine_py(qvec, _unpack(blobs[i]))
    return scores


def _cosine_py(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _chunk_pages(
    pages: list[tuple[int, int, int]], start: int, end: int
) -> tuple[int | None, int | None]:
    """First and last page overlapping ``[start, end)``; (None, None) when pages are unknown."""
    hit = [n for n, lo, hi in pages if not (end <= lo or start >= hi)]
    return (min(hit), max(hit)) if hit else (None, None)


def _highlights(text: str, terms: list[str], limit: int = 20) -> list[tuple[int, int]]:
    """Character spans of query terms inside ``text`` (case-insensitive, non-overlapping)."""
    if not terms:
        return []
    pattern = re.compile("|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True)), re.I)
    return [(m.start(), m.end()) for m in pattern.finditer(text)][:limit]


def _utf16_offset(text: str, index: int) -> int:
    """Convert a code-point index of ``text`` into a UTF-16 code-unit offset (JS string index)."""
    return index + sum(1 for ch in text[:index] if ord(ch) > 0xFFFF)


def _to_utf16_spans(text: str, spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Re-express code-point spans of ``text`` as UTF-16 code-unit spans (never split pairs)."""
    return [(_utf16_offset(text, s), _utf16_offset(text, e)) for s, e in spans]


def _make_snippet(text: str, spans: list[tuple[int, int]]) -> tuple[str, list[tuple[int, int]]]:
    """Window of at most ``_SNIPPET_CHARS`` around the first match, with spans re-based to it."""
    if len(text) <= _SNIPPET_CHARS:
        return text, spans
    start = max(0, (spans[0][0] - 80) if spans else 0)
    end = min(len(text), start + _SNIPPET_CHARS)
    rebased = [(s - start, e - start) for s, e in spans if s >= start and e <= end]
    return text[start:end], rebased


def weighted_rrf(
    rankings: dict[str, list[str]], weights: dict[str, float], rrf_k: int = 60
) -> dict[str, float]:
    """Fuse ranked id lists: ``score(id) = sum(weight[r] / (rrf_k + rank_r(id)))`` (rank from 1)."""
    fused: dict[str, float] = {}
    for name, ids in rankings.items():
        weight = weights.get(name, 1.0)
        for rank, item in enumerate(ids, start=1):
            fused[item] = fused.get(item, 0.0) + weight / (rrf_k + rank)
    return fused


_SIZE_SQL = (
    "LENGTH(CAST(text AS BLOB)) + LENGTH(CAST(metadata AS BLOB)) + LENGTH(CAST(title AS BLOB))"
    " + COALESCE(LENGTH(CAST(source_uri AS BLOB)), 0)"
)


def _stored_size(text: str, title: str, source_uri: str | None, meta_json: str) -> int:
    """Quota bytes of one document, matching ``_SIZE_SQL``."""
    return sum(len(x.encode("utf-8")) for x in (text, title, meta_json)) + (
        len(source_uri.encode("utf-8")) if source_uri else 0
    )


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS hk_lineage (
        workspace_id TEXT NOT NULL, collection_id TEXT NOT NULL, embedder TEXT NOT NULL,
        PRIMARY KEY (workspace_id, collection_id))""",
    """CREATE TABLE IF NOT EXISTS hk_documents (
        document_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, collection_id TEXT NOT NULL,
        title TEXT NOT NULL, source_uri TEXT, mime_type TEXT NOT NULL, content_hash TEXT NOT NULL,
        byte_size INTEGER NOT NULL, text TEXT NOT NULL, chunk_count INTEGER NOT NULL,
        status TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
        metadata TEXT NOT NULL DEFAULT '{}')""",
    "CREATE INDEX IF NOT EXISTS hk_documents_scope ON hk_documents(workspace_id, collection_id)",
    """CREATE TABLE IF NOT EXISTS hk_chunks (
        chunk_id TEXT NOT NULL, document_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
        collection_id TEXT NOT NULL, chunk_index INTEGER NOT NULL, text TEXT NOT NULL,
        start_char INTEGER NOT NULL, end_char INTEGER NOT NULL, embedding BLOB NOT NULL,
        page_start INTEGER, page_end INTEGER,
        PRIMARY KEY (document_id, chunk_index))""",
    "CREATE INDEX IF NOT EXISTS hk_chunks_scope ON hk_chunks(workspace_id, collection_id)",
    """CREATE VIRTUAL TABLE IF NOT EXISTS hk_fts USING fts5(
        text, chunk_id UNINDEXED, document_id UNINDEXED, workspace_id UNINDEXED,
        collection_id UNINDEXED, tokenize='unicode61')""",
)


class HybridKnowledgeIndex:
    """SQLite-backed, workspace-scoped hybrid (vector + FTS5) index with citations and limits."""

    def __init__(
        self,
        connection: sqlite3.Connection | None = None,
        *,
        embedder: EmbeddingOrchestrator | None = None,
        chunker: BaseChunker | None = None,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        max_workspace_bytes: int = DEFAULT_MAX_WORKSPACE_BYTES,
        max_top_k: int = DEFAULT_MAX_TOP_K,
        max_metadata_bytes: int = DEFAULT_MAX_METADATA_BYTES,
        max_source_uri_chars: int = DEFAULT_MAX_SOURCE_URI_CHARS,
        max_scan: int = DEFAULT_MAX_SCAN,
    ) -> None:
        """Create the index on ``connection`` (default: a private in-memory database)."""
        self._conn = connection or sqlite3.connect(":memory:", check_same_thread=False)
        self.embedder = embedder or EmbeddingOrchestrator()
        self.chunker: BaseChunker = chunker or FixedWindowChunker(chunk_size=400, chunk_overlap=40)
        self.max_file_bytes = max_file_bytes
        self.max_workspace_bytes = max_workspace_bytes
        self.max_top_k = max_top_k
        self.max_metadata_bytes = max_metadata_bytes
        self.max_source_uri_chars = max_source_uri_chars
        self.max_scan = max_scan
        self._lock = threading.RLock()
        with self._lock:
            for stmt in _SCHEMA:
                self._conn.execute(stmt)
            columns = {r[1] for r in self._conn.execute("PRAGMA table_info(hk_chunks)")}
            for col in ("page_start", "page_end"):  # databases created before page tracking
                if col not in columns:
                    self._conn.execute(f"ALTER TABLE hk_chunks ADD COLUMN {col} INTEGER")
            self._conn.commit()

    # -- limits ---------------------------------------------------------------------------------

    def embedding_status(self, workspace_id: str, collection_id: str = "default") -> dict[str, Any]:
        """Expose configured and indexed identities, including unverified legacy collections."""
        configured = describe_embedder(self.embedder)
        with self._lock:
            row = self._conn.execute(
                "SELECT embedder FROM hk_lineage WHERE workspace_id = ? AND collection_id = ?",
                (workspace_id, collection_id),
            ).fetchone()
            populated = self._conn.execute(
                "SELECT 1 FROM hk_documents WHERE workspace_id = ? AND collection_id = ? LIMIT 1",
                (workspace_id, collection_id),
            ).fetchone() is not None
        indexed = json.loads(row[0]) if row else None
        return {
            "embedder": configured,
            "indexed_embedder": indexed,
            "is_test_fixture": configured["is_test_fixture"],
            "requires_reindex": (indexed != configured) if row else populated,
        }

    def _check_lineage(self, workspace_id: str, collection_id: str) -> None:
        if self.embedding_status(workspace_id, collection_id)["requires_reindex"]:
            raise EmbeddingLineageMismatchError(
                "Collection embedding lineage is different or unknown. Explicitly reindex "
                "with POST /knowledge/collections/{collection_id}/reindex before use."
            )

    def _write_lineage(self, workspace_id: str, collection_id: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO hk_lineage VALUES (?, ?, ?)",
            (
                workspace_id, collection_id,
                json.dumps(describe_embedder(self.embedder), sort_keys=True),
            ),
        )

    async def reindex(self, workspace_id: str, collection_id: str = "default") -> dict[str, Any]:
        """Replace all collection vectors and lineage atomically, retaining text and citations.

        Provider failure leaves the old index intact. A concurrent change refuses the
        replacement; the operator can retry against the new snapshot.
        """
        def snapshot() -> list[tuple[Any, ...]]:
            return list(self._conn.execute(
                "SELECT document_id, chunk_index, text, embedding FROM hk_chunks "
                "WHERE workspace_id = ? AND collection_id = ? ORDER BY document_id, chunk_index",
                (workspace_id, collection_id),
            ).fetchall())

        with self._lock:
            before = snapshot()
            old_status = self.embedding_status(workspace_id, collection_id)
        result = await self.embedder.embed_texts([r[2] for r in before])

        def commit() -> None:
            if (
                snapshot() != before
                or self.embedding_status(workspace_id, collection_id) != old_status
            ):
                raise EmbeddingLineageMismatchError(
                    "Collection changed during reindex; retry reindex."
                )
            for row, chunk in zip(before, result.embedded_chunks, strict=True):
                self._conn.execute(
                    "UPDATE hk_chunks SET embedding = ? WHERE document_id = ? AND chunk_index = ?",
                    (_pack(chunk.embedding), row[0], row[1]),
                )
            self._write_lineage(workspace_id, collection_id)

        self._atomic(commit)
        return {
            "reindexed_chunks": len(before),
            **self.embedding_status(workspace_id, collection_id),
        }

    def limits(self) -> dict[str, int]:
        """Configured per-file, per-workspace and top_k limits."""
        return {
            "max_file_bytes": self.max_file_bytes,
            "max_workspace_bytes": self.max_workspace_bytes,
            "max_top_k": self.max_top_k,
            "max_metadata_bytes": self.max_metadata_bytes,
            "max_source_uri_chars": self.max_source_uri_chars,
            "max_scan": self.max_scan,
        }

    def workspace_usage(self, workspace_id: str) -> int:
        """Bytes (UTF-8) stored by ``workspace_id``: text, metadata JSON, source URI and title."""
        with self._lock:
            row = self._conn.execute(
                f"SELECT COALESCE(SUM({_SIZE_SQL}), 0) FROM hk_documents WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
        return int(row[0])

    # -- transactions ---------------------------------------------------------------------------

    def _atomic(self, fn: Callable[[], Any]) -> Any:
        """Run ``fn`` inside a savepoint so multi-table changes commit or roll back together."""
        name = f"hk_{uuid.uuid4().hex[:8]}"
        with self._lock:
            self._conn.execute(f"SAVEPOINT {name}")
            try:
                result = fn()
            except BaseException:
                self._conn.execute(f"ROLLBACK TO {name}")
                self._conn.execute(f"RELEASE {name}")
                raise
            self._conn.execute(f"RELEASE {name}")
            if not self._conn.in_transaction:
                return result
            self._conn.commit()
            return result

    # -- ingestion ------------------------------------------------------------------------------

    def _parse(
        self, data: bytes, filename: str, mime_type: str | None
    ) -> tuple[str, str, list[tuple[int, int, int]]]:
        mime = mime_type if mime_type and mime_type != "application/octet-stream" else None
        if mime in ("text/markdown", "text/x-markdown"):
            mime = "text/plain"
        if mime is None or mime == "text/plain":
            detected = detect_mime_type(data, filename)
            mime = (
                detected if mime is None or detected in ("text/html", "application/pdf") else mime
            )
        if mime in _ARCHIVE_MIMES:
            raise DocumentParseError(
                "Archives (zip, gzip) are not accepted; upload the individual files instead."
            )
        try:
            parsed = get_parser_for_mime(mime).parse(data, filename=filename)
        except IngestionError as exc:
            raise DocumentParseError(str(exc)) from exc
        pages = (
            [(p.page_number, p.span_start, p.span_end) for p in parsed.pages]
            if mime == "application/pdf"
            else []
        )
        return parsed.text, mime, pages

    def _row_to_doc(self, row: sqlite3.Row | tuple[Any, ...]) -> IndexedDocument:
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

    _DOC_COLS = (
        "document_id, workspace_id, collection_id, title, source_uri, mime_type, content_hash, "
        "byte_size, LENGTH(text), chunk_count, status, created_at, updated_at, metadata"
    )

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
        self._check_lineage(workspace_id, collection_id)
        if len(data) > self.max_file_bytes:
            raise FileTooLargeError(
                f"File is {len(data)} bytes; the per-file limit is {self.max_file_bytes} bytes."
            )
        if not data:
            raise EmptyDocumentError("Document content cannot be empty.")
        meta_json = json.dumps(metadata or {}, ensure_ascii=False, default=str)
        if len(meta_json.encode("utf-8")) > self.max_metadata_bytes:
            raise DocumentLimitError(
                f"Metadata exceeds the per-document limit of {self.max_metadata_bytes} bytes."
            )
        if source_uri is not None and len(source_uri) > self.max_source_uri_chars:
            raise DocumentLimitError(
                f"source_uri exceeds the limit of {self.max_source_uri_chars} characters."
            )
        content_hash = hashlib.sha256(data).hexdigest()
        with self._lock:
            same = self._find_duplicate(workspace_id, collection_id, filename, content_hash)
        if same is not None:
            deduped: IngestOutcome = self._atomic(
                lambda: self._dedup_outcome(
                    workspace_id, collection_id, filename, content_hash, source_uri, metadata
                )
            )
            return deduped

        text, mime, pages = self._parse(data, filename, mime_type)
        if not text.strip():
            raise EmptyDocumentError("Document has no extractable text.")
        text_bytes = len(text.encode("utf-8"))
        if text_bytes > self.max_file_bytes:
            raise FileTooLargeError(
                f"Extracted text is {text_bytes} bytes; the per-file limit is "
                f"{self.max_file_bytes} bytes."
            )
        new_size = _stored_size(text, filename, source_uri, meta_json)
        self._check_quota(
            workspace_id, self._title_id(workspace_id, collection_id, filename), new_size
        )

        doc_id = f"doc_{uuid.uuid4().hex[:12]}"
        chunks = self.chunker.chunk_text(text, document_id=doc_id)
        embedded = await self.embedder.embed_chunks(chunks)
        vectors = {e.chunk_id: e.embedding for e in embedded.embedded_chunks}
        now = time.time()

        def commit() -> IngestOutcome:
            # Re-check inside the transaction: another ingest may have run while embedding.
            self._check_lineage(workspace_id, collection_id)
            dup = self._find_duplicate(workspace_id, collection_id, filename, content_hash)
            if dup is not None:
                return self._dedup_outcome(
                    workspace_id, collection_id, filename, content_hash, source_uri, metadata
                )
            self._check_quota(
                workspace_id, self._title_id(workspace_id, collection_id, filename), new_size
            )
            old = self._conn.execute(
                "SELECT document_id, created_at FROM hk_documents "
                "WHERE workspace_id = ? AND collection_id = ? AND title = ?",
                (workspace_id, collection_id, filename),
            ).fetchone()
            replaced = old is not None
            final_id = old[0] if old else doc_id
            created = old[1] if old else now
            if old:
                self._purge(final_id)
            self._write_lineage(workspace_id, collection_id)
            for c in chunks:
                first, last = _chunk_pages(pages, c.start_char, c.end_char)
                self._conn.execute(
                    "INSERT INTO hk_chunks(chunk_id, document_id, workspace_id, collection_id, "
                    "chunk_index, text, start_char, end_char, embedding, page_start, page_end) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        c.chunk_id,
                        final_id,
                        workspace_id,
                        collection_id,
                        c.chunk_index,
                        c.text,
                        c.start_char,
                        c.end_char,
                        _pack(vectors[c.chunk_id]),
                        first,
                        last,
                    ),
                )
                self._conn.execute(
                    "INSERT INTO hk_fts(text, chunk_id, document_id, workspace_id, collection_id) "
                    "VALUES (?,?,?,?,?)",
                    (c.text, c.chunk_id, final_id, workspace_id, collection_id),
                )
            self._conn.execute(
                "INSERT OR REPLACE INTO hk_documents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    final_id,
                    workspace_id,
                    collection_id,
                    filename,
                    source_uri,
                    mime,
                    content_hash,
                    len(data),
                    text,
                    len(chunks),
                    "indexed",
                    created,
                    now,
                    meta_json,
                ),
            )
            row = self._conn.execute(
                f"SELECT {self._DOC_COLS} FROM hk_documents WHERE document_id = ?", (final_id,)
            ).fetchone()
            return IngestOutcome(document=self._row_to_doc(row), replaced=replaced)

        outcome: IngestOutcome = self._atomic(commit)
        return outcome

    def _find_duplicate(
        self, workspace_id: str, collection_id: str, title: str, content_hash: str
    ) -> tuple[Any, ...] | None:
        """Existing document to deduplicate against, or None.

        A same-title document always wins (re-uploading changed content replaces it, even when
        that content equals another document's); otherwise any document with the same hash.
        """
        cols = (
            f"SELECT {self._DOC_COLS} FROM hk_documents "
            "WHERE workspace_id = ? AND collection_id = ?"
        )
        titled = self._conn.execute(
            cols + " AND title = ?", (workspace_id, collection_id, title)
        ).fetchone()
        if titled is not None:
            return titled if titled[6] == content_hash else None
        row: tuple[Any, ...] | None = self._conn.execute(
            cols + " AND content_hash = ?", (workspace_id, collection_id, content_hash)
        ).fetchone()
        return row

    def _title_id(self, workspace_id: str, collection_id: str, title: str) -> str | None:
        """Id of the same-title document that an ingest would replace, if any."""
        with self._lock:
            row = self._conn.execute(
                "SELECT document_id FROM hk_documents "
                "WHERE workspace_id = ? AND collection_id = ? AND title = ?",
                (workspace_id, collection_id, title),
            ).fetchone()
        return None if row is None else str(row[0])

    def _check_quota(self, workspace_id: str, replacing: str | None, size: int) -> None:
        """Refuse ``size`` more bytes (minus the replaced document) beyond the workspace quota."""
        with self._lock:
            old = 0
            if replacing is not None:
                row = self._conn.execute(
                    f"SELECT {_SIZE_SQL} FROM hk_documents WHERE document_id = ?", (replacing,)
                ).fetchone()
                old = int(row[0]) if row else 0
        if self.workspace_usage(workspace_id) - old + size > self.max_workspace_bytes:
            raise WorkspaceQuotaExceededError(
                f"Workspace storage limit of {self.max_workspace_bytes} bytes would be exceeded."
            )

    def _dedup_outcome(
        self,
        workspace_id: str,
        collection_id: str,
        title: str,
        content_hash: str,
        source_uri: str | None,
        metadata: dict[str, Any] | None,
    ) -> IngestOutcome:
        """Dedup hit: keep chunks, but apply a changed source_uri / metadata and report it."""
        self._check_lineage(workspace_id, collection_id)
        row = self._find_duplicate(workspace_id, collection_id, title, content_hash)
        if row is None:  # pragma: no cover - guarded by the caller's lookup
            raise KnowledgeError("Duplicate document vanished during ingest.")
        doc = self._row_to_doc(row)
        new_uri = source_uri if source_uri is not None else doc.source_uri
        new_meta = metadata if metadata is not None else doc.metadata
        if new_uri == doc.source_uri and new_meta == doc.metadata:
            return IngestOutcome(document=doc, deduplicated=True)
        meta_json = json.dumps(new_meta, ensure_ascii=False, default=str)
        text = self.get_document_text(workspace_id, doc.document_id) or ""
        self._check_quota(
            workspace_id,
            doc.document_id,
            _stored_size(text, doc.title, new_uri, meta_json),
        )
        self._conn.execute(
            "UPDATE hk_documents SET source_uri = ?, metadata = ?, updated_at = ? "
            "WHERE document_id = ?",
            (new_uri, meta_json, time.time(), doc.document_id),
        )
        fresh = self._conn.execute(
            f"SELECT {self._DOC_COLS} FROM hk_documents WHERE document_id = ?", (doc.document_id,)
        ).fetchone()
        return IngestOutcome(
            document=self._row_to_doc(fresh), deduplicated=True, metadata_updated=True
        )

    def _purge(self, document_id: str) -> None:
        self._conn.execute("DELETE FROM hk_fts WHERE document_id = ?", (document_id,))
        self._conn.execute("DELETE FROM hk_chunks WHERE document_id = ?", (document_id,))

    # -- documents ------------------------------------------------------------------------------

    def list_documents(
        self, workspace_id: str, collection_id: str | None = "default"
    ) -> list[IndexedDocument]:
        """Documents of a workspace (one collection, or all when ``collection_id`` is None)."""
        sql = f"SELECT {self._DOC_COLS} FROM hk_documents WHERE workspace_id = ?"
        args: list[Any] = [workspace_id]
        if collection_id is not None:
            sql += " AND collection_id = ?"
            args.append(collection_id)
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY created_at, document_id", args).fetchall()
        return [self._row_to_doc(r) for r in rows]

    def get_document(self, workspace_id: str, document_id: str) -> IndexedDocument | None:
        """One document of the workspace, or None (also None for another workspace's document)."""
        with self._lock:
            row = self._conn.execute(
                f"SELECT {self._DOC_COLS} FROM hk_documents "
                "WHERE workspace_id = ? AND document_id = ?",
                (workspace_id, document_id),
            ).fetchone()
        return self._row_to_doc(row) if row else None

    def get_document_text(self, workspace_id: str, document_id: str) -> str | None:
        """Normalized text of a workspace document, or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT text FROM hk_documents WHERE workspace_id = ? AND document_id = ?",
                (workspace_id, document_id),
            ).fetchone()
        return None if row is None else str(row[0])

    def get_chunk(
        self, workspace_id: str, document_id: str, chunk_index: int
    ) -> dict[str, Any] | None:
        """One chunk with its citation fields (used by footnote links), or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT c.chunk_id, c.text, c.start_char, c.end_char, d.title, d.source_uri "
                "FROM hk_chunks c JOIN hk_documents d ON d.document_id = c.document_id "
                "WHERE c.workspace_id = ? AND c.document_id = ? AND c.chunk_index = ?",
                (workspace_id, document_id, chunk_index),
            ).fetchone()
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
        """Remove a document, its chunks, vectors and keyword entries atomically."""

        def run() -> bool:
            row = self._conn.execute(
                "SELECT 1 FROM hk_documents WHERE workspace_id = ? AND document_id = ?",
                (workspace_id, document_id),
            ).fetchone()
            if row is None:
                return False
            self._purge(document_id)
            self._conn.execute("DELETE FROM hk_documents WHERE document_id = ?", (document_id,))
            return True

        deleted: bool = self._atomic(run)
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

        ``score`` is the fused RRF score normalised to ``0..1`` (1.0 means rank 1 in every enabled
        ranker); ``min_score`` drops hits whose normalised score is below it.
        """
        top_k = max(1, min(int(top_k), self.max_top_k))
        self._check_lineage(workspace_id, collection_id)
        terms = extract_query_terms(query)
        if not terms:
            return []
        pool = max(top_k * 4, 20)

        keyword_ids: list[str] = []
        if keyword_weight > 0:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT chunk_id FROM hk_fts WHERE hk_fts MATCH ? AND workspace_id = ? "
                    "AND collection_id = ? ORDER BY bm25(hk_fts) LIMIT ?",
                    (_fts_expression(terms), workspace_id, collection_id, pool),
                ).fetchall()
            keyword_ids = [r[0] for r in rows]

        vector_ids: list[str] = []
        vector_scores: dict[str, float] = {}
        if vector_weight > 0:
            qvec = (await self.embedder.embed_texts([" ".join(terms)])).embedded_chunks[0].embedding
            with self._lock:
                self._check_lineage(workspace_id, collection_id)
                rows = self._conn.execute(
                    "SELECT chunk_id, embedding FROM hk_chunks "
                    "WHERE workspace_id = ? AND collection_id = ? ORDER BY rowid DESC LIMIT ?",
                    (workspace_id, collection_id, self.max_scan),
                ).fetchall()
            sims = _cosine_scores(qvec, [r[1] for r in rows])
            scored = [(r[0], sc) for r, sc in zip(rows, sims, strict=True)]
            scored = [s for s in scored if s[1] > min_vector_score]
            scored.sort(key=lambda s: (-s[1], s[0]))
            for cid, sc in scored[:pool]:
                vector_ids.append(cid)
                vector_scores[cid] = sc

        fused = weighted_rrf(
            {"vector": vector_ids, "keyword": keyword_ids},
            {"vector": vector_weight, "keyword": keyword_weight},
            rrf_k,
        )
        best = (vector_weight if vector_weight > 0 else 0.0) / (rrf_k + 1) + (
            keyword_weight if keyword_weight > 0 else 0.0
        ) / (rrf_k + 1)
        if best > 0:
            fused = {c: min(1.0, v / best) for c, v in fused.items()}
        fused = {c: v for c, v in fused.items() if v >= min_score}
        keyword_rank = {c: i for i, c in enumerate(keyword_ids)}
        # Ties: chunks found by both rankers first, then keyword matches, then vector similarity.
        ordered = sorted(
            fused,
            key=lambda c: (
                -fused[c],
                -((c in keyword_rank) + (c in vector_scores)),
                keyword_rank.get(c, len(keyword_rank)),
                -vector_scores.get(c, 0.0),
                c,
            ),
        )[:top_k]
        if not ordered:
            return []
        with self._lock:
            marks = ",".join("?" * len(ordered))
            rows = self._conn.execute(
                "SELECT c.chunk_id, c.document_id, c.chunk_index, c.text, c.start_char, "
                "c.end_char, d.title, d.source_uri, c.page_start, c.page_end FROM hk_chunks c "
                "JOIN hk_documents d ON d.document_id = c.document_id "
                f"WHERE c.workspace_id = ? AND c.collection_id = ? AND c.chunk_id IN ({marks})",
                [workspace_id, collection_id, *ordered],
            ).fetchall()
        by_id = {r[0]: r for r in rows}
        hits: list[SearchHit] = []
        for position, cid in enumerate(ordered, start=1):
            r = by_id.get(cid)
            if r is None:
                continue
            spans = _highlights(r[3], terms)
            snippet, rebased = _make_snippet(r[3], spans)
            rebased_u16 = _to_utf16_spans(snippet, rebased)
            matched = [
                n for n, ids in (("vector", vector_ids), ("keyword", keyword_ids)) if cid in ids
            ]
            hits.append(
                SearchHit(
                    chunk_id=cid,
                    document_id=r[1],
                    title=r[6],
                    chunk_index=r[2],
                    text=r[3],
                    snippet=snippet,
                    highlights=rebased_u16,
                    highlights_cp=rebased,
                    score=fused[cid],
                    vector_rank=vector_ids.index(cid) + 1 if cid in vector_ids else None,
                    keyword_rank=keyword_ids.index(cid) + 1 if cid in keyword_ids else None,
                    vector_score=vector_scores.get(cid),
                    matched_by=matched,
                    citation=HybridCitation(
                        citation_id=f"[{position}]",
                        document_id=r[1],
                        title=r[6],
                        chunk_index=r[2],
                        chunk_id=cid,
                        start_char=r[4],
                        end_char=r[5],
                        source_uri=r[7],
                        page_start=r[8],
                        page_end=r[9],
                    ),
                )
            )
        return hits


def create_hybrid_search_tool(
    index: HybridKnowledgeIndex,
    *,
    workspace_id: str,
    collection_id: str = "default",
    name: str = "search_knowledge",
    top_k: int = 5,
) -> Tool:
    """Agent tool returning a JSON string ``{"context": ..., "citations": [...]}`` for footnotes."""

    async def search_knowledge(query: str) -> str:
        hits = await index.search(
            workspace_id=workspace_id, query=query, collection_id=collection_id, top_k=top_k
        )
        context = "\n\n".join(f"{h.citation.citation_id} {h.title}: {h.snippet}" for h in hits)
        return json.dumps(
            {"context": context, "citations": [h.citation.model_dump() for h in hits]},
            ensure_ascii=False,
        )

    return Tool(
        name=name,
        fn=search_knowledge,
        description="Search the workspace knowledge base; returns context with citations.",
    )
