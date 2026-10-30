# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Typed exceptions for provider cache planning and lifecycle management."""

from __future__ import annotations


class CacheError(Exception):
    """Base exception for all cache planning and lifecycle errors."""


class UnsupportedCachePolicyError(CacheError):
    """Raised when an unsupported cache mode, TTL, or option is specified."""

    def __init__(
        self,
        message: str,
        *,
        model_id: str | None = None,
        option: str | None = None,
    ) -> None:
        super().__init__(message)
        self.model_id = model_id
        self.option = option


class CacheExpiredError(CacheError):
    """Raised when an explicit stored context or cache plan has expired."""

    def __init__(
        self,
        message: str,
        *,
        plan_id: str | None = None,
        cache_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.plan_id = plan_id
        self.cache_id = cache_id


class CacheCleanupError(CacheError):
    """Raised when an explicit stored context fails cleanup / deletion."""

    def __init__(
        self,
        message: str,
        *,
        plan_id: str | None = None,
        cache_id: str | None = None,
        original_error: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.plan_id = plan_id
        self.cache_id = cache_id
        self.original_error = original_error


class CachePermissionError(CacheError):
    """Raised when a cache plan is accessed outside its authorized workspace scope."""

    def __init__(
        self,
        message: str,
        *,
        workspace_id: str | None = None,
        plan_workspace_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.workspace_id = workspace_id
        self.plan_workspace_id = plan_workspace_id


class CacheInvalidationError(CacheError):
    """Raised when a plan is invalidated due to changed model, tools, or prefix layout."""

    def __init__(
        self,
        message: str,
        *,
        plan_id: str | None = None,
        reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.plan_id = plan_id
        self.reason = reason
