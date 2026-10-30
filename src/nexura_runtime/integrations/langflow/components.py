# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Extended (V3) Langflow component families implemented on SDK pieces.

File, MemoryBase, KnowledgeBase/Knowledge, LoopComponent, SQLComponent and SplitText. Every
refusal (size cap, path escape, SQL policy, loop bound) raises ``ComponentRefusedError`` when
the node runs; tools return the refusal to the model as ``{"error": ...}`` instead.
Component source stored in a flow is never executed.
"""

from __future__ import annotations

import contextvars
import hashlib
import inspect
import json
import re
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from nexura_agents import SessionStore, Tool, tool

from .errors import ComponentRefusedError, FlowBuildError

#: ``embed(text) -> list[float]`` (sync or async). Used by Knowledge retrieval and ingestion.
Embedder = Callable[[str], Any]

_NAME = re.compile(r"[A-Za-z0-9_.\-]{1,64}")
TEXT_SUFFIXES = frozenset({".txt", ".md", ".markdown", ".csv", ".json", ".log", ".tsv", ".yaml"})
PDF_SUFFIX = ".pdf"


@dataclass(frozen=True)
class V3Options:
    """Everything the extended components need from the caller (pass to ``build(v3=...)``)."""

    #: Directory File nodes may read from. Without it a File node refuses to build.
    files_root: str | Path | None = None
    #: Per-file cap, and also the aggregate cap across all paths of one File node.
    max_file_bytes: int = 1_000_000
    #: Most paths one File node may read.
    max_file_paths: int = 20
    max_pdf_pages: int = 100
    #: Vector store for Knowledge nodes (default: a fresh in-memory store).
    vector_store: Any = None
    #: Embedding function; required by Knowledge nodes.
    embedder: Embedder | None = None
    workspace_id: str = "default"
    #: ``workspace``: one collection per knowledge base name. ``session``: one per session too.
    knowledge_scope: Literal["workspace", "session"] = "workspace"
    max_top_k: int = 20
    #: ``SqlConnectionManager`` (default: the integration's default manager).
    sql_manager: Any = None
    #: Registered connection id every SQL node uses (a flow's own database_url is never used).
    sql_connection: str | None = None
    sql_allow_write: bool = False
    sql_max_rows: int = 100
    sql_max_bytes: int = 200_000
    sql_timeout_seconds: float = 5.0
    #: A Loop with more items than this is refused (never truncated).
    max_loop_items: int = 100
    max_split_chunks: int = 1000


@dataclass
class RunScope:
    """Per-run context the Memory and Knowledge tools read (set while a node runs)."""

    session_id: str | None
    store: SessionStore | None
    #: Session-store keys of every Agent in the flow for this session.
    session_keys: list[str] = field(default_factory=list)


RUN_SCOPE: contextvars.ContextVar[RunScope | None] = contextvars.ContextVar(
    "nexura_langflow_run_scope", default=None
)


# ---------------------------------------------------------------------------------- helpers
def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _positive_int(value: Any, default: int, hard_max: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return default
    try:
        n = int(value)
    except ValueError:
        return default
    return max(1, min(n, hard_max))


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


# ------------------------------------------------------------------------------------ File
def resolve_file(root: Path, raw: str) -> Path:
    """Resolve ``raw`` under ``root``; refuse traversal, escapes and non-file targets."""
    if not raw or "\x00" in raw:
        raise ComponentRefusedError("File path is empty or contains a NUL byte")
    candidate = Path(raw)
    if ".." in candidate.parts:
        raise ComponentRefusedError(f"File path {raw!r} contains '..' (path traversal refused)")
    full = candidate if candidate.is_absolute() else root / candidate
    try:
        resolved = full.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise ComponentRefusedError(f"File {raw!r} not found under the files root") from exc
    if not resolved.is_relative_to(root):
        raise ComponentRefusedError(f"File path {raw!r} is outside the files root (refused)")
    if not resolved.is_file():
        raise ComponentRefusedError(f"File path {raw!r} is not a regular file")
    return resolved


def read_files_text(root: Path, paths: Sequence[str], opts: V3Options) -> str:
    """Read all paths of one File node; the byte cap applies to their sum."""
    if len(paths) > opts.max_file_paths:
        raise ComponentRefusedError(
            f"File node lists {len(paths)} paths, over the {opts.max_file_paths} path limit"
        )
    parts: list[str] = []
    total = 0
    sep = 2  # the "\n\n" joiner
    for raw in paths:
        remaining = opts.max_file_bytes - total - (sep if parts else 0)
        if remaining < 0:
            remaining = 0
        try:
            text = read_file_text(root, raw, opts, limit=remaining)
        except _OverLimitError as exc:
            raise ComponentRefusedError(
                f"File node total is over the {opts.max_file_bytes} byte aggregate limit "
                f"(at {raw!r}, after {len(parts)} files)"
            ) from exc
        total += len(text.encode("utf-8")) + (sep if parts else 0)
        parts.append(text)
    return "\n\n".join(parts)


class _OverLimitError(ComponentRefusedError):
    """Internal: a file would push the node past its remaining byte budget."""


def read_file_text(root: Path, raw: str, opts: V3Options, limit: int | None = None) -> str:
    """Read one allowed text or PDF file as text, enforcing the size caps.

    ``limit`` (bytes) is the budget left for this file within its node; it never exceeds
    ``max_file_bytes``.
    """
    cap = opts.max_file_bytes if limit is None else min(limit, opts.max_file_bytes)
    path = resolve_file(root, raw)
    suffix = path.suffix.lower()
    if suffix not in TEXT_SUFFIXES and suffix != PDF_SUFFIX:
        raise ComponentRefusedError(
            f"File type {suffix or '(none)'!r} is not supported "
            f"(allowed: {', '.join(sorted(TEXT_SUFFIXES | {PDF_SUFFIX}))})"
        )
    size = path.stat().st_size
    if size > opts.max_file_bytes:
        raise ComponentRefusedError(
            f"File {raw!r} is {size} bytes, over the {opts.max_file_bytes} byte limit"
        )
    if size > cap and suffix != PDF_SUFFIX:
        raise _OverLimitError(raw)
    data = path.read_bytes()
    if suffix == PDF_SUFFIX:
        from nexura_runtime.knowledge.ingest.parsers import PdfParser

        try:
            parsed = PdfParser().parse(data, filename=path.name, max_pages=opts.max_pdf_pages)
        except Exception as exc:  # parser errors carry their own message
            raise ComponentRefusedError(f"PDF {raw!r} could not be read: {exc}") from exc
        text = parsed.text
    else:
        if b"\x00" in data:
            raise ComponentRefusedError(f"File {raw!r} looks binary, not text")
        text = data.decode("utf-8", errors="replace")
    if len(text.encode("utf-8")) > opts.max_file_bytes:
        raise ComponentRefusedError(f"Text of {raw!r} exceeds the {opts.max_file_bytes} byte limit")
    if len(text.encode("utf-8")) > cap:
        raise _OverLimitError(raw)
    return text


def file_paths(value: Any) -> list[str]:
    """Normalise a File node's ``path``/``file_path`` field (string or list) to strings."""
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(v) for v in value if isinstance(v, str) and v.strip()]
    return []


