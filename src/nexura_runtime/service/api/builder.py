# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Chat-to-build API: ``POST /builder/draft`` turns a description into an unsaved draft.

Same auth and workspace rules as workflow creation (editors and admins). The model is the
caller's own: a workspace provider credential (``credential``) or, when the operator has
configured one, a named chat model (``model_ref``). Nothing is saved or run, and no secret
is ever returned.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from nexura_runtime.builder_assistant import (
    MAX_DESCRIPTION_CHARS,
    DraftError,
    draft_from_description,
)
from nexura_runtime.credentials.errors import CredentialAccessDeniedError, CredentialNotFoundError
from nexura_runtime.credentials.store import ScopedCredentialStore
from nexura_runtime.service.api.chat_models import ChatModelRegistry, ChatModelUnavailableError
from nexura_runtime.service.api.credentials import _assert_visible
from nexura_runtime.service.api.validation import validate_workflow_graph
from nexura_runtime.service.api.workflows import _get_caller_context, _require_edit
from nexura_runtime.templates.catalog import list_templates
from nexura_runtime.templates.handlers import register_template_handlers

router = APIRouter(prefix="/builder", tags=["builder"])

# Per workspace: this many drafts in this many seconds (each draft may cost several model calls).
DEFAULT_RATE_LIMIT = 10
RATE_WINDOW_SECONDS = 60.0

# Used only when the request names a credential but no model; override with ``model``.
DEFAULT_MODELS: dict[str, str] = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-haiku-4-5",
    "gemini": "gemini-2.5-flash",
    "google": "gemini-2.5-flash",
    "sarvam": "sarvam-m",
}


class DraftRequest(BaseModel):
    """Describe the workflow; name the model through ``credential`` or ``model_ref``."""

    description: str = Field(description=f"What to build, at most {MAX_DESCRIPTION_CHARS} chars.")
    credential: str | None = Field(default=None, max_length=200)
    model: str | None = Field(default=None, max_length=200)
    model_ref: str | None = Field(default=None, max_length=200)
    max_attempts: int = Field(default=3, ge=1, le=5)


def build_model_from_credential(provider: str, model_name: str, api_key: str) -> Any:
    """Build the provider adapter with the user's key. Tests replace this with a fake."""
    if provider == "openai":
        from nexura_agents.providers.openai.provider import OpenAIProvider

        return OpenAIProvider(model_name, api_key=api_key)
    if provider == "anthropic":
        from nexura_agents.providers.anthropic.provider import AnthropicNativeProvider

        return AnthropicNativeProvider(model_name, api_key=api_key)
    if provider in ("gemini", "google"):
        from nexura_agents.providers.gemini.provider import GeminiNativeProvider

        return GeminiNativeProvider(model_name, api_key=api_key)
    if provider == "sarvam":
        from nexura_agents.providers.sarvam.provider import SarvamProvider

        return SarvamProvider(model_name, api_key=api_key)
    raise ValueError(f"unsupported provider '{provider}'")


def _error(code: str, message: str, http_status: int, **extra: Any) -> HTTPException:
    return HTTPException(
        status_code=http_status, detail={"code": code, "message": message, **extra}
    )


def _no_model() -> HTTPException:
    return _error(
        "NO_MODEL_CREDENTIAL",
        "No model is configured for drafting. Add a provider credential (OpenAI, Anthropic, "
        "Gemini or Sarvam) in Credentials, then try again.",
        status.HTTP_400_BAD_REQUEST,
    )


def _check_rate(request: Request, workspace_id: str) -> None:
    limit = int(getattr(request.app.state, "builder_rate_limit", DEFAULT_RATE_LIMIT))
    windows: dict[str, deque[float]] = request.app.state.__dict__.setdefault(
        "builder_rate_windows", {}
    )
    now = time.monotonic()
    hits = windows.setdefault(workspace_id, deque())
    while hits and now - hits[0] > RATE_WINDOW_SECONDS:
        hits.popleft()
    if len(hits) >= limit:
        retry = max(1, int(RATE_WINDOW_SECONDS - (now - hits[0])))
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "BUILDER_RATE_LIMITED",
                "message": f"Too many drafts; try again in {retry} seconds.",
            },
            headers={"Retry-After": str(retry)},
        )
    hits.append(now)


