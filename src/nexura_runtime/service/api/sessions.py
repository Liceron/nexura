# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""API endpoints for persisted conversation sessions and chat playground."""

from __future__ import annotations

import ast
import json
import logging
import math
import os
import re
import time
import uuid
from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import aclosing
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from nexura_agents.agent import Agent
from nexura_agents.errors import MaxTurnsExceededError, UsageLimitExceededError
from nexura_agents.events import RunEvent, RunEventType
from nexura_agents.limits import UsageLimits
from nexura_agents.models.fake import FakeModel
from nexura_agents.runner import Runner
from nexura_agents.session import SQLiteSessionStore
from nexura_agents.tools import Tool
from nexura_agents.types import Message, ToolCall, Usage
from pydantic import BaseModel, Field

from nexura_runtime.observability.run_diagnostics import redact_secret_text
from nexura_runtime.service.api.chat_models import (
    DEFAULT_CHAT_MODEL,
    ChatModelRegistry,
    ChatModelUnavailableError,
)
from nexura_runtime.service.api.knowledge import _check_retrieval_rate, _hit_citation, _index
from nexura_runtime.service.api.models import PaginatedResponse
from nexura_runtime.service.auth.errors import PermissionDeniedError
from nexura_runtime.service.auth.models import ActionPermission, Principal, WorkspaceRole
from nexura_runtime.service.auth.permissions import check_permission
from nexura_runtime.service.workspaces.repository import WorkspaceRepository

router = APIRouter(prefix="/sessions", tags=["sessions"])
chat_router = APIRouter(prefix="/chat", tags=["sessions"])

DEFAULT_CHAT_USAGE_LIMITS = UsageLimits(
    request_limit=10, total_tokens_limit=50_000, allow_unknown_usage=True
)


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


def _claim_session(
    ws_repo: WorkspaceRepository, session_id: str, workspace_id: str, actor_id: str
) -> None:
    """Record session ownership without ever reassigning it to another workspace.

    Session ids are client-supplied and share one global key space, so an existing owner
    in a different workspace is a conflict (409). Must be called without an intervening
    ``await`` between the check and the write so the claim is atomic on the event loop.
    """
    owned = ws_repo.get_resource_ownership("session", session_id)
    if owned is not None:
        if owned[0] != workspace_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "SESSION_ID_CONFLICT",
                    "message": f"Session id '{session_id}' is already in use.",
                },
            )
        return
    ws_repo.record_resource_ownership("session", session_id, workspace_id, actor_id)


def _assert_session_ownership(request: Request, session_id: str) -> None:
    """Verify that the conversation session belongs to the caller's workspace."""
    caller_ws, principal, _ = _get_caller_context(request)
    ws_repo: WorkspaceRepository = request.app.state.workspace_repo
    owner_ws = ws_repo.get_resource_workspace("session", session_id)

    # In local-owner mode with unmapped resources, default workspace owns it
    if (
        owner_ws is None
        and principal
        and principal.profile == "local-owner"
        and caller_ws == "default"
    ):
        owner_ws = "default"
        ws_repo.record_resource_ownership("session", session_id, "default", principal.principal_id)

    if owner_ws is not None and owner_ws != caller_ws:
        if principal is not None and principal.profile == "local-owner":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "CROSS_WORKSPACE_ACCESS_DENIED",
                    "message": (
                        f"Cross-workspace access denied: session '{session_id}' belongs to "
                        f"workspace '{owner_ws}', not '{caller_ws}'."
                    ),
                },
            )
        # Shared service: identical to a missing session so tenants cannot probe ids.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "SESSION_NOT_FOUND", "message": f"Session '{session_id}' not found."},
        )


class CreateSessionRequest(BaseModel):
    """POST /sessions body: optional id (``[A-Za-z0-9_.:-]``, 1-128 chars), title (max 200)."""

    session_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9_.:-]{1,128}$",
        description="Optional custom session id (letters, digits, _ . : -; max 128)",
    )
    title: str | None = Field(
        default=None, max_length=200, description="Optional session title, at most 200 characters"
    )


MAX_CHAT_MESSAGE_CHARS = 8_000


class ChatRequest(BaseModel):
    """POST /sessions/{id}/chat body: prompt, model name, language and run limits."""

    message: str = Field(
        max_length=MAX_CHAT_MESSAGE_CHARS,
        description=f"User prompt text, at most {MAX_CHAT_MESSAGE_CHARS} characters.",
    )
    model: str = Field(
        default="fake-deterministic",
        description=(
            "Name of an operator-configured chat model (see GET /chat/models). "
            "Defaults to the offline fake-deterministic fixture."
        ),
    )
    language: Literal["en", "hi"] = Field(
        default="en",
        description="Conversation language: 'en' for English or 'hi' for Hindi.",
    )
    max_turns: int = Field(default=5, ge=1, le=20)
    timeout: float = Field(default=30.0, ge=1.0, le=120.0)
    agent_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description=(
            "Chat with an agent saved in this workspace instead of the playground demo. "
            "The agent's own model is used (it must be in the allowlist); write tools are "
            "never run from chat."
        ),
    )


# Builtin tools for conversation playground
_CALC_MAX_EXPR_CHARS = 200
_CALC_MAX_LITERAL_CHARS = 30
_CALC_MAX_DEPTH = 24
_CALC_MAX_EXPONENT = 1000
_CALC_MAX_RESULT_BITS = 10_000