# --------------------------------------------------------------------------------- SplitText
def split_text(text: str, size: int, overlap: int, separator: str) -> list[str]:
    """Character chunks of at most ``size``; split on ``separator`` first, then overlap."""
    if size < 1:
        raise ComponentRefusedError("chunk_size must be at least 1")
    if overlap < 0 or overlap >= size:
        raise ComponentRefusedError("chunk_overlap must be >= 0 and smaller than chunk_size")
    pieces = [p for p in (text.split(separator) if separator else [text]) if p]
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        joined = f"{current}{separator}{piece}" if current else piece
        if len(joined) <= size:
            current = joined
            continue
        if current:
            chunks.append(current)
            tail = current[-overlap:] if overlap else ""
            current = f"{tail}{separator}{piece}" if tail else piece
        else:
            current = piece
        while len(current) > size:
            chunks.append(current[:size])
            current = current[size - overlap :]
    if current:
        chunks.append(current)
    return chunks


# ----------------------------------------------------------------------------------- Loop
def parse_items(value: Any) -> list[str]:
    """Loop input -> item strings: a list, a JSON list in text, or one item per non-empty line."""
    if isinstance(value, list):
        return [x if isinstance(x, str) else json.dumps(x, ensure_ascii=False) for x in value]
    text = _text(value).strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            loaded = json.loads(text)
        except ValueError:
            loaded = None
        if isinstance(loaded, list):
            return parse_items(loaded)
    return [line for line in text.splitlines() if line.strip()]


