# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""API router for knowledge ingestion, scoped retrieval, and citations."""

from __future__ import annotations

import asyncio
import base64
import binascii
import threading
import time
import weakref
from collections.abc import Callable
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field

from nexura_runtime.knowledge.hybrid import (
    DocumentLimitError,
    EmbeddingLineageMismatchError,
    FileTooLargeError,
    IndexedDocument,
    KnowledgeError,
    KnowledgeIndexBase,
    SearchHit,
    WorkspaceQuotaExceededError,
)
from nexura_runtime.service.api.models import PaginatedResponse
from nexura_runtime.service.api.ratelimit import SlidingWindowRateLimiter
from nexura_runtime.service.auth.errors import PermissionDeniedError
from nexura_runtime.service.auth.models import ActionPermission, Principal, WorkspaceRole
from nexura_runtime.service.auth.permissions import check_permission
from nexura_runtime.service.auth.resources import require_resource
from nexura_runtime.service.workspaces.repository import WorkspaceRepository

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


def _get_caller_context(request: Request) -> tuple[str, Principal | None, WorkspaceRole]:
    """Extract (workspace_id, principal, role) from request state."""
    workspace_id: str = getattr(request.state, "workspace_id", "default")
    principal: Principal | None = getattr(request.state, "principal", None)
    raw_role = getattr(
        request.state,
        "role",
        "admin" if (principal and principal.profile == "local-owner") else "viewer",
    )
    role: WorkspaceRole = (
        "admin" if raw_role == "admin" else ("editor" if raw_role == "editor" else "viewer")
    )
    return workspace_id, principal, role


def _require(permission: ActionPermission) -> Any:
    """Route dependency enforcing a workspace permission (local-owner is always allowed)."""

    def dependency(request: Request) -> None:
        _, principal, role = _get_caller_context(request)
        try:
            if principal is None:
                raise PermissionDeniedError("No authenticated principal on this request.")
            check_permission(principal, role, permission)
        except PermissionDeniedError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "PERMISSION_DENIED", "message": str(exc)},
            ) from exc

    return Depends(dependency)


_COLLECTION_RESOURCE = "knowledge_collection"
_COLLECTION_SEP = "\x1f"


def _workspace_collections(ws_repo: WorkspaceRepository, workspace_id: str) -> list[str]:
    """Collection ids registered by ``workspace_id`` (durable via the ownership table)."""
    prefix = f"{workspace_id}{_COLLECTION_SEP}"
    ids = ws_repo.list_workspace_resource_ids(_COLLECTION_RESOURCE, workspace_id)
    names = {r_id[len(prefix) :] for r_id in ids if r_id.startswith(prefix)}
    names.add("default")
    return sorted(names)


def _register_collection(
    ws_repo: WorkspaceRepository, workspace_id: str, collection_id: str, owner_id: str
) -> None:
    key = f"{workspace_id}{_COLLECTION_SEP}{collection_id}"
    if ws_repo.get_resource_ownership(_COLLECTION_RESOURCE, key) is None:
        ws_repo.record_resource_ownership(_COLLECTION_RESOURCE, key, workspace_id, owner_id)


def _assert_doc_ownership(request: Request, document_id: str) -> None:
    """Verify that document belongs to caller's workspace."""
    caller_ws, principal, _ = _get_caller_context(request)
    ws_repo: WorkspaceRepository = request.app.state.workspace_repo
    owner_ws = ws_repo.get_resource_workspace("document", document_id)

    if (
        owner_ws is None
        and principal
        and principal.profile == "local-owner"
        and caller_ws == "default"
    ):
        owner_ws = "default"
        ws_repo.record_resource_ownership(
            "document", document_id, "default", principal.principal_id
        )

    if owner_ws is not None and owner_ws != caller_ws:
        if principal is not None and principal.profile == "local-owner":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "CROSS_WORKSPACE_ACCESS_DENIED",
                    "message": (
                        f"Cross-workspace access denied: document '{document_id}' belongs "
                        f"to workspace '{owner_ws}', not '{caller_ws}'."
                    ),
                },
            )
        # Shared service: identical to a missing document (no tenant-id disclosure).
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "DOCUMENT_NOT_FOUND",
                "message": f"Document '{document_id}' not found.",
            },
        )


