# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Cache lifecycle manager providing scoping, invalidation, and cleanup."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from nexura_agents.providers.cache.errors import (
    CacheCleanupError,
    CacheExpiredError,
    CachePermissionError,
)
from nexura_agents.providers.cache.models import CachePlan


class CacheManager:
    """Manages cache plan registration, workspace scoping, and cleanup lifecycles."""

    def __init__(self) -> None:
        self._plans: dict[tuple[str, str], CachePlan] = {}

    def register_plan(self, plan: CachePlan) -> CachePlan:
        """Register a cache plan scoped to its workspace."""
        key = (plan.workspace_id, plan.plan_id)
        self._plans[key] = plan
        return plan

    def get_plan(self, plan_id: str, workspace_id: str) -> CachePlan | None:
        """Retrieve an active cache plan, validating workspace access and TTL."""
        # Check cross-workspace leakage
        for (w_id, p_id), _p in self._plans.items():
            if p_id == plan_id and w_id != workspace_id:
                raise CachePermissionError(
                    f"Unauthorized access to plan '{plan_id}' belonging to workspace '{w_id}'.",
                    workspace_id=workspace_id,
                    plan_workspace_id=w_id,
                )

        key = (workspace_id, plan_id)
        plan = self._plans.get(key)
        if plan is None:
            return None

        if plan.is_expired:
            plan.status = "expired"
            plan.invalidation_reason = "Cache TTL expired"
            raise CacheExpiredError(
                f"Cache plan '{plan_id}' has expired.",
                plan_id=plan_id,
                cache_id=plan.cache_id,
            )

        return plan

    def invalidate_plan(
        self,
        plan_id: str,
        workspace_id: str,
        reason: str = "manual_invalidation",
    ) -> CachePlan:
        """Mark a plan as invalidated."""
        plan = self.get_plan(plan_id, workspace_id)
        if plan is None:
            raise KeyError(f"Cache plan '{plan_id}' not found in workspace '{workspace_id}'.")
        plan.status = "invalidated"
        plan.invalidation_reason = reason
        return plan

    async def cleanup_stored_context(
        self,
        plan: CachePlan,
        delete_fn: Callable[[str], Any] | None = None,
    ) -> bool:
        """Clean up an explicit stored context resource.

        Raises CacheCleanupError on failure.
        """
        if plan.capability_type != "explicit_stored_context" or not plan.cache_id:
            plan.status = "invalidated"
            plan.invalidation_reason = "cleaned_up"
            return True

        if delete_fn is not None:
            try:
                res = delete_fn(plan.cache_id)
                if inspect.isawaitable(res):
                    await res
            except Exception as exc:
                plan.status = "invalidated"
                plan.invalidation_reason = f"Cleanup failed: {exc}"
                raise CacheCleanupError(
                    f"Failed to delete stored context resource '{plan.cache_id}': {exc}",
                    plan_id=plan.plan_id,
                    cache_id=plan.cache_id,
                    original_error=exc,
                ) from exc

        plan.status = "invalidated"
        plan.invalidation_reason = "cleaned_up"
        return True

    def list_active_plans(self, workspace_id: str) -> list[CachePlan]:
        """List active, non-expired cache plans for the given workspace."""
        active: list[CachePlan] = []
        for (w_id, _), p in list(self._plans.items()):
            if w_id == workspace_id:
                if p.is_expired:
                    p.status = "expired"
                elif p.status in ("planned", "active"):
                    active.append(p)
        return active