def check_loop_bound(count: int, opts: V3Options, node_id: str) -> None:
    if count > opts.max_loop_items:
        raise ComponentRefusedError(
            f"Loop {node_id}: {count} items exceeds the limit of {opts.max_loop_items}"
        )


@dataclass(frozen=True)
class LoopPlan:
    loop_id: str
    #: Body node ids in execution order.
    body: list[str]
    #: ``(node id, output name)`` whose value is collected each iteration, or ``None``.
    result: tuple[str, str] | None


# ------------------------------------------------------------------------------------ SQL
_ALWAYS_DENY = frozenset(
    {"drop", "alter", "create", "attach", "detach", "pragma", "vacuum", "reindex"}
    | {"load_extension", "truncate", "grant", "revoke"}
)
_WRITE_FIRST = frozenset({"insert", "update", "delete", "replace"})


def _sql_tokens(sql: str) -> tuple[list[str], int]:
    """Words outside strings/comments and the count of top-level statements."""
    words: list[str] = []
    statements = 0
    pending = False  # tokens seen since the last ';'
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch.isspace():
            i += 1
        elif sql.startswith("--", i):
            end = sql.find("\n", i)
            i = n if end < 0 else end + 1
        elif sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            if end < 0:
                raise ComponentRefusedError("SQL has an unterminated comment")
            i = end + 2
        elif ch in "'\"`[":
            close = "]" if ch == "[" else ch
            j = i + 1
            while True:
                j = sql.find(close, j)
                if j < 0:
                    raise ComponentRefusedError("SQL has an unterminated quoted string")
                if close != "]" and sql.startswith(close, j + 1):
                    j += 2  # doubled quote escape
                    continue
                break
            pending = True
            i = j + 1
        elif ch == ";":
            if pending:
                statements += 1
                pending = False
            i += 1
            # anything after a terminator (other than comments/space) is a second statement
            rest = re.sub(r"--[^\n]*|/\*.*?\*/", "", sql[i:], flags=re.DOTALL).strip(" \t\r\n;")
            if rest:
                raise ComponentRefusedError("Multiple SQL statements are refused")
        else:
            m = re.match(r"[A-Za-z_][A-Za-z0-9_]*", sql[i:])
            if m:
                words.append(m.group(0).lower())
                i += len(m.group(0))
            else:
                i += 1
            pending = True
    if pending:
        statements += 1
    return words, statements


def validate_sql(sql: str, *, allow_write: bool) -> str:
    """Return the single statement to run, or raise ``ComponentRefusedError``.

    Read mode accepts SELECT / WITH / VALUES only. ``allow_write`` also accepts INSERT, UPDATE,
    DELETE and REPLACE. DDL, ATTACH, PRAGMA, VACUUM and extension loading are always refused,
    and so is more than one statement.
    """
    text = sql.strip()
    if not text:
        raise ComponentRefusedError("SQL query is empty")
    if len(text) > 10_000:
        raise ComponentRefusedError("SQL query is too long")
    words, statements = _sql_tokens(text)
    if statements != 1 or not words:
        raise ComponentRefusedError("Exactly one SQL statement is allowed")
    first = words[0]
    banned_always = sorted(set(words) & _ALWAYS_DENY)
    if banned_always:
        raise ComponentRefusedError(f"SQL keyword {banned_always[0].upper()} is refused")
    if first in _WRITE_FIRST:
        if not allow_write:
            raise ComponentRefusedError(
                f"{first.upper()} is a write; writes are refused unless sql_allow_write is set"
            )
        return text.rstrip("; \t\r\n")
    if first not in {"select", "with", "values"}:
        raise ComponentRefusedError(f"SQL statement type {first.upper()!r} is refused")
    if not allow_write:
        writes = sorted(set(words) & {"insert", "update", "delete"})
        replace_into = any(
            a == "replace" and b == "into" for a, b in zip(words, words[1:], strict=False)
        )
        if writes or replace_into:
            raise ComponentRefusedError(
                "A data-modifying statement is refused in read-only mode "
                f"({(writes or ['REPLACE'])[0].upper()})"
            )
    return text.rstrip("; \t\r\n")