def _calc_eval(node: ast.AST, depth: int = 0) -> int | float:
    if depth > _CALC_MAX_DEPTH:
        raise ValueError("expression too deeply nested")
    if isinstance(node, ast.Constant):
        if type(node.value) in (int, float):
            return node.value  # type: ignore[return-value]
        raise ValueError("unsupported literal")
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub | ast.UAdd):
        val = _calc_eval(node.operand, depth + 1)
        return -val if isinstance(node.op, ast.USub) else +val
    if isinstance(node, ast.BinOp):
        left = _calc_eval(node.left, depth + 1)
        right = _calc_eval(node.right, depth + 1)
        op = node.op
        if isinstance(op, ast.Add):
            result = left + right
        elif isinstance(op, ast.Sub):
            result = left - right
        elif isinstance(op, ast.Mult):
            result = left * right
        elif isinstance(op, ast.Div):
            result = left / right
        elif isinstance(op, ast.FloorDiv):
            result = left // right
        elif isinstance(op, ast.Pow):
            if abs(right) > _CALC_MAX_EXPONENT:
                raise ValueError("exponent too large")
            if isinstance(left, int) and isinstance(right, int) and right >= 0:
                if left.bit_length() * right > _CALC_MAX_RESULT_BITS:
                    raise ValueError("result too large")
                result = left**right
            else:
                if left < 0 and right != int(right):
                    raise ValueError("result is not a real number")
                result = float(left) ** right
                if isinstance(result, complex):
                    raise ValueError("result is not a real number")
        else:
            raise ValueError("unsupported operator")
        if isinstance(result, int) and result.bit_length() > _CALC_MAX_RESULT_BITS:
            raise ValueError("result too large")
        if isinstance(result, complex) or (isinstance(result, float) and not math.isfinite(result)):
            raise ValueError("result is not a finite real number")
        return result
    raise ValueError("unsupported expression")


def calculate(expression: str) -> str:
    """Calculate a basic arithmetic expression with a bounded AST evaluator (no eval)."""
    clean = expression.replace(" ", "")
    if len(clean) > _CALC_MAX_EXPR_CHARS:
        return "Error: Mathematical expression is too long."
    for ch in clean:
        if ch not in "0123456789+-*/.()":
            return f"Error: Unsupported characters in mathematical expression: {expression}"
    if re.search(rf"[0-9.]{{{_CALC_MAX_LITERAL_CHARS + 1},}}", clean):
        return "Calculation error: numeric literal too long"
    try:
        result = _calc_eval(ast.parse(clean, mode="eval").body)
        return str(result)
    except (
        ValueError,
        TypeError,
        ArithmeticError,
        SyntaxError,
        RecursionError,
        MemoryError,
    ) as exc:
        return f"Calculation error: {exc}"


def get_order_status(order_id: str) -> dict[str, Any]:
    """Retrieve tracking and logistics status for an order."""
    return {
        "order_id": order_id,
        "status": "In Transit",
        "carrier": "Delhivery Express",
        "origin": "Bengaluru Hub",
        "destination": "New Delhi",
        "estimated_delivery": "Within 24 hours",
    }


PLAYGROUND_TOOLS = [
    Tool(
        "calculate", calculate, description="Evaluate arithmetic calculations and math expressions."
    ),
    Tool(
        "get_order_status",
        get_order_status,
        description="Lookup shipping and delivery status for an order.",
    ),
]


KNOWLEDGE_SEARCH_TOP_K = 5
KNOWLEDGE_QUERY_MAX_CHARS = 512
_KNOWLEDGE_KEYWORD_RE = re.compile(
    r"\b(?:search(?:es|ing)?|according\s+to|docs|documents?|knowledge)\b", re.IGNORECASE
)
_KNOWLEDGE_MIN_QUESTION_WORDS = 3
_SMALL_TALK_RE = re.compile(
    r"\b(?:how\s+are\s+(?:you|things)|how\s+do\s+you\s+do|how'?s\s+it\s+going|"
    r"what'?s\s+up|who\s+are\s+you|what\s+can\s+you\s+do|how\s+have\s+you\s+been)\b",
    re.IGNORECASE,
)
_CITATION_MARKER_RE = re.compile(r"\[(\^?\s*\d+(?:\s*[,\-\u2013]\s*\d+)*\s*)\]")


def looks_like_document_question(message: str, has_documents: bool = True) -> bool:
    """Deterministic playground rule for calling ``knowledge_search``.

    A whole-word keyword (search, according to, docs, document(s), knowledge) always
    triggers. A bare question (a '?' and at least three words) triggers only when the
    workspace has documents and the message is not small talk such as a greeting. The
    calculate and order-status triggers are checked first and win over this rule.
    """
    if _KNOWLEDGE_KEYWORD_RE.search(message):
        return True
    if not has_documents or "?" not in message or _SMALL_TALK_RE.search(message):
        return False
    return len(message.split()) >= _KNOWLEDGE_MIN_QUESTION_WORDS