#: Guards only the creation of an app's own build lock (held for a dict lookup, never a build).
_LOCKS_GUARD = threading.Lock()


def _build_lock(state: Any) -> threading.Lock:
    """The app's knowledge index build lock (``app.state.knowledge_build_lock``).

    One per app (MVP WP7 review R2-L2): a module-wide lock made one app's lazy Postgres build,
    which can wait for the migration lock, block another app's build in the same process.
    ``create_api_app`` sets it; an app state without one gets one here, once.
    """
    lock = getattr(state, "knowledge_build_lock", None)
    if lock is None:
        with _LOCKS_GUARD:
            lock = getattr(state, "knowledge_build_lock", None)
            if lock is None:
                lock = threading.Lock()
                state.knowledge_build_lock = lock
    return lock


def _index(request: Request) -> KnowledgeIndexBase:
    """Workspace-scoped hybrid index, created lazily (SQLite, or Postgres with ``database_url``).

    Synchronous: use ``_aindex`` from async code. A Postgres service builds its index when the
    app is created (``create_api_app``), so this only builds on SQLite or after a test or
    operator replaced it with ``None``.
    """
    state = request.app.state
    index: KnowledgeIndexBase | None = getattr(state, "hybrid_knowledge_index", None)
    if index is None:
        with _build_lock(state):
            index = getattr(state, "hybrid_knowledge_index", None)
            if index is None:
                index = state.service_stores.knowledge_index(
                    getattr(state, "vector_store", None), state.knowledge_embedder
                )
                state.hybrid_knowledge_index = index
    return index


def _collection_lock(request: Request, workspace_id: str, collection_id: str) -> asyncio.Lock:
    """The app's lock for one collection, held by an upload (ingest and registration) and by
    a collection delete (index delete and unregistration), so an upload never lands between
    the two steps of a delete: it runs wholly before (and is deleted with the collection) or
    wholly after (and finds a fresh collection)."""
    state = request.app.state
    # Weak values: a lock lives only while someone holds or waits on it, so collection ids
    # named by uploads do not accumulate for the life of the process.
    locks: weakref.WeakValueDictionary[tuple[str, str], asyncio.Lock] | None = getattr(
        state, "knowledge_collection_locks", None
    )
    if locks is None:
        locks = weakref.WeakValueDictionary()
        state.knowledge_collection_locks = locks
    key = (workspace_id, collection_id)
    lock = locks.get(key)
    if lock is None:
        lock = locks[key] = asyncio.Lock()
    return lock


async def _aindex(request: Request) -> KnowledgeIndexBase:
    """``_index`` for async callers. Building a Postgres index runs its migrations (the shared
    migration lock, ``CREATE EXTENSION``) and a catalog query, so it runs in a worker thread
    and never on the event loop (MVP WP7 review M2); concurrent first callers build it once."""
    state = request.app.state
    index: KnowledgeIndexBase | None = getattr(state, "hybrid_knowledge_index", None)
    if index is not None:
        return index
    stores = getattr(state, "service_stores", None)
    if stores is not None and getattr(stores, "database_url", None):
        return await asyncio.to_thread(_index, request)
    return _index(request)


async def _io[T](index: KnowledgeIndexBase, fn: Callable[[], T]) -> T:
    """Run a synchronous index call; in a worker thread when the backend waits on the network
    or a lock (Postgres, MVP WP7), inline otherwise (SQLite, unchanged)."""
    if index.blocking_io:
        return await asyncio.to_thread(fn)
    return fn()


