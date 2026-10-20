# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Data models for identity and roles: ``Principal``, ``WorkspaceRole``, ``ActionPermission``.

``TokenPayload``, ``ApiKey`` and ``MembershipAuditEvent`` moved to the closed edition in
EDITIONS_PLAN step 7. ``Principal.scopes`` and
``Principal.resources`` stay: open-source routes check them when a closed-edition provider
authenticates a restricted API key; the local owner never has them.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

ProfileType = Literal["local-owner", "authenticated-shared-service"]
WorkspaceRole = Literal["admin", "editor", "viewer"]
ActionPermission = Literal["run", "edit", "approve", "admin", "export_credentials"]


class Principal(BaseModel):
    """Server-derived authenticated identity."""

    principal_id: str
    email: str = ""
    name: str = ""
    profile: ProfileType = "local-owner"
    is_service_account: bool = False
    # Optional permission restriction (API key scopes). ``None`` means "no restriction beyond
    # the role"; a non-empty list further limits the role's permissions (intersection).
    scopes: list[ActionPermission] | None = None
    # Optional resource restriction (resource-scoped API keys), e.g. ``["workflow:wf-1",
    # "knowledge:default"]``. ``None`` means unrestricted. When set, only the routes that
    # check resources accept the principal (see ``nexura_runtime.service.auth.resources``).
    resources: list[str] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
