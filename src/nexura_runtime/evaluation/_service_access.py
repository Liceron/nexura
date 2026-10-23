# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Shared host registration policy for evaluation service entry points."""

from __future__ import annotations

from typing import Any


def registered_agents(
    app_state: Any, workspace_id: str, *, local_owner: bool
) -> dict[str, Any]:
    """Workspace registrations override legacy default-local-owner registrations."""
    agents: dict[str, Any] = {}
    if local_owner and workspace_id == "default":
        agents.update(getattr(app_state, "eval_agents", {}) or {})
    scoped = getattr(app_state, "eval_agents_by_workspace", {}) or {}
    agents.update(scoped.get(workspace_id, {}) or {})
    return agents