DEFAULT_RETRIEVAL_RATE_LIMIT = 120  # retrieve + answer requests per workspace per minute


def _check_retrieval_rate(request: Request, workspace_id: str) -> None:
    """Per-workspace sliding-window limit on retrieve/answer (``state.knowledge_rate_limit``)."""
    state = request.app.state
    limiter: SlidingWindowRateLimiter | None = getattr(state, "knowledge_rate_limiter", None)
    if limiter is None:
        limiter = SlidingWindowRateLimiter()
        state.knowledge_rate_limiter = limiter
    limit = int(getattr(state, "knowledge_rate_limit", DEFAULT_RETRIEVAL_RATE_LIMIT))
    allowed, retry_after = limiter.check(workspace_id, limit)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "RATE_LIMITED",
                "message": f"Too many knowledge queries; retry in {retry_after:.0f} seconds.",
            },
            headers={"Retry-After": str(max(1, int(retry_after + 0.999)))},
        )


def _safe_filename(name: str) -> bool:
    """True unless the name has path separators, NUL, or a ``.``/``..`` segment (``a..b`` is ok)."""
    if not name or "\x00" in name or "/" in name or "\\" in name:
        return False
    return name not in (".", "..")


def _knowledge_http_error(exc: KnowledgeError) -> HTTPException:
    """Map a hybrid-index error to an HTTP error carrying its stable code."""
    too_big = isinstance(exc, (FileTooLargeError, WorkspaceQuotaExceededError, DocumentLimitError))
    code = 413 if too_big else (409 if isinstance(exc, EmbeddingLineageMismatchError) else 400)
    return HTTPException(
        status_code=code,
        detail={"code": exc.code, "message": str(exc)},
    )


def _doc_summary(d: IndexedDocument) -> dict[str, Any]:
    """Public document record used by list/upload responses."""
    return {
        "document_id": d.document_id,
        "collection_id": d.collection_id,
        "filename": d.title,
        "title": d.title,
        "source_uri": d.source_uri,
        "mime_type": d.mime_type,
        "content_hash": d.content_hash,
        "text_length": d.text_length,
        "byte_size": d.byte_size,
        "chunk_count": d.chunk_count,
        "status": d.status,
        "pages_count": 1,
        "created_at": d.created_at,
        "updated_at": d.updated_at,
        "metadata": d.metadata,
    }


def _hit_citation(hit: SearchHit) -> dict[str, Any]:
    """Citation payload (legacy fields plus title, chunk index, source URI and highlights)."""
    c = hit.citation
    return {
        "citation_id": c.citation_id,
        "document_id": c.document_id,
        "origin": c.title,
        "title": c.title,
        "chunk_id": c.chunk_id,
        "chunk_index": c.chunk_index,
        "page_start": c.page_start,
        "page_end": c.page_end,
        "start_char": c.start_char,
        "end_char": c.end_char,
        "source_uri": c.source_uri,
        "snippet": hit.snippet,
        "highlights": [list(s) for s in hit.highlights_cp],
        "highlights_utf16": [list(s) for s in hit.highlights],
        "matched_by": hit.matched_by,
    }


class CreateCollectionRequest(BaseModel):
    collection_id: str = Field(description="Unique collection identifier")
    name: str = Field(description="Display name of the collection")
    description: str | None = Field(default=None, description="Optional description")


class IngestDocumentRequest(BaseModel):
    collection_id: str = Field(default="default", description="Target collection ID")
    filename: str = Field(description="Document filename")
    content: str = Field(description="Text content, or base64 when content_encoding is base64")
    mime_type: str = Field(default="text/plain", description="MIME type")
    content_encoding: Literal["text", "base64"] = Field(
        default="text", description="Use base64 for binary files such as PDF"
    )
    source_uri: str | None = Field(default=None, description="Optional source URI for citations")
    metadata: dict[str, Any] | None = Field(default=None, description="Additional metadata")


