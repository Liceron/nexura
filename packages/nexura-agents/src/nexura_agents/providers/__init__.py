# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Provider-neutral model protocol, streaming, and tool assembly package."""

from __future__ import annotations

import importlib
import importlib.util
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexura_agents.providers.anthropic import AnthropicMetadata, AnthropicNativeProvider
    from nexura_agents.providers.base import BaseProvider, ProviderProtocol
    from nexura_agents.providers.cache import (
        CacheCapabilityProfile,
        CacheCapabilityType,
        CacheCleanupError,
        CacheError,
        CacheExpiredError,
        CacheInvalidationError,
        CacheLayout,
        CacheManager,
        CachePermissionError,
        CachePlan,
        CachePlanStatus,
        CachePolicy,
        CacheUsageMetrics,
        UnsupportedCachePolicyError,
        create_cache_plan,
        get_provider_cache_capability,
        separate_layout,
        validate_cache_plan,
    )
    from nexura_agents.providers.errors import (
        CapabilityError,
        MalformedToolCallArgumentsError,
        ProviderAuthError,
        ProviderContentFilterError,
        ProviderError,
        ProviderQuotaExceededError,
        ProviderTimeoutError,
        StreamCancelledError,
    )
    from nexura_agents.providers.fake import StreamingFakeProvider
    from nexura_agents.providers.gemini import GeminiNativeProvider, GeminiThoughtMetadata
    from nexura_agents.providers.module import ModelProviderModule
    from nexura_agents.providers.openai import OpenAIProvider, OpenAIResponsesProvider
    from nexura_agents.providers.sarvam import (
        SarvamProvider,
        TranslationRequest,
        TranslationResult,
    )
    from nexura_agents.providers.types import (
        ArtifactPart,
        ContentPart,
        ContinuationPart,
        EmbeddingResult,
        GenerationRequest,
        ProviderCapabilities,
        ProviderMessage,
        StreamDelta,
        StreamDeltaKind,
        TextPart,
        ToolCallAssembler,
        ToolCallPart,
        ToolResultPart,
    )

_EXPORTS: dict[str, str] = {
    "AnthropicMetadata": "nexura_agents.providers.anthropic",
    "AnthropicNativeProvider": "nexura_agents.providers.anthropic",
    "ArtifactPart": "nexura_agents.providers.types",
    "BaseProvider": "nexura_agents.providers.base",
    "CacheCapabilityProfile": "nexura_agents.providers.cache",
    "CacheCapabilityType": "nexura_agents.providers.cache",
    "CacheCleanupError": "nexura_agents.providers.cache",
    "CacheError": "nexura_agents.providers.cache",
    "CacheExpiredError": "nexura_agents.providers.cache",
    "CacheInvalidationError": "nexura_agents.providers.cache",
    "CacheLayout": "nexura_agents.providers.cache",
    "CacheManager": "nexura_agents.providers.cache",
    "CachePermissionError": "nexura_agents.providers.cache",
    "CachePlan": "nexura_agents.providers.cache",
    "CachePlanStatus": "nexura_agents.providers.cache",
    "CachePolicy": "nexura_agents.providers.cache",
    "CacheUsageMetrics": "nexura_agents.providers.cache",
    "CapabilityError": "nexura_agents.providers.errors",
    "ContentPart": "nexura_agents.providers.types",
    "ContinuationPart": "nexura_agents.providers.types",
    "EmbeddingResult": "nexura_agents.providers.types",
    "GeminiNativeProvider": "nexura_agents.providers.gemini",
    "GeminiThoughtMetadata": "nexura_agents.providers.gemini",
    "GenerationRequest": "nexura_agents.providers.types",
    "MalformedToolCallArgumentsError": "nexura_agents.providers.errors",
    "ModelProviderModule": "nexura_agents.providers.module",
    "OpenAIProvider": "nexura_agents.providers.openai",
    "OpenAIResponsesProvider": "nexura_agents.providers.openai",
    "ProviderContentFilterError": "nexura_agents.providers.errors",
    "ProviderQuotaExceededError": "nexura_agents.providers.errors",
    "ProviderAuthError": "nexura_agents.providers.errors",
    "ProviderCapabilities": "nexura_agents.providers.types",
    "ProviderError": "nexura_agents.providers.errors",
    "ProviderMessage": "nexura_agents.providers.types",
    "ProviderProtocol": "nexura_agents.providers.base",
    "ProviderTimeoutError": "nexura_agents.providers.errors",
    "SarvamProvider": "nexura_agents.providers.sarvam",
    "StreamCancelledError": "nexura_agents.providers.errors",
    "StreamDelta": "nexura_agents.providers.types",
    "StreamDeltaKind": "nexura_agents.providers.types",
    "StreamingFakeProvider": "nexura_agents.providers.fake",
    "TextPart": "nexura_agents.providers.types",
    "ToolCallAssembler": "nexura_agents.providers.types",
    "ToolCallPart": "nexura_agents.providers.types",
    "ToolResultPart": "nexura_agents.providers.types",
    "TranslationRequest": "nexura_agents.providers.sarvam",
    "TranslationResult": "nexura_agents.providers.sarvam",
    "UnsupportedCachePolicyError": "nexura_agents.providers.cache",
    "create_cache_plan": "nexura_agents.providers.cache",
    "get_provider_cache_capability": "nexura_agents.providers.cache",
    "separate_layout": "nexura_agents.providers.cache",
    "validate_cache_plan": "nexura_agents.providers.cache",
}


__all__ = [
    "AnthropicMetadata",
    "AnthropicNativeProvider",
    "ArtifactPart",
    "BaseProvider",
    "CacheCapabilityProfile",
    "CacheCapabilityType",
    "CacheCleanupError",
    "CacheError",
    "CacheExpiredError",
    "CacheInvalidationError",
    "CacheLayout",
    "CacheManager",
    "CachePermissionError",
    "CachePlan",
    "CachePlanStatus",
    "CachePolicy",
    "CacheUsageMetrics",
    "CapabilityError",
    "ContentPart",
    "ContinuationPart",
    "EmbeddingResult",
    "GeminiNativeProvider",
    "GeminiThoughtMetadata",
    "GenerationRequest",
    "MalformedToolCallArgumentsError",
    "ModelProviderModule",
    "OpenAIProvider",
    "OpenAIResponsesProvider",
    "ProviderAuthError",
    "ProviderCapabilities",
    "ProviderContentFilterError",
    "ProviderError",
    "ProviderQuotaExceededError",
    "ProviderMessage",
    "ProviderProtocol",
    "ProviderTimeoutError",
    "SarvamProvider",
    "StreamCancelledError",
    "StreamDelta",
    "StreamDeltaKind",
    "StreamingFakeProvider",
    "TextPart",
    "ToolCallAssembler",
    "ToolCallPart",
    "ToolResultPart",
    "TranslationRequest",
    "TranslationResult",
    "UnsupportedCachePolicyError",
    "create_cache_plan",
    "get_provider_cache_capability",
    "separate_layout",
    "validate_cache_plan",
]


def __getattr__(name: str) -> Any:
    module_path = _EXPORTS.get(name)
    if module_path is None:
        if not name.startswith("_") and importlib.util.find_spec(f"{__name__}.{name}") is not None:
            return importlib.import_module(f"{__name__}.{name}")
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module_path), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
