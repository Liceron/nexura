# Knowledge: ingestion, hybrid search and citations

`HybridKnowledgeIndex` (in `nexura_runtime.knowledge`) is the workspace-scoped knowledge store
behind the Studio **Knowledge Base** page and the `/api/v1/knowledge` routes. It keeps chunks,
vectors and an SQLite FTS5 keyword index in one database.

## Quick start

The example uses the deterministic fake embedder, which is labelled **non-semantic**: it makes
tests reproducible but does not understand meaning, so keyword (BM25) matching does most of the
work. Plug in a real embedding provider for semantic ranking.

```python
import asyncio

from nexura_runtime.knowledge import EmbeddingOrchestrator, HybridKnowledgeIndex
from nexura_runtime.knowledge.embeddings import DeterministicEmbeddingProvider


async def main() -> None:
    index = HybridKnowledgeIndex(embedder=EmbeddingOrchestrator(DeterministicEmbeddingProvider()))
    await index.ingest(
        workspace_id="acme",
        filename="faq.md",
        data=b"# FAQ\nRefunds are processed within five business days.",
        source_uri="https://example.test/faq",
    )
    hits = await index.search(workspace_id="acme", query="refund business days", top_k=3)
    top = hits[0]
    print(top.citation.title, top.citation.chunk_index, top.citation.source_uri)
    assert await index.search(workspace_id="other", query="refund") == []


asyncio.run(main())
```

## How search works

1. The query is reduced to safe terms (letters, digits and Devanagari only). FTS5 operators,
   quotes and `col:` filters cannot survive, so query text cannot change the SQL or the match
   expression.
2. A **keyword ranker** (FTS5 `bm25`) and a **vector ranker** (cosine similarity) each produce a
   ranked candidate list, always filtered by `workspace_id` and `collection_id`.
3. The lists are fused with weighted **reciprocal rank fusion**:
   `score = sum(weight / (rrf_k + rank))`, with `rrf_k = 60`. Set `vector_weight=0` or
   `keyword_weight=0` to use a single ranker.
4. `top_k` is clamped to `[1, max_top_k]` (default cap 50).

Every `SearchHit` carries a `citation` (document id, title, chunk index, chunk id, character span
in the normalized document text, source URI), a `snippet` (at most 500 characters) and
`highlights`: spans inside the snippet that Studio wraps in `<mark>` elements built from plain
text, never from HTML. `SearchHit.highlights` is in **UTF-16 code units** (what JavaScript
`String.slice` uses, so astral characters such as emoji are never split); `highlights_cp` holds
the same spans in Python code points. The HTTP citation carries `highlights` (code points) and
`highlights_utf16`; Studio uses the UTF-16 ones.

`SearchHit.score` is the fused RRF score **normalised to 0..1** (1.0 = rank 1 in every enabled
ranker). `min_score` (HTTP: `min_score_threshold`, 0..1) drops hits below that normalised score.
Vector candidates below a cosine floor (`min_vector_score`, default 0.05) are ignored so the
non-semantic fake embedder's hash-collision noise does not surface unrelated chunks. Query terms
may be up to 256 characters; indexing does not truncate tokens.

## Ingestion

| Input | Handling |
| --- | --- |
| text, Markdown | decoded and normalized |
| HTML | `script`, `style` and `head` content removed, tags stripped |
| PDF | existing `PdfParser` (needs the `pypdf` extra), page limits apply |

* **Archives are not accepted**: zip and gzip uploads are refused (`DOCUMENT_PARSE_FAILED`);
  upload the individual files. This closes decompression-bomb expansion in the hybrid API.
* **Size limits** apply to the extracted text as well as the upload: 5 MB per file and 50 MB per workspace by default
  (`max_file_bytes`, `max_workspace_bytes`). Over-limit files are refused with `OVERSIZED_DOCUMENT`
  or `WORKSPACE_QUOTA_EXCEEDED` (HTTP 413) before any parsing or embedding. `GET
  /knowledge/limits` reports the limits and current usage; usage counts stored UTF-8 text bytes,
  not upload bytes.
* **Dedup**: a file whose SHA-256 matches an existing document in the same workspace and
  collection returns that document (`deduplicated: true`), unless a document with the same title
  exists: then the same-title document is the one compared (and replaced when content differs).