def _neutralise(text: str) -> str:
    """Redact secrets, then defuse citation markers and line breaks in untrusted text.

    ``[n]`` becomes ``(n)`` and newlines become spaces, so document text cannot forge the
    tool's own ``[n] title: snippet`` lines. Everything but redaction keeps the length.
    """
    text = redact_secret_text(text)
    text = _CITATION_MARKER_RE.sub(lambda m: f"({m.group(1)})", text)
    return re.sub(r"[\r\n\u2028\u2029]", " ", text)


_DOCUMENT_ID_RE = re.compile(r"^doc_[0-9a-f]{12}$")


def _redact_uri(uri: str) -> str:
    """Strip query string, fragment and credentials from a source URI, then redact secrets."""
    base = re.split(r"[?#]", uri, maxsplit=1)[0]
    base = re.sub(r"^([A-Za-z][A-Za-z0-9+.-]*://)[^/@]*@", r"\1", base)
    return redact_secret_text(base)


def _safe_citation(citation: dict[str, Any]) -> dict[str, Any]:
    """Copy of a hit citation with title, origin and snippet neutralised.

    ``source_uri`` loses its query/fragment and credentials; ``document_id`` is kept only if it
    matches the platform id format (``doc_`` plus 12 hex digits).

    Highlight offsets index the snippet, so they are dropped if redaction changed its length.
    """
    out = dict(citation)
    for key in ("title", "origin"):
        if isinstance(out.get(key), str):
            out[key] = _neutralise(out[key])
    if isinstance(out.get("source_uri"), str):
        out["source_uri"] = _neutralise(_redact_uri(out["source_uri"]))
    doc_id = out.get("document_id")
    if isinstance(doc_id, str) and not _DOCUMENT_ID_RE.match(doc_id):
        out["document_id"] = None
    snippet = citation.get("snippet")
    if isinstance(snippet, str):
        out["snippet"] = _neutralise(snippet)
        if len(out["snippet"]) != len(snippet):
            out["highlights"] = []
            out["highlights_utf16"] = []
    return out


def _workspace_has_documents(request: Request, workspace_id: str) -> bool:
    index = _index(request)
    with index._lock:
        row = index._conn.execute(
            "SELECT 1 FROM hk_documents WHERE workspace_id = ? LIMIT 1", (workspace_id,)
        ).fetchone()
    return row is not None


def create_knowledge_search_tool(request: Request) -> Tool:
    """Playground tool searching only the calling workspace's hybrid knowledge index."""
    return knowledge_search_tool(request, _get_caller_context(request)[0])


def knowledge_search_tool(
    holder: Any, workspace_id: str, collections: Sequence[str] = ("default",)
) -> Tool:
    """``knowledge_search`` over ``collections`` of one workspace's hybrid knowledge index.

    ``holder`` is anything with ``.app.state`` (a request, or the app wrapped for code that
    runs outside a request, such as a workflow's agent node). Several collections are
    searched one by one and the best hits kept, renumbered ``[1]``, ``[2]``...
    """
    ids = list(dict.fromkeys(collections)) or ["default"]

    async def knowledge_search(query: str) -> str:
        """Search workspace documents; returns JSON with context and Studio-shaped citations."""
        if not _workspace_has_documents(holder, workspace_id):
            # Nothing to search: do not consume the workspace's retrieval rate limit.
            return json.dumps({"context": "", "citations": []})
        try:
            _check_retrieval_rate(holder, workspace_id)
        except HTTPException as exc:
            detail: dict[str, Any] = exc.detail if isinstance(exc.detail, dict) else {}
            return json.dumps(
                {
                    "context": "",
                    "citations": [],
                    "error": {"code": "RATE_LIMITED", "message": str(detail.get("message", ""))},
                }
            )
        hits = []
        for collection_id in ids:
            hits += await _index(holder).search(
                workspace_id=workspace_id,
                query=str(query)[:KNOWLEDGE_QUERY_MAX_CHARS],
                collection_id=collection_id,
                top_k=KNOWLEDGE_SEARCH_TOP_K,
            )
        citations = [_safe_citation(_hit_citation(h)) for h in hits]
        refs = [h.citation.citation_id for h in hits]
        if len(ids) > 1:
            ranked = sorted(zip(hits, citations, strict=True), key=lambda p: -p[0].score)
            citations = [c for _, c in ranked[:KNOWLEDGE_SEARCH_TOP_K]]
            refs = [f"[{i}]" for i in range(1, len(citations) + 1)]
            for ref, citation in zip(refs, citations, strict=True):
                citation["citation_id"] = ref
        context = "\n\n".join(
            f"{ref} {c['title']}: {c['snippet']}" for ref, c in zip(refs, citations, strict=True)
        )
        return json.dumps({"context": context, "citations": citations}, ensure_ascii=False)

    return Tool(
        "knowledge_search",
        knowledge_search,
        description="Search the workspace knowledge base; returns context with citations.",
    )


def _knowledge_reply(tool_content: str, language: str) -> str | None:
    """Answer text for a knowledge_search result, or None when the content is another tool's."""
    reply = _knowledge_reply_raw(tool_content, language)
    return None if reply is None else redact_secret_text(reply)