def _bind(parameters: Any) -> Any:
    if parameters is None:
        return ()
    scalar = (str, int, float, bool, type(None))
    if isinstance(parameters, (list, tuple)):
        if not all(isinstance(p, scalar) for p in parameters):
            raise ComponentRefusedError("SQL parameters must be scalars")
        return tuple(parameters)
    if isinstance(parameters, dict):
        if not all(isinstance(k, str) and isinstance(v, scalar) for k, v in parameters.items()):
            raise ComponentRefusedError("SQL parameters must be a name -> scalar mapping")
        return dict(parameters)
    raise ComponentRefusedError("SQL parameters must be a list or an object")


def _fetch_capped(
    cursor: Any, cols: list[str], opts: V3Options
) -> tuple[list[dict[str, Any]], bool]:
    """Fetch row by row, refusing as soon as the running size passes ``sql_max_bytes``."""
    rows: list[dict[str, Any]] = []
    total = 2
    truncated = False
    while True:
        row = cursor.fetchone()
        if row is None:
            break
        if len(rows) >= opts.sql_max_rows:
            truncated = True
            break
        # Reject on the raw value size first (no str() of a large blob).
        floor = sum(len(v) if isinstance(v, (bytes, bytearray, str)) else 0 for v in row)
        if total + floor > opts.sql_max_bytes:
            raise ComponentRefusedError(
                f"SQL result is over the {opts.sql_max_bytes} byte limit "
                f"(stopped after {len(rows)} rows)"
            )
        record = dict(zip(cols, row, strict=False))
        total += len(json.dumps(record, ensure_ascii=False, default=str).encode()) + 2
        if total > opts.sql_max_bytes:
            raise ComponentRefusedError(
                f"SQL result is over the {opts.sql_max_bytes} byte limit "
                f"(stopped after {len(rows) + 1} rows)"
            )
        rows.append(record)
    return rows, truncated


