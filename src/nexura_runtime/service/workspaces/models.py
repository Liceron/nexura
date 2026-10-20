# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Data models for workspaces and memberships.

The organisation models moved to the closed edition in EDITIONS_PLAN step 5.
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, Field

from nexura_runtime.service.auth.models import WorkspaceRole


class Workspace(BaseModel):
    """Logical tenant workspace isolating resources, credentials, and indexes."""

    workspace_id: str
    name: str
    owner_id: str
    created_at: float = Field(default_factory=time.time)
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkspaceMember(BaseModel):
    """Membership binding between a principal and a workspace with an assigned role."""

    workspace_id: str
    principal_id: str
    role: WorkspaceRole = "viewer"
    joined_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