def _knowledge_reply_raw(tool_content: str, language: str) -> str | None:
    """Answer text for a knowledge_search result, or None when the content is another tool's."""
    try:
        data = json.loads(tool_content)
    except ValueError:
        return None
    if not isinstance(data, dict) or "citations" not in data:
        return None
    citations = data["citations"]
    if data.get("error"):
        if language == "hi":
            return "ज्ञानकोष खोज अभी सीमित है; कृपया कुछ देर बाद पुनः प्रयास करें।"
        return "Knowledge search is rate limited; please retry shortly."
    if not citations:
        if language == "hi":
            return "ज्ञानकोष में इस प्रश्न का उत्तर देने के लिए कोई प्रासंगिक दस्तावेज़ नहीं मिला।"
        return "No relevant documents found in the knowledge base."
    first = citations[0]
    snippet = _neutralise(str(first.get("snippet", ""))).strip()
    ref = first.get("citation_id", "[1]")
    if language == "hi":
        return f"दस्तावेज़ {_neutralise(str(first.get('title', '')))} के अनुसार: {snippet} {ref}"
    return f"According to {_neutralise(str(first.get('title', '')))}: {snippet} {ref}"


def _create_deterministic_handler(
    language: str, has_documents: bool = False
) -> Callable[[list[Message], list[dict[str, Any]] | None], str | list[ToolCall]]:
    """Create a deterministic fake model handler supporting tools and bilingual responses."""

    def handler(
        messages: list[Message], tools: list[dict[str, Any]] | None
    ) -> str | list[ToolCall]:
        last_msg = messages[-1] if messages else None
        if not last_msg:
            return "Hello! How can I help you today?"

        # If previous message was a tool result, synthesize answer
        if last_msg.role == "tool":
            tool_content = last_msg.content or ""
            knowledge = _knowledge_reply(tool_content, language)
            if knowledge is not None:
                return knowledge
            if "In Transit" in tool_content or "order_id" in tool_content:
                if language == "hi":
                    return (
                        "आपके ऑर्डर की स्थिति: पारगमन में (In Transit)। "
                        "अनुमानित डिलीवरी 24 घंटे के भीतर होगी।"
                    )
                return (
                    "Order status verified: The order is currently In Transit via "
                    "Delhivery Express and expected within 24 hours."
                )
            return f"The calculation result is: {tool_content}"

        content = (last_msg.content or "").strip()
        lower = content.lower()

        # Check for calculate trigger
        if (
            "calculate" in lower
            or "calc" in lower
            or ("+" in lower or "*" in lower or "/" in lower)
        ):
            # Extract expression
            expr = content
            for prefix in ["calculate", "calc", "what is", "compute", "कितना है", "हिसाब"]:
                if lower.startswith(prefix):
                    expr = content[len(prefix) :].strip(" ?:=")
                    break
            if not expr:
                expr = "2 + 2"
            return [
                ToolCall(
                    id=f"call_{uuid.uuid4().hex[:8]}",
                    name="calculate",
                    arguments={"expression": expr},
                )
            ]

        # Check for order status trigger
        if "order" in lower or "ord-" in lower or "ऑर्डर" in lower:
            order_id = "ORD-DELHI-501"
            parts = content.split()
            for p in parts:
                if p.upper().startswith("ORD-"):
                    order_id = p.upper().strip(",.!?")
                    break
            return [
                ToolCall(
                    id=f"call_{uuid.uuid4().hex[:8]}",
                    name="get_order_status",
                    arguments={"order_id": order_id},
                )
            ]

        # Document questions go to the workspace knowledge index (looks_like_document_question)
        if looks_like_document_question(content, has_documents):
            return [
                ToolCall(
                    id=f"call_{uuid.uuid4().hex[:8]}",
                    name="knowledge_search",
                    arguments={"query": content},
                )
            ]

        # Language-specific responses
        if language == "hi" or any(word in content for word in ("नमस्ते", "हैलो", "मदद")):
            return (
                "नमस्ते! मैं नेक्सुरा असिस्टेंट हूँ। "
                "मैं गणितीय गणना और ऑर्डर स्थिति की जांच में आपकी सहायता कर सकता हूँ।"
            )

        return (
            "Hello! I am the Nexura Assistant running on local deterministic fixture. "
            "I can assist with calculations, order status lookups, and general workflow queries."
        )

    return handler


@router.get("", response_model=PaginatedResponse[dict[str, Any]])
async def list_sessions(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> PaginatedResponse[dict[str, Any]]:
    """List conversation sessions scoped to caller's workspace."""
    ws_repo: WorkspaceRepository = request.app.state.workspace_repo
    session_store: SQLiteSessionStore = request.app.state.session_store
    caller_ws, principal, _ = _get_caller_context(request)

    # Find sessions owned by caller_ws
    owned_ids: list[str] = []
    with ws_repo._lock:
        for (r_type, r_id), (ws_id, _) in ws_repo._ownership.items():
            if r_type == "session" and ws_id == caller_ws:
                owned_ids.append(r_id)

    # Most recently active first, so clients that open items[0] resume the latest chat.
    updated = await session_store.updated_at(owned_ids)
    owned_ids.sort(key=lambda sid: updated.get(sid, 0.0), reverse=True)

    total = len(owned_ids)
    page_ids = owned_ids[offset : offset + limit]

    items: list[dict[str, Any]] = []
    for s_id in page_ids:
        snap = await session_store.get(s_id)
        if snap is not None:
            last_message = snap.messages[-1].content if snap.messages else None
            items.append(
                {
                    "session_id": snap.session_id,
                    "revision": snap.revision,
                    "message_count": len(snap.messages),
                    "last_message": last_message,
                }
            )

    return PaginatedResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        has_more=(offset + limit) < total,
    )