async def run_sql(
    opts: V3Options, sql: str, parameters: Any = None, *, allow_write: bool | None = None
) -> dict[str, Any]:
    """Validate and run one parameterised statement on the configured connection."""
    write = opts.sql_allow_write if allow_write is None else allow_write
    statement = validate_sql(sql, allow_write=write)
    bound = _bind(parameters)
    if not opts.sql_connection:
        raise ComponentRefusedError("No SQL connection configured (V3Options.sql_connection)")
    from nexura_runtime.integrations.sql import default_sql_connection_manager
    from nexura_runtime.integrations.sql.errors import SqlReadOnlyViolationError

    manager = opts.sql_manager or default_sql_connection_manager
    pool = await manager.get_pool(opts.sql_connection, opts.workspace_id)
    if write and pool.config.read_only:
        raise ComponentRefusedError(
            f"Connection {opts.sql_connection!r} is configured read-only; writes refused"
        )
    async with pool.connection() as conn:
        raw = conn.raw_connection
        started = time.monotonic()
        raw.set_progress_handler(
            lambda: 1 if time.monotonic() - started > opts.sql_timeout_seconds else 0, 100
        )
        # SQLite refuses to build any single value/row larger than the cap, so a huge
        # zeroblob()/replace() result is never allocated on the Python side.
        old_limit = raw.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, max(1, opts.sql_max_bytes))
        try:
            if not write:
                raw.execute("PRAGMA query_only = ON")
            cursor = raw.cursor()
            cursor.execute(statement, bound)
            if cursor.description:
                cols = [c[0] for c in cursor.description]
                rows, truncated = _fetch_capped(cursor, cols, opts)
                return {
                    "columns": cols,
                    "rows": rows,
                    "row_count": len(rows),
                    "truncated": truncated,
                }
            raw.commit()
            return {"rows_affected": cursor.rowcount}
        except sqlite3.DataError as exc:
            raise ComponentRefusedError(
                f"SQL result value is over the {opts.sql_max_bytes} byte limit ({exc})"
            ) from exc
        except sqlite3.OperationalError as exc:
            if "too big" in str(exc).lower():
                raise ComponentRefusedError(
                    f"SQL result value is over the {opts.sql_max_bytes} byte limit"
                ) from exc
            if "interrupted" in str(exc).lower():
                raise ComponentRefusedError("SQL query timed out") from exc
            if "readonly" in str(exc).lower() or "query_only" in str(exc).lower():
                raise ComponentRefusedError(f"SQL write refused by the database: {exc}") from exc
            raise ComponentRefusedError(f"SQL error: {exc}") from exc
        except (sqlite3.Warning, sqlite3.ProgrammingError, sqlite3.IntegrityError) as exc:
            raw.rollback()
            raise ComponentRefusedError(f"SQL error: {exc}") from exc
        except SqlReadOnlyViolationError as exc:
            raise ComponentRefusedError(str(exc)) from exc
        finally:
            raw.set_progress_handler(None, 0)
            raw.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, old_limit)
            if not write and not pool.config.read_only:
                raw.execute("PRAGMA query_only = OFF")


def sql_tool(opts: V3Options, fixed_query: str | None) -> Tool:
    """Tool for a SQLComponent. A literal ``query`` in the flow fixes the statement."""
    if fixed_query:
        validate_sql(fixed_query, allow_write=opts.sql_allow_write)

        @tool(
            name="sql_query",
            description="Run the flow's fixed, parameterised SQL query. Pass parameters only.",
        )
        async def sql_fixed(parameters: Any = None) -> Any:
            try:
                return await run_sql(opts, fixed_query, parameters)
            except ComponentRefusedError as exc:
                return {"error": "refused", "message": str(exc)}

        return sql_fixed

    @tool(
        name="sql_query",
        description="Run one parameterised read-only SQL statement. Use ? placeholders and "
        "pass values in parameters; never put values into the SQL text.",
    )
    async def sql_free(query: str, parameters: Any = None) -> Any:
        try:
            return await run_sql(opts, query, parameters)
        except ComponentRefusedError as exc:
            return {"error": "refused", "message": str(exc)}

    return sql_free


# --------------------------------------------------------------------------------- Memory
def _history_matches(messages: Sequence[Any], query: str, limit: int) -> list[dict[str, str]]:
    turns = [
        {"role": str(m.role), "content": m.content}
        for m in messages
        if getattr(m, "role", "") in ("user", "assistant") and getattr(m, "content", None)
    ]
    terms = {t for t in re.findall(r"\w+", query.lower()) if len(t) > 1}
    if terms:
        scored = []
        for idx, turn in enumerate(turns):
            overlap = len(terms & set(re.findall(r"\w+", str(turn["content"]).lower())))
            if overlap:
                scored.append((overlap, idx, turn))
        scored.sort(key=lambda t: (-t[0], t[1]))
        return [t[2] for t in scored[:limit]]
    return turns[-limit:]


async def search_memory(query: str, limit: int) -> dict[str, Any]:
    """Search the running session's saved history (never any other session's)."""
    scope = RUN_SCOPE.get()
    if scope is None or scope.store is None or scope.session_id is None:
        return {"error": "no_session", "message": "This run has no session history to search."}
    found: list[dict[str, str]] = []
    for key in scope.session_keys:
        snap = await scope.store.get(key)
        if snap is not None:
            found.extend(_history_matches(snap.messages, query, limit))
    return {"matches": found[:limit]}


def memory_tool(limit: int) -> Tool:
    @tool(
        name="search_memory",
        description="Search earlier messages of this conversation for a topic.",
    )
    async def memory_search(search_query: str = "") -> Any:
        return await search_memory(search_query, limit)

    return memory_search


