# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Workspace-scoped skills API: versioned, labelled, text-only agent instructions.

Reads need workspace membership; create and label need the ``edit`` permission; remove needs
``admin``. Skills are refused whenever ``nexura_agents.Skill`` refuses them (scripts, non-text
resources, oversize). Resource-scoped API keys cannot reach these routes (deny-by-default in
``service/auth/resources.py``).
"""

from __future__ import annotations

import copy
from typing import Any, NoReturn

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request, status
from nexura_agents.skills import SkillError
from pydantic import BaseModel, Field

from nexura_runtime.service.api.credentials import _enforce_perm, _get_caller_context
from nexura_runtime.skills_store import SkillNotFoundError, SkillStore

router = APIRouter(prefix="/skills", tags=["skills"])

_MAX_MARKDOWN_CHARS = 70_000
_MAX_RESOURCES = 50


class CreateSkillRequest(BaseModel):
    """SKILL.md text plus optional text resources (name -> text)."""

    markdown: str = Field(max_length=_MAX_MARKDOWN_CHARS)
    resources: dict[str, str] = Field(default_factory=dict, max_length=_MAX_RESOURCES)
    label: str | None = Field(default=None, max_length=64)


class SetLabelRequest(BaseModel):
    """The version a label should point at."""

    version: int = Field(ge=1)


def _store(request: Request) -> SkillStore:
    store: SkillStore = request.app.state.skills_store
    return store


def _fail(exc: SkillError) -> NoReturn:
    if isinstance(exc, SkillNotFoundError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "SKILL_NOT_FOUND", "message": str(exc)},
        ) from exc
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"code": "SKILL_INVALID", "message": str(exc)},
    ) from exc


@router.get("")
async def list_skills(
    request: Request, label: str | None = Query(default=None, max_length=64)
) -> dict[str, Any]:
    """List the workspace's skills, optionally only those with a version carrying ``label``."""
    workspace_id, _, _ = _get_caller_context(request)
    items = _store(request).list_skills(workspace_id, label)
    return {"items": items, "total": len(items)}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_skill(request: Request, payload: CreateSkillRequest) -> dict[str, Any]:
    """Store SKILL.md text as a new version (identical text returns the existing version)."""
    workspace_id, principal, role = _get_caller_context(request)
    _enforce_perm(principal, role, "edit")
    try:
        return _store(request).create(
            workspace_id,
            payload.markdown,
            resources=payload.resources,
            labels=[payload.label] if payload.label else (),
            actor=principal.principal_id,
        )
    except SkillError as exc:
        _fail(exc)


@router.get("/{name}")
async def get_skill_version(
    request: Request,
    name: str,
    version: int | None = Query(default=None, ge=1),
    label: str | None = Query(default=None, max_length=64),
) -> dict[str, Any]:
    """One version (by number, else label, else newest) with its text; text only, never HTML."""
    workspace_id, _, _ = _get_caller_context(request)
    try:
        return _store(request).get(workspace_id, name, version=version, label=label)
    except SkillError as exc:
        _fail(exc)


@router.get("/{name}/versions")
async def list_skill_versions(request: Request, name: str) -> dict[str, Any]:
    """Every version of a skill with its labels."""
    workspace_id, _, _ = _get_caller_context(request)
    try:
        return _store(request).versions(workspace_id, name)
    except SkillError as exc:
        _fail(exc)


@router.put("/{name}/labels/{label}")
async def set_skill_label(
    request: Request, name: str, label: str, payload: SetLabelRequest
) -> dict[str, Any]:
    """Point ``label`` (for example ``production``) at a version."""
    workspace_id, principal, role = _get_caller_context(request)
    _enforce_perm(principal, role, "edit")
    try:
        return _store(request).set_label(
            workspace_id, name, label, payload.version, actor=principal.principal_id
        )
    except SkillError as exc:
        _fail(exc)


@router.delete("/{name}")
async def remove_skill(
    request: Request, name: str, version: int | None = Query(default=None, ge=1)
) -> dict[str, Any]:
    """Delete a skill, or only one version of it (admin)."""
    workspace_id, principal, role = _get_caller_context(request)
    _enforce_perm(principal, role, "admin")
    try:
        removed = _store(request).remove(workspace_id, name, version, actor=principal.principal_id)
    except SkillError as exc:
        _fail(exc)
    return {"removed": removed, "name": name, "version": version}


def mount_skills(app: FastAPI, api_v1: APIRouter) -> None:
    """Create the skills store on the service database, wire it to agent nodes and mount routes."""
    store = SkillStore(app.state.store.db_path)
    app.state.skills_store = store
    # Agent-node modules already registered read their skills from this store at run time.
    registry = app.state.registry
    handlers = getattr(registry, "_handlers", {})
    for handler in list(handlers.values()):
        if hasattr(handler, "bind_skill_source") and getattr(handler, "skill_source", None) is None:
            handler.bind_skill_source(store)
    # Shared versioned agent modules (the built-in coordinator) are process-wide: bind an
    # app-local copy instead, so each app reads its own store.
    versioned = getattr(registry, "versioned_registry", None)
    if versioned is not None and hasattr(registry, "override_module"):
        for entry in versioned.list_modules():
            if not entry["is_available"]:
                continue
            module = versioned.get(entry["module_id"], entry["version"])
            if hasattr(module, "bind_skill_source") and module.skill_source is None:
                local = copy.copy(module)
                local.bind_skill_source(store)
                registry.override_module(local)
    api_v1.include_router(router)


__all__ = ["CreateSkillRequest", "SetLabelRequest", "mount_skills", "router"]