class RetrieveQueryRequest(BaseModel):
    query: str = Field(description="Search or retrieval query")
    collection_id: str = Field(default="default", description="Target collection ID")
    top_k: int = Field(default=5, ge=1, le=50, description="Maximum citations to retrieve")
    dense_weight: float = Field(default=0.7, ge=0.0, le=1.0)
    lexical_weight: float = Field(default=0.3, ge=0.0, le=1.0)
    min_score_threshold: float = Field(default=0.0, ge=0.0, le=1.0)


class AnswerQueryRequest(BaseModel):
    query: str = Field(description="Question to answer using grounded knowledge base")
    collection_id: str = Field(default="default", description="Target collection ID")
    language: Literal["en", "hi"] = Field(default="en", description="Output language")


@router.get("/collections")
async def list_collections(request: Request) -> dict[str, Any]:
    """List knowledge collections in the caller workspace."""
    caller_ws, _, _ = _get_caller_context(request)
    ws_repo: WorkspaceRepository = request.app.state.workspace_repo
    index = await _aindex(request)
    collections = _workspace_collections(ws_repo, caller_ws)

    def statuses() -> tuple[dict[str, Any], dict[str, Any]]:
        return index.embedding_status(caller_ws), {
            cid: index.embedding_status(caller_ws, cid) for cid in collections
        }

    default_status, collection_status = await _io(index, statuses)
    return {
        "workspace_id": caller_ws,
        "collections": collections,
        **default_status,
        "collection_status": collection_status,
    }


@router.get("/status")
async def knowledge_status(
    request: Request, collection_id: str = Query(default="default")
) -> dict[str, Any]:
    """Configured and persisted collection embedding lineage (no provider calls)."""
    caller_ws, _, _ = _get_caller_context(request)
    index = await _aindex(request)
    return await _io(index, lambda: index.embedding_status(caller_ws, collection_id))


@router.post("/collections/{collection_id}/reindex", dependencies=[_require("edit")])
async def reindex_collection(request: Request, collection_id: str) -> dict[str, Any]:
    """Explicitly rebuild this workspace's collection using the configured embedder."""
    caller_ws, principal, _ = _get_caller_context(request)
    try:
        require_resource(principal, "knowledge", collection_id)
        return await (await _aindex(request)).reindex(caller_ws, collection_id)
    except PermissionDeniedError as exc:
        raise HTTPException(
            status_code=403, detail={"code": exc.code, "message": str(exc)}
        ) from exc
    except KnowledgeError as exc:
        raise _knowledge_http_error(exc) from exc


@router.post("/collections", status_code=status.HTTP_201_CREATED, dependencies=[_require("edit")])
async def create_collection(request: Request, payload: CreateCollectionRequest) -> dict[str, Any]:
    """Create a new knowledge collection in the workspace."""
    caller_ws, principal, _ = _get_caller_context(request)
    cid = payload.collection_id.strip()
    if not cid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_COLLECTION_ID", "message": "collection_id cannot be empty."},
        )

    ws_repo: WorkspaceRepository = request.app.state.workspace_repo
    _register_collection(ws_repo, caller_ws, cid, principal.principal_id if principal else "")

    return {
        "workspace_id": caller_ws,
        "collection_id": cid,
        "name": payload.name,
        "description": payload.description,
    }


@router.delete("/collections/{collection_id}", dependencies=[_require("edit")])
async def delete_collection(request: Request, collection_id: str) -> dict[str, Any]:
    """Delete a collection of the caller's workspace with its documents and index.

    Removes every document of the collection (text, chunks, vectors, keyword entries) and the
    collection's embedding lineage, then the collection itself. Needs the ``edit`` permission
    (and, for a resource-scoped key, this collection). ``default`` always exists: deleting it
    empties it. Agents that still name the collection find nothing in it. 404
    ``COLLECTION_NOT_FOUND`` for a collection this workspace does not have.
    """
    caller_ws, principal, _ = _get_caller_context(request)
    try:
        require_resource(principal, "knowledge", collection_id)
    except PermissionDeniedError as exc:
        raise HTTPException(
            status_code=403, detail={"code": exc.code, "message": str(exc)}
        ) from exc
    ws_repo: WorkspaceRepository = request.app.state.workspace_repo
    index = await _aindex(request)
    async with _collection_lock(request, caller_ws, collection_id):
        return await _delete_collection_locked(request, ws_repo, index, caller_ws, collection_id)