@router.post("", status_code=status.HTTP_201_CREATED, dependencies=[_require("edit")])
async def create_session(
    request: Request, payload: CreateSessionRequest | None = None
) -> dict[str, Any]:
    """Create a new persisted conversation session in caller's workspace."""
    ws_repo: WorkspaceRepository = request.app.state.workspace_repo
    session_store: SQLiteSessionStore = request.app.state.session_store
    caller_ws, principal, _ = _get_caller_context(request)

    session_id = (
        payload.session_id.strip() if payload and payload.session_id else None
    ) or f"sess-{uuid.uuid4().hex[:12]}"
    actor_id = principal.principal_id if principal else "anonymous"

    # Record ownership (409 if another workspace owns this id; never reassigned)
    owned = ws_repo.get_resource_ownership("session", session_id)
    is_local_default = bool(
        principal and principal.profile == "local-owner" and caller_ws == "default"
    )
    # Unowned id that already has a transcript (legacy data) must not be claimable.
    if owned is None and not is_local_default and await session_store.get(session_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "SESSION_ID_CONFLICT",
                "message": f"Session id '{session_id}' is already in use.",
            },
        )
    _claim_session(ws_repo, session_id, caller_ws, actor_id)

    # Initialize empty session in store if not existing
    snap = await session_store.get(session_id)
    if snap is None:
        snap = await session_store.save(session_id, [], expected_revision=0)

    return {
        "session_id": snap.session_id,
        "revision": snap.revision,
        "messages": [],
        "workspace_id": caller_ws,
    }


@router.get("/{session_id}")
async def get_session(request: Request, session_id: str) -> dict[str, Any]:
    """Retrieve full conversation snapshot and ordered message transcript."""
    _assert_session_ownership(request, session_id)
    session_store: SQLiteSessionStore = request.app.state.session_store
    snap = await session_store.get(session_id)
    if snap is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "SESSION_NOT_FOUND", "message": f"Session '{session_id}' not found."},
        )

    serialized_msgs: list[dict[str, Any]] = []
    for m in snap.messages:
        item: dict[str, Any] = {
            "role": m.role,
            "content": m.content,
            "name": m.name,
            "tool_call_id": m.tool_call_id,
        }
        if m.tool_calls:
            item["tool_calls"] = [
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in m.tool_calls
            ]
        serialized_msgs.append(item)

    return {
        "session_id": snap.session_id,
        "revision": snap.revision,
        "messages": serialized_msgs,
    }


@router.delete("/{session_id}", dependencies=[_require("edit")])
async def delete_session(request: Request, session_id: str) -> dict[str, Any]:
    """Delete a conversation session."""
    _assert_session_ownership(request, session_id)
    ws_repo: WorkspaceRepository = request.app.state.workspace_repo
    session_store: SQLiteSessionStore = request.app.state.session_store

    with session_store._lock:
        conn = session_store._ensure_open()
        with conn:
            conn.execute("DELETE FROM nexura_sessions WHERE session_id = ?", (session_id,))

    with ws_repo._lock:
        ws_repo._ownership.pop(("session", session_id), None)

    return {"deleted": True, "session_id": session_id}


def _registry(request: Request) -> ChatModelRegistry:
    registry: ChatModelRegistry = request.app.state.chat_models
    return registry


def _resolve_chat_model(request: Request, payload: ChatRequest) -> Any:
    """Validate the requested name against the allowlist and build the model lazily."""
    registry = _registry(request)
    if payload.model not in registry:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "MODEL_NOT_AVAILABLE",
                "message": (
                    f"Model '{payload.model}' is not available. "
                    f"Allowed models: {', '.join(registry.names)}."
                ),
            },
        )
    if registry.is_fake(payload.model):
        return None
    _, _, role = _get_caller_context(request)
    if role == "viewer":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "MODEL_FORBIDDEN",
                "message": "Editor or admin role is required to use a configured chat model.",
            },
        )
    try:
        return registry.resolve(payload.model)
    except ChatModelUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "MODEL_UNAVAILABLE", "message": str(exc)},
        ) from None


def _build_playground_agent(
    payload: ChatRequest,
    model: Any | None = None,
    knowledge_tool: Tool | None = None,
    has_documents: bool = False,
) -> Agent:
    """Agent bound to the offline fixture, or to an operator-configured model."""
    instructions = (
        "You are a helpful assistant capable of mathematical calculation and order status "
        "verification."
    )
    if model is None:
        model = FakeModel(
            handler=_create_deterministic_handler(payload.language, has_documents),
            default_usage=Usage(prompt_tokens=18, completion_tokens=32, total_tokens=50),
        )
    elif payload.language == "hi":
        instructions += " Reply in Hindi."
    return Agent(
        name="NexuraPlaygroundAgent",
        instructions=instructions,
        model=model,
        tools=[*PLAYGROUND_TOOLS, *([knowledge_tool] if knowledge_tool else [])],
        max_turns=payload.max_turns,
        timeout=payload.timeout,
    )


def _usage_limits(request: Request) -> UsageLimits:
    limits: UsageLimits = request.app.state.chat_usage_limits
    return limits


@chat_router.get("/models")
async def list_chat_models(request: Request) -> list[dict[str, Any]]:
    """Chat models the operator allowed. Never includes keys or provider specs."""
    return _registry(request).describe()


