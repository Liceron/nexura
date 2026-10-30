# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Primary-source capability registry and dated evidence for provider caching."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from nexura_agents.providers.cache.models import CacheCapabilityType


@dataclass(frozen=True)
class CacheCapabilityProfile:
    """Capability specification derived directly from dated primary provider documentation."""

    provider: str
    model_pattern: str
    capability_type: CacheCapabilityType
    min_tokens: int
    supports_custom_ttl: bool
    default_ttl_seconds: float | None
    max_breakpoints: int | None
    wire_response_read_key: str
    wire_response_write_key: str | None
    evidence_source: str
    evidence_date: str
    notes: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model_pattern": self.model_pattern,
            "capability_type": self.capability_type,
            "min_tokens": self.min_tokens,
            "supports_custom_ttl": self.supports_custom_ttl,
            "default_ttl_seconds": self.default_ttl_seconds,
            "max_breakpoints": self.max_breakpoints,
            "wire_response_read_key": self.wire_response_read_key,
            "wire_response_write_key": self.wire_response_write_key,
            "evidence_source": self.evidence_source,
            "evidence_date": self.evidence_date,
            "notes": self.notes,
        }


# Dated Primary-Source Capability Registry
# References:
# - OpenAI Platform Documentation: "Prompt Caching in the API"
#   (Updated Oct 2024 / verified 2026-10-01)
# - Anthropic Claude Documentation: "Prompt Caching (beta)" (Updated Aug 2024 / verified 2026-10-01)
# - Google Cloud Gemini API Documentation: "Context Caching Overview"
#   (Updated June 2024 / verified 2026-10-01)
# - Sarvam AI API Documentation: "v1 Chat Completions" (Verified 2026-10-01)