async def _delete_collection_locked(
    request: Request,
    ws_repo: WorkspaceRepository,
    index: KnowledgeIndexBase,
    caller_ws: str,
    collection_id: str,
) -> dict[str, Any]:
    key = f"{caller_ws}{_COLLECTION_SEP}{collection_id}"
    registered = ws_repo.get_resource_ownership(_COLLECTION_RESOURCE, key) is not None
    has_docs = bool(await _io(index, lambda: index.list_documents(caller_ws, collection_id)))
    if not registered and not has_docs and collection_id != "default":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "COLLECTION_NOT_FOUND",
                "message": f"Collection '{collection_id}' not found.",
            },
        )
    deleted = await _io(index, lambda: index.delete_collection(caller_ws, collection_id))
    for document_id in deleted:
        ws_repo.delete_resource_ownership("document", document_id)
    if registered:
        ws_repo.delete_resource_ownership(_COLLECTION_RESOURCE, key)
    return {
        "deleted": True,
        "collection_id": collection_id,
        "documents_deleted": len(deleted),
    }


@router.get("/limits")
async def knowledge_limits(request: Request) -> dict[str, Any]:
    """Upload size limits and the caller workspace's current usage."""
    caller_ws, _, _ = _get_caller_context(request)
    index = await _aindex(request)

    def read() -> dict[str, Any]:
        return {
            **index.limits(), "workspace_used_bytes": index.workspace_usage(caller_ws),
            **index.embedding_status(caller_ws),
        }

    return await _io(index, read)


@router.get("/documents")
async def list_documents(
    request: Request,
    collection_id: str = Query(default="default"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> PaginatedResponse[dict[str, Any]]:
    """List documents in a collection scoped to caller's workspace."""
    caller_ws, _, _ = _get_caller_context(request)
    index = await _aindex(request)
    owned = await _io(index, lambda: index.list_documents(caller_ws, collection_id))
    total = len(owned)
    items = [_doc_summary(d) for d in owned[offset : offset + limit]]
    return PaginatedResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        has_more=(offset + limit) < total,
    )


@router.post(
    "/documents/upload", status_code=status.HTTP_201_CREATED, dependencies=[_require("edit")]
)
async def upload_document(
    request: Request, payload: IngestDocumentRequest, response: Response
) -> dict[str, Any]:
    """Ingest a file (201 created/replaced; 200 when deduplicated); dedup and replace by title."""
    caller_ws, principal, _ = _get_caller_context(request)
    index = await _aindex(request)
    ws_repo: WorkspaceRepository = request.app.state.workspace_repo

    filename = payload.filename.strip()
    if not _safe_filename(filename):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "PATH_TRAVERSAL_REJECTED",
                "message": "Filename must not contain path separators, NUL, or be '.' or '..'.",
            },
        )

    # Refuse oversized payloads before decoding them.
    max_chars = index.max_file_bytes * 4 // 3 + 8
    if len(payload.content) > max_chars:
        raise _knowledge_http_error(
            FileTooLargeError(f"File exceeds the per-file limit of {index.max_file_bytes} bytes.")
        )
    if payload.content_encoding == "base64":
        try:
            raw_bytes = base64.b64decode(payload.content, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": "INVALID_ENCODING", "message": "content is not valid base64."},
            ) from exc
    else:
        raw_bytes = payload.content.encode("utf-8")

    async with _collection_lock(request, caller_ws, payload.collection_id):
        return await _ingest_locked(
            request, response, index, ws_repo, caller_ws, principal, payload, filename, raw_bytes
        )


