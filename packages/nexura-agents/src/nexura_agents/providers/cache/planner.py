# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Prompt and tool layout separation and cache planning engine."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Sequence
from typing import Any

from nexura_agents.providers.cache.capabilities import get_provider_cache_capability
from nexura_agents.providers.cache.errors import (
    CacheExpiredError,
    CacheInvalidationError,
    CachePermissionError,
    UnsupportedCachePolicyError,
)
from nexura_agents.providers.cache.models import (
    CacheLayout,
    CachePlan,
    CachePolicy,
)
from nexura_agents.providers.types import ProviderMessage
from nexura_agents.types import Message

# Regex to detect potential secrets accidentally placed in prompt text
_SECRET_PATTERN = re.compile(
    r"(sk-[a-zA-Z0-9_\-]{20,}|bearer\s+[a-zA-Z0-9_\-\.]{20,}|(?:api[_-]?key|secret|password)\s*[:=]\s*['\"][^'\"]+['\"])",
    re.IGNORECASE,
)


def _sanitize_prefix(text: str) -> str:
    """Mask credentials or raw secrets to prevent secret leakage in cache IDs."""
    return _SECRET_PATTERN.sub("[REDACTED_SECRET]", text)


def separate_layout(
    messages: Sequence[Message | ProviderMessage],
    tools: list[dict[str, Any]] | None = None,
    *,
    workspace_id: str = "default",
    model_id: str = "default-model",
) -> CacheLayout:
    """Separate stable system instructions and tool schemas from dynamic turns.

    Guarantees:
    1. Preserves message order, roles, and instruction priority.
    2. Never rewrites, mutates, or truncates conversation history turns.
    3. Leading system instructions are extracted as stable prefix.
    4. Canonicalizes tool schemas deterministically.
    5. Computes a secure, secret-free prefix hash.
    """
    system_parts: list[str] = []
    # Identify leading system messages that form the stable instruction prefix
    for msg in messages:
        if msg.role == "system":
            content = msg.text_content if isinstance(msg, ProviderMessage) else msg.content
            if content and content.strip():
                system_parts.append(content.strip())
        else:
            # Stop accumulating leading system instructions at the first non-system turn
            # to preserve instruction priority and sequence
            break

    stable_system = "\n\n".join(system_parts) if system_parts else None

    # Canonicalize tool definitions (sort keys for deterministic hashing)
    canonical_tools: list[dict[str, Any]] | None = None
    if tools:
        canonical_tools = sorted(
            [json.loads(json.dumps(t, sort_keys=True)) for t in tools],
            key=lambda t: t.get("name") or (t.get("function", {}).get("name") or ""),
        )

    # Compute deterministic prefix hash
    hasher = hashlib.sha256()
    hasher.update(workspace_id.encode("utf-8"))
    hasher.update(b"::")
    hasher.update(model_id.encode("utf-8"))
    hasher.update(b"::")

    safe_sys = _sanitize_prefix(stable_system or "")
    hasher.update(safe_sys.encode("utf-8"))
    hasher.update(b"::")

    if canonical_tools:
        safe_tools_json = _sanitize_prefix(json.dumps(canonical_tools, sort_keys=True))
        hasher.update(safe_tools_json.encode("utf-8"))

    prefix_hash = hasher.hexdigest()

    raw_chars = len(stable_system or "") + (
        len(json.dumps(canonical_tools or [])) if canonical_tools else 0
    )
    est_tokens = max(0, raw_chars // 4)

    return CacheLayout(
        stable_system_prompt=stable_system,
        stable_tools=canonical_tools,
        prefix_hash=prefix_hash,
        estimated_prefix_tokens=est_tokens,
        raw_prefix_chars=raw_chars,
    )


def create_cache_plan(
    model_id: str,
    workspace_id: str,
    messages: Sequence[Message | ProviderMessage],
    tools: list[dict[str, Any]] | None = None,
    *,
    policy: CachePolicy | None = None,
) -> CachePlan:
    """Generate a cache plan matching model capabilities and configured policy.

    Fails explicitly if model or requested options are unsupported.
    """
    effective_policy = policy or CachePolicy()
    if not effective_policy.enabled:
        raise UnsupportedCachePolicyError(
            "Cache policy is explicitly disabled.",
            model_id=model_id,
        )

    capability = get_provider_cache_capability(model_id)
    if capability.capability_type == "unsupported":
        raise UnsupportedCachePolicyError(
            f"Model '{model_id}' does not support prompt caching ({capability.notes}).",
            model_id=model_id,
        )

    # Validate requested mode against provider capability
    if (
        effective_policy.preferred_mode != "auto"
        and effective_policy.preferred_mode != capability.capability_type
    ):
        raise UnsupportedCachePolicyError(
            f"Requested cache mode '{effective_policy.preferred_mode}' is unsupported by "
            f"'{model_id}' (supports '{capability.capability_type}').",
            model_id=model_id,
            option=effective_policy.preferred_mode,
        )

    # Validate custom TTL request
    if effective_policy.ttl_seconds is not None and not capability.supports_custom_ttl:
        raise UnsupportedCachePolicyError(
            f"Custom TTL ({effective_policy.ttl_seconds}s) is not supported by "
            f"'{model_id}' (only supports fixed/automatic retention).",
            model_id=model_id,
            option="ttl_seconds",
        )

    layout = separate_layout(
        messages=messages,
        tools=tools,
        workspace_id=workspace_id,
        model_id=model_id,
    )

    ttl = effective_policy.ttl_seconds or capability.default_ttl_seconds
    now = time.time()
    expires_at = (now + ttl) if ttl is not None else None

    # Deterministic safe plan ID
    plan_id = f"plan_{layout.prefix_hash[:16]}"

    # Stored context resource ID for Gemini
    cache_id: str | None = None
    if capability.capability_type == "explicit_stored_context":
        cache_id = f"cachedContents/{layout.prefix_hash[:24]}"

    return CachePlan(
        plan_id=plan_id,
        workspace_id=workspace_id,
        model_id=model_id,
        capability_type=capability.capability_type,
        cache_id=cache_id,
        prefix_hash=layout.prefix_hash,
        stable_system_prompt=layout.stable_system_prompt,
        stable_tools=layout.stable_tools,
        ttl_seconds=ttl,
        created_at=now,
        expires_at=expires_at,
        version="1.0",
        status="planned",
        estimated_prefix_tokens=layout.estimated_prefix_tokens,
    )


def validate_cache_plan(
    plan: CachePlan,
    model_id: str,
    workspace_id: str,
    messages: Sequence[Message | ProviderMessage],
    tools: list[dict[str, Any]] | None = None,
) -> bool:
    """Validate that an existing cache plan remains valid for the pending invocation.

    Enforces workspace permissions, model match, TTL validity, and prefix stability.
    Raises typed exceptions on any violation.
    """
    # 1. Workspace scope authorization check
    if plan.workspace_id != workspace_id:
        raise CachePermissionError(
            f"Unauthorized cache access: plan belongs to workspace '{plan.workspace_id}', "
            f"cannot be accessed by workspace '{workspace_id}'.",
            workspace_id=workspace_id,
            plan_workspace_id=plan.workspace_id,
        )

    # 2. Model match check
    if plan.model_id != model_id:
        plan.status = "invalidated"
        plan.invalidation_reason = f"Model changed from '{plan.model_id}' to '{model_id}'"
        raise CacheInvalidationError(
            f"Cache plan '{plan.plan_id}' invalidated: model changed from "
            f"'{plan.model_id}' to '{model_id}'.",
            plan_id=plan.plan_id,
            reason="model_mismatch",
        )

    # 3. TTL expiration check
    if plan.is_expired:
        plan.status = "expired"
        plan.invalidation_reason = "Cache TTL expired"
        raise CacheExpiredError(
            f"Cache plan '{plan.plan_id}' has expired.",
            plan_id=plan.plan_id,
            cache_id=plan.cache_id,
        )

    # 4. Prefix consistency check (system prompt or tool modifications)
    current_layout = separate_layout(
        messages=messages,
        tools=tools,
        workspace_id=workspace_id,
        model_id=model_id,
    )

    if current_layout.prefix_hash != plan.prefix_hash:
        plan.status = "invalidated"
        plan.invalidation_reason = (
            "Prefix instructions or tool schemas modified; cache plan invalidated."
        )
        raise CacheInvalidationError(
            f"Cache plan '{plan.plan_id}' invalidated: system prompt or tool schemas have changed.",
            plan_id=plan.plan_id,
            reason="prefix_mismatch",
        )

    return True