_REGISTRY: list[CacheCapabilityProfile] = [
    # OpenAI: Automatic implicit prefix caching for prompts >= 1024 tokens in 128-token blocks
    CacheCapabilityProfile(
        provider="openai",
        model_pattern="gpt-4o",
        capability_type="implicit_prefix",
        min_tokens=1024,
        supports_custom_ttl=False,
        default_ttl_seconds=300.0,  # ~5-10 minutes inactivity eviction
        max_breakpoints=None,
        wire_response_read_key="prompt_tokens_details.cached_tokens",
        wire_response_write_key=None,
        evidence_source="OpenAI Platform Documentation: Prompt Caching (Oct 2024)",
        evidence_date="2026-10-01",
        notes="Automatic exact prefix matching on >=1024 tokens; 50% discount on cached input.",
    ),
    CacheCapabilityProfile(
        provider="openai",
        model_pattern="gpt-4o-mini",
        capability_type="implicit_prefix",
        min_tokens=1024,
        supports_custom_ttl=False,
        default_ttl_seconds=300.0,
        max_breakpoints=None,
        wire_response_read_key="prompt_tokens_details.cached_tokens",
        wire_response_write_key=None,
        evidence_source="OpenAI Platform Documentation: Prompt Caching (Oct 2024)",
        evidence_date="2026-10-01",
        notes="Automatic exact prefix matching on >=1024 tokens; 50% discount on cached input.",
    ),
    CacheCapabilityProfile(
        provider="openai",
        model_pattern="o1",
        capability_type="implicit_prefix",
        min_tokens=1024,
        supports_custom_ttl=False,
        default_ttl_seconds=300.0,
        max_breakpoints=None,
        wire_response_read_key="prompt_tokens_details.cached_tokens",
        wire_response_write_key=None,
        evidence_source="OpenAI Platform Documentation: Prompt Caching (Oct 2024)",
        evidence_date="2026-10-01",
        notes="Automatic prefix caching on o1 reasoning family.",
    ),
    # Anthropic: Cache-routing breakpoint keys (cache_control: {"type": "ephemeral"})
    CacheCapabilityProfile(
        provider="anthropic",
        model_pattern="claude-3-5-sonnet",
        capability_type="ephemeral_routing",
        min_tokens=1024,
        supports_custom_ttl=False,
        default_ttl_seconds=300.0,  # 5-minute ephemeral TTL refreshed on hit
        max_breakpoints=4,
        wire_response_read_key="usage.cache_read_input_tokens",
        wire_response_write_key="usage.cache_creation_input_tokens",
        evidence_source="Anthropic Claude Documentation: Prompt Caching (Aug 2024)",
        evidence_date="2026-10-01",
        notes="Explicit ephemeral cache_control breakpoints; 90% discount on read, 1.25x on write.",
    ),
    CacheCapabilityProfile(
        provider="anthropic",
        model_pattern="claude-3-5-haiku",
        capability_type="ephemeral_routing",
        min_tokens=2048,  # Haiku minimum token boundary is 2048 tokens
        supports_custom_ttl=False,
        default_ttl_seconds=300.0,
        max_breakpoints=4,
        wire_response_read_key="usage.cache_read_input_tokens",
        wire_response_write_key="usage.cache_creation_input_tokens",
        evidence_source="Anthropic Claude Documentation: Prompt Caching (Aug 2024)",
        evidence_date="2026-10-01",
        notes="2048 token minimum for Haiku; up to 4 cache_control breakpoints.",
    ),
    CacheCapabilityProfile(
        provider="anthropic",
        model_pattern="claude-3-opus",
        capability_type="ephemeral_routing",
        min_tokens=1024,
        supports_custom_ttl=False,
        default_ttl_seconds=300.0,
        max_breakpoints=4,
        wire_response_read_key="usage.cache_read_input_tokens",
        wire_response_write_key="usage.cache_creation_input_tokens",
        evidence_source="Anthropic Claude Documentation: Prompt Caching (Aug 2024)",
        evidence_date="2026-10-01",
        notes="1024 token minimum for Opus.",
    ),
    # Google Gemini: Explicit stored context resource (CachedContent)
    CacheCapabilityProfile(
        provider="gemini",
        model_pattern="gemini-1.5-pro",
        capability_type="explicit_stored_context",
        min_tokens=32768,
        supports_custom_ttl=True,
        default_ttl_seconds=300.0,
        max_breakpoints=None,
        wire_response_read_key="usageMetadata.cachedContentTokenCount",
        wire_response_write_key=None,
        evidence_source="Google Cloud Gemini API: Context Caching (June 2024)",
        evidence_date="2026-10-01",
        notes="Explicit CachedContent resource creation and deletion; 75% read discount.",
    ),
    CacheCapabilityProfile(
        provider="gemini",
        model_pattern="gemini-1.5-flash",
        capability_type="explicit_stored_context",
        min_tokens=32768,
        supports_custom_ttl=True,
        default_ttl_seconds=300.0,
        max_breakpoints=None,
        wire_response_read_key="usageMetadata.cachedContentTokenCount",
        wire_response_write_key=None,
        evidence_source="Google Cloud Gemini API: Context Caching (June 2024)",
        evidence_date="2026-10-01",
        notes="Explicit CachedContent resource; 75% read discount.",
    ),
    CacheCapabilityProfile(
        provider="gemini",
        model_pattern="gemini-2.0-flash",
        capability_type="explicit_stored_context",
        min_tokens=32768,
        supports_custom_ttl=True,
        default_ttl_seconds=300.0,
        max_breakpoints=None,
        wire_response_read_key="usageMetadata.cachedContentTokenCount",
        wire_response_write_key=None,
        evidence_source="Google Cloud Gemini API: Context Caching (June 2024)",
        evidence_date="2026-10-01",
        notes="Gemini 2.0 context caching.",
    ),
    # Sarvam AI: Unsupported
    CacheCapabilityProfile(
        provider="sarvam",
        model_pattern="sarvam",
        capability_type="unsupported",
        min_tokens=0,
        supports_custom_ttl=False,
        default_ttl_seconds=None,
        max_breakpoints=None,
        wire_response_read_key="",
        wire_response_write_key=None,
        evidence_source="Sarvam AI API Documentation (v1)",
        evidence_date="2026-10-01",
        notes="Sarvam AI does not support prompt caching.",
    ),
]


def get_provider_cache_capability(model_id: str) -> CacheCapabilityProfile:
    """Retrieve capability profile for a given model or provider name.

    Returns the matching profile or an unsupported profile if not recognized.
    """
    clean_id = model_id.lower().strip()

    # Exact or substring match in priority order
    for profile in _REGISTRY:
        if profile.model_pattern in clean_id:
            return profile

    # Provider name fallback matching
    if any(prefix in clean_id for prefix in ("gpt", "openai", "o1")):
        return _REGISTRY[0]  # openai gpt-4o default
    if any(prefix in clean_id for prefix in ("claude", "anthropic")):
        return _REGISTRY[3]  # claude-3-5-sonnet default
    if any(prefix in clean_id for prefix in ("gemini", "goog")):
        return _REGISTRY[6]  # gemini-1.5-pro default
    if "sarvam" in clean_id:
        return _REGISTRY[-1]  # sarvam unsupported

    # Default unknown model: unsupported
    return CacheCapabilityProfile(
        provider="unknown",
        model_pattern=clean_id,
        capability_type="unsupported",
        min_tokens=0,
        supports_custom_ttl=False,
        default_ttl_seconds=None,
        max_breakpoints=None,
        wire_response_read_key="",
        wire_response_write_key=None,
        evidence_source="Operator unknown model fallback",
        evidence_date="2026-10-01",
        notes=f"Model '{model_id}' has no verified provider prompt caching support.",
    )