def _resolve_model(request: Request, payload: DraftRequest, workspace_id: str) -> tuple[Any, str]:
    chat_models: ChatModelRegistry = request.app.state.chat_models
    cred_store: ScopedCredentialStore | None = getattr(request.app.state, "credential_store", None)

    if payload.model_ref:
        if payload.model_ref not in chat_models:
            raise _error(
                "MODEL_NOT_FOUND",
                f"Model '{payload.model_ref}' is not configured on this server.",
                status.HTTP_400_BAD_REQUEST,
            )
        if chat_models.is_fake(payload.model_ref):
            raise _no_model()
        try:
            return chat_models.resolve(payload.model_ref), chat_models.display(payload.model_ref)
        except ChatModelUnavailableError as exc:
            raise _error("MODEL_UNAVAILABLE", str(exc), status.HTTP_400_BAD_REQUEST) from None

    if cred_store is not None:
        name = payload.credential
        if name is None:
            usable = [
                m
                for m in cred_store.list_credentials(workspace_id)
                if m.status == "active" and m.provider in DEFAULT_MODELS
            ]
            name = usable[0].credential_id if usable else None
        if name is not None:
            repo = request.app.state.workspace_repo
            try:
                _assert_visible(repo, workspace_id, name)
                meta = cred_store.get_metadata(workspace_id, name)
                secret = cred_store.resolve(workspace_id, name)
            except (HTTPException, CredentialNotFoundError):
                raise _error(
                    "CREDENTIAL_NOT_FOUND",
                    f"Credential '{name}' was not found in this workspace.",
                    status.HTTP_400_BAD_REQUEST,
                ) from None
            except CredentialAccessDeniedError as exc:
                raise _error("CREDENTIAL_UNUSABLE", str(exc), status.HTTP_400_BAD_REQUEST) from None
            if meta.provider not in DEFAULT_MODELS:
                raise _error(
                    "CREDENTIAL_NOT_A_MODEL_PROVIDER",
                    f"Credential '{name}' is for '{meta.provider}', which cannot draft workflows.",
                    status.HTTP_400_BAD_REQUEST,
                )
            model_name = payload.model or DEFAULT_MODELS[meta.provider]
            try:
                model = build_model_from_credential(meta.provider, model_name, secret.reveal())
            except Exception as exc:
                raise _error(
                    "MODEL_UNAVAILABLE",
                    f"Could not set up the {meta.provider} model ({type(exc).__name__}).",
                    status.HTTP_400_BAD_REQUEST,
                ) from None
            return model, f"{meta.provider}:{model_name} (credential '{name}')"
    elif payload.credential:
        raise _no_model()

    # Operator-configured chat models (their own keys), never the offline fixture.
    for name in chat_models.names:
        if not chat_models.is_fake(name):
            try:
                return chat_models.resolve(name), chat_models.display(name)
            except ChatModelUnavailableError:
                continue
    raise _no_model()


@router.post("/draft")
async def draft_workflow(request: Request, payload: DraftRequest) -> dict[str, Any]:
    """Draft a workflow from a description. The draft is validated, not saved, not run."""
    _require_edit(request)
    caller_ws, _, _ = _get_caller_context(request)

    description = payload.description
    if not description.strip():
        raise _error("DESCRIPTION_EMPTY", "Describe what the workflow should do.", 422)
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise _error(
            "DESCRIPTION_TOO_LONG",
            f"The description is {len(description)} characters; the limit is "
            f"{MAX_DESCRIPTION_CHARS}.",
            422,
        )

    model, label = _resolve_model(request, payload, caller_ws)
    _check_rate(request, caller_ws)

    registry = request.app.state.registry
    register_template_handlers(registry)
    approvals: frozenset[str] = getattr(
        request.app.state, "approval_required_handlers", frozenset()
    )
    try:
        result = await draft_from_description(
            description,
            model=model,
            registry=registry,
            templates=list_templates(),
            max_attempts=payload.max_attempts,
            approval_required_handlers=approvals,
        )
    except DraftError as exc:
        raise _error(
            exc.code,
            exc.message,
            422,
            errors=exc.errors,
            warnings=exc.warnings,
            attempts=exc.attempts,
            unknown_modules=exc.unknown_modules,
        ) from None
    except Exception as exc:
        # Provider failures can quote request details; report only the type.
        raise _error(
            "MODEL_ERROR",
            f"The model call failed ({type(exc).__name__}). Check the credential and try again.",
            status.HTTP_502_BAD_GATEWAY,
        ) from None

    report = validate_workflow_graph(result.spec, registry, approvals or None)
    return {
        **result.to_dict(),
        "saved": False,
        "model": label,
        "validation": report.model_dump(),
    }