* **Re-ingest**: a changed file with the same name replaces the old document in one SQLite
  savepoint. The document id is kept, old chunks, vectors and keyword entries are removed, and a
  failure leaves the previous version untouched.
* **Delete**: removes chunks, vectors and FTS5 rows together.

## HTTP routes

`POST /knowledge/documents/upload` accepts `content` as text, or base64 with
`"content_encoding": "base64"` for PDFs. `POST /knowledge/retrieve` returns `citations` and
`items`; `GET /knowledge/documents/{id}/chunks/{index}` returns the cited chunk, which is where
citation footnotes link to in Studio (they open the Knowledge page at `#/knowledge` and Studio
focuses the cited chunk from in-app state; the URL carries no `doc=` or `chunk=` query string).

## Citations in chat

`create_hybrid_search_tool(index, workspace_id=...)` returns an agent tool whose output is a JSON
string `{"context": ..., "citations": [...]}`. Studio chat detects a `citations` array in tool
output and shows numbered footnotes under the reply that link to the source chunk.

## Limits

* Vector search scores at most `max_scan` chunks per query (default 20,000, newest first)
  in one vectorised pass over packed float32 embeddings; older chunks beyond that are only
  found by keyword search. It suits local and small workspaces, not millions of chunks.
* The workspace quota (50 MB by default) counts stored text plus each document's metadata
  JSON, `source_uri` and title. Per document, metadata is capped at 1 MiB
  (`max_metadata_bytes`) and `source_uri` at 131,072 characters (`max_source_uri_chars`);
  larger values are refused with 413 `DOCUMENT_FIELD_TOO_LARGE`.
* `/retrieve` and `/answer` share a limit of 120 requests per minute per workspace; above it
  they return 429 `RATE_LIMITED` with `Retry-After`.
* Page numbers are known only for PDF chunks (`page_start`, `page_end`); other citations
  carry `null` and answers cite no page.
* Uploading content identical to an existing document returns 200 with
  `deduplicated: true` (201 only when a document is created); a new `source_uri` or
  metadata on that upload updates the existing document.
* The default embedder is non-semantic. No live embedding provider was exercised in tests.
* The routes read the workspace from the request, so isolation is as strong as the service's
  workspace authentication (see `SECURITY_MODEL.md`).

## In the chat playground

The Studio chat playground (`POST /api/v1/sessions/{id}/chat` and `/chat/stream`) has a
`knowledge_search` tool. It searches the default collection of the caller's workspace only, returns
at most 5 hits, and counts against the same per-workspace 120 requests per minute limit as
`/retrieve` and `/answer`. Its output is a JSON string with `context` and `citations` (the same
citation fields `/retrieve` returns), so the chat footnotes link to the cited chunk. When the limit
is hit the output has an empty `citations` list and an `error` with code `RATE_LIMITED`.

The offline deterministic handler calls the tool when the message does not match the calculate or
order-status triggers and either contains one of `search`, `according to`, `docs`, `document(s)` or
`knowledge` as a whole word (`research` does not match), or is a bare question: a `?`, at least
three words, not small talk such as a greeting, and the workspace has at least one knowledge
document. A lookup against an empty index returns no citations and does not use the rate limit.

Safety limits: a chat message is at most 8,000 characters (longer is rejected with 422), and the
query passed to the tool is cut to 512 characters. Secret-looking values (API keys, tokens,
`password=` pairs) in document text are redacted in the tool output, the reply and the saved
messages. `[n]` markers and line breaks in document text are defused (`[7]` becomes `(7)`), so only
the tool's own numbering appears as a citation. Highlight offsets are dropped for a snippet whose
text was redacted.

Operator-configured models receive the same tool and decide for themselves.

## Package overview

**PostgreSQL backends for `nexura-runtime`: durable run and session storage, and a pgvector
vector store.**

```bash
pip install nexura-postgres   # installs nexura-runtime and asyncpg too
```

- **`PostgresRepository`**: the same storage contract as the runtime's built-in
  `SQLiteRepository` (sessions with compare-and-swap revisions, runs, run events), over an
  asyncpg connection pool. `init_db()` creates its three tables.
- **`PgVectorStoreAdapter`**: the runtime's `VectorStore` protocol on PostgreSQL with the
  `vector` extension (workspace-scoped collections, immutable index generations).