async def _ingest_locked(
    request: Request,
    response: Response,
    index: KnowledgeIndexBase,
    ws_repo: WorkspaceRepository,
    caller_ws: str,
    principal: Principal | None,
    payload: IngestDocumentRequest,
    filename: str,
    raw_bytes: bytes,
) -> dict[str, Any]:
    try:
        outcome = await index.ingest(
            workspace_id=caller_ws,
            collection_id=payload.collection_id,
            filename=filename,
            data=raw_bytes,
            mime_type=payload.mime_type,
            source_uri=payload.source_uri,
            metadata=payload.metadata,
        )
    except KnowledgeError as exc:
        raise _knowledge_http_error(exc) from exc
    doc = outcome.document
    if outcome.deduplicated:
        response.status_code = status.HTTP_200_OK

    actor_id = principal.principal_id if principal else "anonymous"
    owned = ws_repo.get_resource_ownership("document", doc.document_id)
    existing_owner = owned[0] if owned else None
    if existing_owner is not None and existing_owner != caller_ws:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "RESOURCE_OWNED_ELSEWHERE",
                "message": "Document id is already owned by another workspace.",
            },
        )
    if existing_owner is None:
        ws_repo.record_resource_ownership("document", doc.document_id, caller_ws, actor_id)
    _register_collection(ws_repo, caller_ws, payload.collection_id, actor_id)

    return {
        **_doc_summary(doc),
        "indexed_records": doc.chunk_count,
        "deduplicated": outcome.deduplicated,
        "replaced": outcome.replaced,
        "metadata_updated": outcome.metadata_updated,
        "workspace_id": caller_ws,
    }


@router.get("/documents/{document_id}")
async def get_document(request: Request, document_id: str) -> dict[str, Any]:
    """Retrieve document details and text content."""
    _assert_doc_ownership(request, document_id)
    caller_ws, _, _ = _get_caller_context(request)
    index = await _aindex(request)
    doc = await _io(index, lambda: index.get_document(caller_ws, document_id))
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "DOCUMENT_NOT_FOUND",
                "message": f"Document '{document_id}' not found.",
            },
        )
    text = await _io(index, lambda: index.get_document_text(caller_ws, document_id))
    return {**_doc_summary(doc), "text": text}


@router.get("/documents/{document_id}/chunks/{chunk_index}")
async def get_chunk(request: Request, document_id: str, chunk_index: int) -> dict[str, Any]:
    """Return one chunk (text, span, title, source URI): the target of citation footnote links."""
    _assert_doc_ownership(request, document_id)
    caller_ws, _, _ = _get_caller_context(request)
    index = await _aindex(request)
    chunk = await _io(index, lambda: index.get_chunk(caller_ws, document_id, chunk_index))
    if chunk is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "CHUNK_NOT_FOUND", "message": "Chunk not found."},
        )
    return chunk


@router.delete("/documents/{document_id}", dependencies=[_require("edit")])
async def delete_document(request: Request, document_id: str) -> dict[str, Any]:
    """Delete a document and purge its chunks, vectors and keyword-index entries."""
    _assert_doc_ownership(request, document_id)
    caller_ws, _, _ = _get_caller_context(request)
    ws_repo: WorkspaceRepository = request.app.state.workspace_repo
    index = await _aindex(request)
    if not await _io(index, lambda: index.delete_document(caller_ws, document_id)):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "DOCUMENT_NOT_FOUND",
                "message": f"Document '{document_id}' not found.",
            },
        )
    ws_repo.delete_resource_ownership("document", document_id)
    return {"deleted": True, "document_id": document_id}


