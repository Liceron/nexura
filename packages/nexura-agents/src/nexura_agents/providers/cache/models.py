# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Data structures and domain models for cache planning, layouts, and metrics."""

from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, Field

CacheCapabilityType = Literal[
    "implicit_prefix",  # OpenAI, vLLM: automatic prefix matching on exact token sequence
    "ephemeral_routing",  # Anthropic: cache_control breakpoints, ephemeral 5m TTL
    "explicit_stored_context",  # Gemini: explicit cachedContents resource creation and delete
    "unsupported",  # Sarvam, legacy models: prompt caching not supported
]

CachePlanStatus = Literal["planned", "active", "invalidated", "expired"]


class CachePolicy(BaseModel):
    """Configuration governing provider prompt cache planning."""

    enabled: bool = True
    preferred_mode: Literal[
        "auto",
        "implicit_prefix",
        "ephemeral_routing",
        "explicit_stored_context",
    ] = "auto"
    ttl_seconds: float | None = None
    no_loss_only: bool = True
    min_prefix_tokens: int = 1024


class CachePlan(BaseModel):
    """Deterministic, permission-scoped cache plan for an LLM invocation."""

    plan_id: str
    workspace_id: str
    model_id: str
    capability_type: CacheCapabilityType
    cache_id: str | None = None  # e.g. "cachedContents/..." for Gemini, or plan key
    prefix_hash: str
    stable_system_prompt: str | None = None
    stable_tools: list[dict[str, Any]] | None = None
    ttl_seconds: float | None = None
    created_at: float = Field(default_factory=time.time)
    expires_at: float | None = None
    version: str = "1.0"
    status: CachePlanStatus = "planned"
    invalidation_reason: str | None = None
    estimated_prefix_tokens: int = 0

    @property
    def is_expired(self) -> bool:
        """True if the plan has a configured expiration and current time exceeds it."""
        return bool(self.expires_at is not None and time.time() > self.expires_at)


class CacheLayout(BaseModel):
    """Separated prompt and tool layout preserving role order and priority."""

    stable_system_prompt: str | None = None
    stable_tools: list[dict[str, Any]] | None = None
    prefix_hash: str
    estimated_prefix_tokens: int
    raw_prefix_chars: int


class CacheUsageMetrics(BaseModel):
    """Normalized observed provider cache metrics."""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cached_prompt_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    uncached_prompt_tokens: int | None = None
    is_hit: bool = False
    details: dict[str, Any] = Field(default_factory=dict)
