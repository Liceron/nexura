# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Data models for workspaces and memberships.

The organisation models moved to the closed edition in EDITIONS_PLAN step 5.
"""

from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, Field

from nexura_runtime.service.auth.models import WorkspaceRole


class Workspace(BaseModel):
    """Logical tenant workspace isolating resources, credentials, and indexes."""

    workspace_id: str
    name: str
    owner_id: str
    created_at: float = Field(default_factory=time.time)
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkspaceDeletion(BaseModel):
    """The deletion record of a retired workspace (P46): its content purge, ``purging`` until a
    final pass after the workflow lease TTL found nothing left, then ``done``."""

    workspace_id: str
    requested_by: str
    requested_at: float = Field(default_factory=time.time)
    status: Literal["purging", "done"] = "purging"
    attempts: int = 0
    last_error: str | None = None
    completed_at: float | None = None


class WorkspaceMember(BaseModel):
    """Membership binding between a principal and a workspace with an assigned role."""

    workspace_id: str
    principal_id: str
    role: WorkspaceRole = "viewer"
    joined_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