@router.post("/retrieve")
async def retrieve_knowledge(request: Request, payload: RetrieveQueryRequest) -> dict[str, Any]:
    """Hybrid (vector + keyword, reciprocal rank fusion) search returning cited, ranked hits.

    The keyword ranker is FTS5 ``bm25`` on SQLite and ``ts_rank_cd`` on Postgres."""
    caller_ws, principal, _ = _get_caller_context(request)
    try:
        require_resource(principal, "knowledge", payload.collection_id)
    except PermissionDeniedError as exc:
        raise HTTPException(
            status_code=403, detail={"code": exc.code, "message": str(exc)}
        ) from exc
    _check_retrieval_rate(request, caller_ws)
    index = await _aindex(request)
    t0 = time.perf_counter()
    try:
        hits = await index.search(
            workspace_id=caller_ws,
            query=payload.query,
            collection_id=payload.collection_id,
            top_k=payload.top_k,
            vector_weight=payload.dense_weight,
            keyword_weight=payload.lexical_weight,
            min_score=payload.min_score_threshold,
        )
    except KnowledgeError as exc:
        raise _knowledge_http_error(exc) from exc
    latency_ms = round((time.perf_counter() - t0) * 1000, 2)

    items_list = [
        {
            "chunk_id": h.chunk_id,
            "document_id": h.document_id,
            "origin": h.title,
            "title": h.title,
            "chunk_index": h.chunk_index,
            "text": h.text,
            "score": round(h.score, 6),
            "dense_score": round(h.vector_score, 4) if h.vector_score is not None else None,
            "lexical_score": None,
            "vector_rank": h.vector_rank,
            "keyword_rank": h.keyword_rank,
            "matched_by": h.matched_by,
        }
        for h in hits
    ]
    return {
        "query": payload.query,
        "collection_id": payload.collection_id,
        "workspace_id": caller_ws,
        "has_sufficient_evidence": bool(hits),
        "total_tokens": sum(len(h.text.split()) for h in hits),
        "citations": [_hit_citation(h) for h in hits],
        "items": items_list,
        "latency_ms": latency_ms,
    }


@router.post("/answer")
async def answer_with_citations(request: Request, payload: AnswerQueryRequest) -> dict[str, Any]:
    """Grounded offline answer built from the top hybrid hit, with citations."""
    caller_ws, principal, _ = _get_caller_context(request)
    try:
        require_resource(principal, "knowledge", payload.collection_id)
    except PermissionDeniedError as exc:
        raise HTTPException(
            status_code=403, detail={"code": exc.code, "message": str(exc)}
        ) from exc
    _check_retrieval_rate(request, caller_ws)
    t0 = time.perf_counter()
    try:
        hits = await (await _aindex(request)).search(
            workspace_id=caller_ws,
            query=payload.query,
            collection_id=payload.collection_id,
            top_k=5,
        )
    except KnowledgeError as exc:
        raise _knowledge_http_error(exc) from exc
    retrieval_ms = round((time.perf_counter() - t0) * 1000, 2)
    citations_list = [_hit_citation(h) for h in hits]
    sufficient = bool(hits)

    if not sufficient:
        if payload.language == "hi":
            answer_text = "ज्ञानकोष में इस प्रश्न का उत्तर देने के लिए कोई प्रासंगिक दस्तावेज़ नहीं मिला।"
        else:
            answer_text = "No relevant documents found in knowledge base to answer the query."
    else:
        top = hits[0]
        ref = top.citation.citation_id
        if payload.language == "hi":
            answer_text = f"दस्तावेज़ {top.title} के अनुसार: {top.text.strip()} {ref}"
        else:
            page = top.citation.page_start
            where = f"{top.title} (Page {page})" if page is not None else top.title
            answer_text = f"According to {where}, {top.text.strip()} {ref}"

    return {
        "query": payload.query,
        "answer": answer_text,
        "has_sufficient_evidence": sufficient,
        "citations": citations_list,
        "retrieval_duration_ms": retrieval_ms,
        "generation_duration_ms": 0.0,
        "model": "fake-deterministic (Local Offline Fixture)",
    }