async def _ensure_session(
    request: Request, session_id: str, session_store: SQLiteSessionStore
) -> None:
    """Create the session on first use and record its owning workspace."""
    caller_ws, principal, _ = _get_caller_context(request)
    ws_repo: WorkspaceRepository = request.app.state.workspace_repo
    actor_id = principal.principal_id if principal else "anonymous"
    owned = ws_repo.get_resource_ownership("session", session_id)
    if owned is not None and owned[0] != caller_ws:
        _claim_session(ws_repo, session_id, caller_ws, actor_id)  # raises 409
    if await session_store.get(session_id) is None:
        _claim_session(ws_repo, session_id, caller_ws, actor_id)
        await session_store.save(session_id, [], expected_revision=0)


def _usage_dict(usage: Usage | None) -> dict[str, Any]:
    # Unknown usage stays unknown (null); counts are never invented.
    return {
        "prompt_tokens": usage.prompt_tokens if usage else None,
        "completion_tokens": usage.completion_tokens if usage else None,
        "total_tokens": usage.total_tokens if usage else None,
        "is_complete": bool(usage and usage.is_complete),
    }


def _tool_trace(events: list[RunEvent]) -> list[dict[str, Any]]:
    """Pair each tool's start and completion so the trace carries its real arguments."""
    arguments: dict[str, Any] = {}
    trace: list[dict[str, Any]] = []
    for ev in events:
        if ev.type == RunEventType.TOOL_STARTED:
            arguments[ev.data.get("tool_call_id", "")] = ev.data.get("arguments")
        elif ev.type == RunEventType.TOOL_COMPLETED:
            trace.append(
                {
                    "tool": ev.data.get("tool_name"),
                    "arguments": arguments.get(ev.data.get("tool_call_id", "")),
                    "output": ev.data.get("result"),
                    "timestamp": ev.timestamp,
                }
            )
    return trace


async def _chat_response(
    session_store: SQLiteSessionStore,
    session_id: str,
    events: list[RunEvent],
    reply: str | None,
    usage: Usage | None,
    latency_ms: float,
    *,
    model_name: str = DEFAULT_CHAT_MODEL,
    model_display: str = "fake-deterministic (Local Offline Fixture)",
    provider: str = "local",
    delivery: Literal["streamed", "final_only"] = "final_only",
) -> dict[str, Any]:
    updated_snap = await session_store.get(session_id)
    assert updated_snap is not None

    serialized_msgs: list[dict[str, Any]] = []
    for m in updated_snap.messages:
        item: dict[str, Any] = {
            "role": m.role,
            "content": m.content,
            "name": m.name,
            "tool_call_id": m.tool_call_id,
        }
        if m.tool_calls:
            item["tool_calls"] = [
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in m.tool_calls
            ]
        serialized_msgs.append(item)

    return {
        "session_id": updated_snap.session_id,
        "revision": updated_snap.revision,
        "messages": serialized_msgs,
        "reply": reply,
        "model": model_display,
        "model_name": model_name,
        "provider": provider,
        # "streamed" only when the model produced text deltas on /chat/stream.
        "delivery": delivery,
        "tool_executions": _tool_trace(events),
        "usage": _usage_dict(usage),
        "latency_ms": latency_ms,
    }


#: What the model is told when it calls a tool that needs approval in the chat playground.
PLAYGROUND_APPROVAL_REFUSAL = (
    "Not run: this tool needs human approval, which the chat playground cannot give. "
    "Run the agent in a workflow to approve it."
)
_MAX_REFUSAL_ROUNDS = 20


def _saved_agent(
    request: Request, session_id: str, payload: ChatRequest
) -> tuple[Agent, str, bool]:
    """``(agent, model name, is a configured model)`` for ``payload.agent_id``.

    The agent must belong to the caller's workspace (others are 404, like a missing one) and
    run on its own model, which must be in the allowlist; viewers may use only the fake.
    """
    from nexura_agents.contracts.context import ExecutionContext

    from nexura_runtime.agents_spec import AgentSpecError
    from nexura_runtime.agents_store import AgentStoreError
    from nexura_runtime.service.api.agents import runtime_for

    workspace_id, principal, role = _get_caller_context(request)
    runtime = runtime_for(request)
    agent_id = str(payload.agent_id)
    try:
        spec, revision = runtime.store.get_spec(workspace_id, agent_id)
    except AgentStoreError as exc:
        raise HTTPException(
            status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}
        ) from None
    if "model" in payload.model_fields_set and payload.model != spec.model:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "AGENT_MODEL_FIXED",
                "message": f"Agent '{spec.name}' runs on its own model '{spec.model}'; omit "
                "model or send that name.",
            },
        )
    registry = _registry(request)
    if spec.model not in registry:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "MODEL_NOT_AVAILABLE",
                "message": (
                    f"Model '{spec.model}' is not available. "
                    f"Allowed models: {', '.join(registry.names)}."
                ),
            },
        )
    configured = not registry.is_fake(spec.model)
    if configured and role == "viewer":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "MODEL_FORBIDDEN",
                "message": "Editor or admin role is required to use a configured chat model.",
            },
        )
    context = ExecutionContext(
        workspace_id=workspace_id,
        principal_id=principal.principal_id if principal else "anonymous",
        run_id=f"chat:{session_id}",
        node_id=f"playground:{agent_id}",
    )
    try:
        built = runtime.build_from_spec(workspace_id, agent_id, revision, spec, context)
    except AgentSpecError as exc:
        raise HTTPException(
            status_code=503 if exc.code == "MODEL_UNAVAILABLE" else 422,
            detail={"code": exc.code, "message": exc.message},
        ) from None
    return built.agent, spec.model, configured