- **Found by the runtime.** `nexura_runtime.storage.get_storage_repository("postgresql://...")`
  returns a `PostgresRepository` when this package is installed. Without it, that call raises
  `MissingDependencyError`: "PostgreSQL storage is provided by the 'nexura-postgres' package,
  which is not installed. Install it with: pip install nexura-postgres". SQLite storage needs
  nothing extra.

> Status: **alpha** (0.4), Python 3.12 (the same as `nexura-runtime`). Tested against a local
> disposable PostgreSQL service with pgvector; never against a managed cloud database.

## Storage through the runtime factory

The example below needs no database: it only builds the repository (the pool is created on
first use). Point the URI at a real server and call `await repository.init_db()` to use it.

```python
from nexura_runtime.storage import StorageRepository, get_storage_repository

from nexura_postgres import PostgresRepository

repository = get_storage_repository("postgresql://nexura:secret@localhost:5432/nexura")
assert isinstance(repository, PostgresRepository)
assert isinstance(repository, StorageRepository)  # same protocol as SQLiteRepository
print(type(repository).__name__, repository.dsn)
```

## Using your own asyncpg pool

`PostgresRepository(pool=...)` accepts any asyncpg-compatible pool. Here a stand-in pool shows
the compare-and-swap rule without a server: saving with a stale revision is refused.

```python
import asyncio
from typing import Any

from nexura_agents.errors import SessionConflictError
from nexura_agents.types import Message

from nexura_postgres import PostgresRepository


class Connection:
    async def fetchrow(self, query: str, *args: Any) -> dict[str, int]:
        return {"revision": 2}  # the stored session is already at revision 2

    def transaction(self) -> "Context":
        return Context(None)


class Context:
    def __init__(self, value: Any) -> None:
        self.value = value

    async def __aenter__(self) -> Any:
        return self.value

    async def __aexit__(self, *exc: object) -> None:
        return None


class Pool:
    def acquire(self) -> Context:
        return Context(Connection())

    async def close(self) -> None:
        return None


async def main() -> None:
    repository = PostgresRepository(pool=Pool())
    try:
        await repository.save_session("ws", "s1", [Message(role="user", content="hi")], 1)
    except SessionConflictError as exc:
        print("refused:", exc.expected_revision, "!=", exc.actual_revision)
    await repository.close()


asyncio.run(main())
```

## Package overview — additional examples and notes

**A Qdrant vector store for `nexura-runtime`, with workspace-scoped collections and
immutable index generations.**

```bash
pip install nexura-qdrant   # installs nexura-runtime and qdrant-client too
```

- **`QdrantVectorStoreAdapter`** implements the runtime's `VectorStore` protocol
  (`nexura_runtime.knowledge.vector.store`), so it can replace the built-in in-memory or SQLite
  stores anywhere the runtime takes a `VectorStore`.
- **Workspace isolation.** Every collection, generation and record is keyed by workspace; a
  search never returns another workspace's records.
- **Index generations.** A new embedding model or chunking run gets a new generation; a
  generation is immutable once activated, and searches use the active one.
- The runtime does not depend on this package. Without it, the built-in stores still work.

> Status: **alpha** (0.4), Python 3.12 (the same as `nexura-runtime`). Tested against a local
> disposable Qdrant service; never against Qdrant Cloud.

## Offline example

The adapter connects on first use, so it can be built and checked without a server. Inputs are
validated before any request: a collection with zero dimensions is refused locally.

```python
import asyncio

from nexura_runtime.knowledge.vector.errors import VectorDimensionMismatchError
from nexura_runtime.knowledge.vector.store import VectorStore

from nexura_qdrant import QdrantVectorStoreAdapter


async def main() -> None:
    store = QdrantVectorStoreAdapter(url="http://localhost:6333")
    assert isinstance(store, VectorStore) and store.is_available
    try:
        await store.create_collection("ws-1", "faq", dimensions=0)
    except VectorDimensionMismatchError as exc:
        print("refused before any request:", exc)
    await store.close()


asyncio.run(main())
```

With a running Qdrant (for example `docker run -p 6333:6333 qdrant/qdrant`), call
`create_collection`, `upsert` and `search` on the same object.