# ------------------------------------------------------------------------------- Knowledge
def knowledge_name(value: Any, node_id: str) -> str:
    if isinstance(value, list) and value:
        value = value[0]
    if isinstance(value, dict):
        value = value.get("name") or value.get("value")
    name = _text(value).strip()
    if not _NAME.fullmatch(name):
        raise FlowBuildError(
            f"node {node_id}: knowledge base name {name!r} must be 1-64 of letters, digits, _ . -"
        )
    return name


def collection_id(name: str, scope: str, session_id: str | None) -> str:
    """Collection for ``name``; ``session`` scope hashes the session id into the id."""
    if scope == "session":
        digest = hashlib.sha256((session_id or "").encode()).hexdigest()[:16]
        return f"{name}::s::{digest}"
    return name


async def ingest_knowledge(
    opts: V3Options,
    name: str,
    texts: Sequence[str],
    *,
    session_id: str | None = None,
    dimensions: int | None = None,
) -> int:
    """Embed ``texts`` into the knowledge base ``name`` of this workspace (and session)."""
    from nexura_runtime.knowledge.vector import VectorRecord

    if opts.embedder is None or opts.vector_store is None:
        raise ComponentRefusedError("ingest needs V3Options.embedder and vector_store")
    coll = collection_id(name, opts.knowledge_scope, session_id)
    vectors = [list(await _maybe_await(opts.embedder(t))) for t in texts]
    dims = dimensions or (len(vectors[0]) if vectors else 1)
    await opts.vector_store.create_collection(opts.workspace_id, coll, dims)
    gen = await opts.vector_store.get_active_generation(opts.workspace_id, coll)
    records = [
        VectorRecord(
            id=f"{coll}:{i}",
            workspace_id=opts.workspace_id,
            collection_id=coll,
            generation_id=gen.generation_id,
            vector=vec,
            document_id=f"{name}-{i}",
            text=text,
        )
        for i, (text, vec) in enumerate(zip(texts, vectors, strict=True))
    ]
    return int(await opts.vector_store.upsert(opts.workspace_id, coll, records))


async def search_knowledge(
    opts: V3Options, name: str, query: str, top_k: int, session_id: str | None
) -> list[dict[str, Any]]:
    from nexura_runtime.knowledge.vector import VectorSearchQuery

    if opts.embedder is None or opts.vector_store is None:
        raise ComponentRefusedError("Knowledge needs V3Options.embedder and vector_store")
    coll = collection_id(name, opts.knowledge_scope, session_id)
    vector = list(await _maybe_await(opts.embedder(query)))
    hits = await opts.vector_store.search(
        opts.workspace_id,
        VectorSearchQuery(
            vector=vector,
            collection_id=coll,
            workspace_id=opts.workspace_id,
            top_k=min(top_k, opts.max_top_k),
        ),
    )
    return [
        {"text": h.text, "score": round(h.score, 6), "document_id": h.document_id} for h in hits
    ]


def knowledge_tool(opts: V3Options, name: str, top_k: int) -> Tool:
    @tool(
        name="search_knowledge",
        description=f"Search the knowledge base '{name}' and return the best passages.",
    )
    async def knowledge_search(search_query: str) -> Any:
        scope = RUN_SCOPE.get()
        try:
            hits = await search_knowledge(
                opts, name, search_query, top_k, scope.session_id if scope else None
            )
        except ComponentRefusedError as exc:
            return {"error": "refused", "message": str(exc)}
        return {"results": hits}

    return knowledge_search


def knowledge_defaults(opts: V3Options) -> V3Options:
    """Fill in an in-memory vector store when Knowledge nodes exist and none was given."""
    if opts.vector_store is not None:
        return opts
    from nexura_runtime.knowledge.vector import InMemoryVectorStore

    return V3Options(**{**opts.__dict__, "vector_store": InMemoryVectorStore()})


def format_hits(hits: Sequence[Mapping[str, Any]]) -> str:
    return "\n\n".join(str(h["text"]) for h in hits)