def _refusals(interruption: Any) -> dict[str, str]:
    return {call.id: PLAYGROUND_APPROVAL_REFUSAL for call in interruption.pending}


@router.post("/{session_id}/chat", dependencies=[_require("run")])
async def chat_in_session(
    request: Request, session_id: str, payload: ChatRequest
) -> dict[str, Any]:
    """Execute conversational turn with local deterministic model, executing typed tools and
    persisting messages. With ``agent_id``, chat with a saved agent of the workspace."""
    _assert_session_ownership(request, session_id)
    registry = _registry(request)
    agent: Agent | None = None
    model_name = payload.model
    if payload.agent_id is not None:
        agent, model_name, configured = _saved_agent(request, session_id, payload)
        model: Any = agent.model if configured else None
    else:
        model = _resolve_chat_model(request, payload)
    session_store: SQLiteSessionStore = request.app.state.session_store
    await _ensure_session(request, session_id, session_store)

    runner = Runner(session_store=session_store, usage_limits=_usage_limits(request))
    t0 = time.perf_counter()
    refused: list[dict[str, Any]] = []
    try:
        if agent is None:
            agent = _build_playground_agent(
                payload,
                model,
                create_knowledge_search_tool(request),
                _workspace_has_documents(request, _get_caller_context(request)[0]),
            )
        result = await runner.run_async(
            agent=agent,
            prompt=payload.message,
            session_id=session_id,
            timeout=payload.timeout,
        )
        events = list(result.events)
        # Tools that need approval are never run from chat: the model is told so.
        for _ in range(_MAX_REFUSAL_ROUNDS):
            if result.status != "paused" or result.interruption is None:
                break
            refused += [{"id": c.id, "tool": c.name} for c in result.interruption.pending]
            result = await runner.resume_async(
                agent,
                result.interruption,
                reject=_refusals(result.interruption),
                timeout=payload.timeout,
            )
            events += result.events
    except UsageLimitExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "USAGE_LIMIT_EXCEEDED", "message": str(exc)},
        ) from None
    except MaxTurnsExceededError:
        # The agent kept asking for tools it may not run here (or looped): not a provider fault.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "AGENT_TURN_LIMIT",
                "message": "The agent reached its turn limit without a final answer. Tools "
                "that need approval are not run in chat; run the agent in a workflow instead.",
            },
        ) from None
    except Exception as exc:
        if model is None:
            raise
        code, message = _safe_failure(type(exc).__name__)
        _log_provider_failure(exc, code)
        raise HTTPException(
            status_code=(
                status.HTTP_504_GATEWAY_TIMEOUT
                if code == "TIMEOUT"
                else status.HTTP_502_BAD_GATEWAY
            ),
            detail={"code": code, "message": message},
        ) from None
    latency_ms = round((time.perf_counter() - t0) * 1000, 2)
    body = await _chat_response(
        session_store,
        session_id,
        events,
        result.final_output,
        result.usage,
        latency_ms,
        **_model_fields(registry, model_name),
    )
    if payload.agent_id is not None:
        body["agent_id"] = payload.agent_id
        body["refused_tool_calls"] = refused
    return body


def _model_fields(registry: ChatModelRegistry, name: str) -> dict[str, Any]:
    return {
        "model_name": name,
        "model_display": registry.display(name),
        "provider": registry.provider(name),
    }


_SAFE_FAILURES: dict[str, tuple[str, str]] = {
    "ProviderAuthError": ("PROVIDER_AUTH", "The model provider rejected the server credentials."),
    "ProviderRateLimitError": ("PROVIDER_RATE_LIMIT", "The model provider rate-limited the call."),
    "ModelTimeoutError": ("TIMEOUT", "The model call timed out."),
    "TimeoutError": ("TIMEOUT", "The model call timed out."),
    "UsageLimitExceededError": ("USAGE_LIMIT_EXCEEDED", "The request exceeded the usage limits."),
    "MaxTurnsExceededError": (
        "AGENT_TURN_LIMIT",
        "The agent reached its turn limit without a final answer.",
    ),
}
_GENERIC_FAILURE = ("MODEL_PROVIDER_ERROR", "The model provider returned an error.")
_log = logging.getLogger(__name__)


def _safe_failure(error_type: str) -> tuple[str, str]:
    """Fixed (code, message) for a real-model failure; never includes provider text."""
    return _SAFE_FAILURES.get(error_type, _GENERIC_FAILURE)


def _log_provider_failure(exc: BaseException, code: str) -> None:
    """Server-side detail only, with environment secret values redacted."""
    text = str(exc)
    for name, value in os.environ.items():
        if value and len(value) >= 6 and name.upper().endswith(("_KEY", "_TOKEN", "_SECRET")):
            text = text.replace(value, "[redacted]")
    _log.warning("chat model failure %s (%s): %s", code, type(exc).__name__, text)


def _public_event_data(event: RunEvent, *, scrub_errors: bool = False) -> dict[str, Any]:
    """JSON-safe event payload; full message history is never streamed."""
    data: dict[str, Any] = {}
    for key, value in event.data.items():
        # A paused run's snapshot holds the whole transcript; ``pending`` names the calls.
        if key in ("messages", "interruption", "last_agent"):
            continue
        data[key] = _usage_dict(value) if isinstance(value, Usage) else value
    if scrub_errors and event.type == RunEventType.RUN_FAILED:
        code, message = _safe_failure(str(event.data.get("error_type", "")))
        data["error"] = message
        data["code"] = code
    return data


def _sse(seq: int, event: str, payload: dict[str, Any]) -> str:
    body = json.dumps(payload, ensure_ascii=False, default=str)
    return f"id: {seq}\nevent: {event}\ndata: {body}\n\n"


async def _chat_event_stream(
    session_store: SQLiteSessionStore,
    session_id: str,
    payload: ChatRequest,
    model: Any | None = None,
    registry: ChatModelRegistry | None = None,
    usage_limits: UsageLimits | None = None,
    knowledge_tool: Tool | None = None,
    has_documents: bool = False,
    agent: Agent | None = None,
    model_name: str | None = None,
) -> AsyncGenerator[str, None]:
    """Ordered run events, then the same body the non-streaming endpoint returns.

    The turn is saved only once the run completes. If the client disconnects or
    cancels first, the generator is closed, nothing is persisted and the authoritative
    transcript is unchanged, so the turn can simply be retried. ``agent`` (a saved agent)
    replaces the playground demo; its tool calls that need approval are refused, never run.
    """
    registry = registry if registry is not None else ChatModelRegistry()
    runner = Runner(
        session_store=session_store, usage_limits=usage_limits or DEFAULT_CHAT_USAGE_LIMITS
    )
    events: list[RunEvent] = []
    saw_deltas = False
    seq = 0
    reply: str | None = None
    usage: Usage | None = None
    refused: list[dict[str, Any]] = []
    t0 = time.perf_counter()
    try:
        chat_agent = agent or _build_playground_agent(payload, model, knowledge_tool, has_documents)
        stream = runner.stream_async(
            chat_agent,
            payload.message,
            session_id=session_id,
            timeout=payload.timeout,
            stream_text=model is not None,
        )
        for _ in range(_MAX_REFUSAL_ROUNDS + 1):
            paused: Any = None
            async with aclosing(stream):
                async for event in stream:
                    events.append(event)
                    seq += 1
                    if event.type == RunEventType.TEXT_DELTA:
                        saw_deltas = True
                    if event.type == RunEventType.RUN_COMPLETED:
                        reply = event.data.get("output")
                        usage = event.data.get("usage")
                    if event.type == RunEventType.RUN_PAUSED:
                        paused = event.data.get("interruption")
                    yield _sse(
                        seq,
                        "run_event",
                        {
                            "seq": seq,
                            "type": event.type.value.upper(),
                            "timestamp": event.timestamp,
                            "data": _public_event_data(event, scrub_errors=model is not None),
                        },
                    )
            if paused is None:
                break
            # Tools that need approval are never run from chat: the model is told so.
            refused += [{"id": c.id, "tool": c.name} for c in paused.pending]
            stream = runner.resume_stream_async(
                chat_agent,
                paused,
                reject=_refusals(paused),
                timeout=payload.timeout,
                stream_text=model is not None,
            )
    except Exception as exc:
        seq += 1
        if model is None:
            failure = {"code": type(exc).__name__, "message": str(exc), "persisted": False}
        else:
            code, message = _safe_failure(type(exc).__name__)
            _log_provider_failure(exc, code)
            failure = {"code": code, "message": message, "persisted": False}
        yield _sse(seq, "chat_failed", failure)
        return

    latency_ms = round((time.perf_counter() - t0) * 1000, 2)
    body = await _chat_response(
        session_store,
        session_id,
        events,
        reply,
        usage,
        latency_ms,
        delivery="streamed" if saw_deltas else "final_only",
        **_model_fields(registry, model_name or payload.model),
    )
    if payload.agent_id is not None:
        body["agent_id"] = payload.agent_id
        body["refused_tool_calls"] = refused
    yield _sse(seq + 1, "chat_completed", body)


@router.post("/{session_id}/chat/stream", dependencies=[_require("run")])
async def stream_chat_in_session(
    request: Request, session_id: str, payload: ChatRequest
) -> StreamingResponse:
    """Stream a chat turn's lifecycle events over SSE, ending with the full chat response."""
    _assert_session_ownership(request, session_id)
    agent: Agent | None = None
    model_name: str | None = None
    if payload.agent_id is not None:
        agent, model_name, configured = _saved_agent(request, session_id, payload)
        model: Any = agent.model if configured else None
    else:
        model = _resolve_chat_model(request, payload)
    session_store: SQLiteSessionStore = request.app.state.session_store
    await _ensure_session(request, session_id, session_store)
    return StreamingResponse(
        _chat_event_stream(
            session_store,
            session_id,
            payload,
            model,
            _registry(request),
            _usage_limits(request),
            create_knowledge_search_tool(request),
            _workspace_has_documents(request, _get_caller_context(request)[0]),
            agent=agent,
            model_name=model_name,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
