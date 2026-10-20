# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Map Langflow model/provider fields to SDK models. API keys are never read from a flow."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .errors import ModelMappingError

_PROVIDER_PREFIX = {
    "openai": "openai",
    "anthropic": "anthropic",
    "google": "gemini",
    "google generative ai": "gemini",
    "googlegenerativeai": "gemini",
    "gemini": "gemini",
    "ollama": "ollama",
    "sarvam": "sarvam",
    "vertex": "vertex",
}


@dataclass(frozen=True)
class ModelRef:
    """What the flow asks for. ``api_key`` is only ever caller-supplied (never from the flow)."""

    node_id: str
    display_name: str
    provider: str
    name: str
    #: Name of the global variable the flow says holds the key (``load_from_db``), if any.
    api_key_variable: str | None = None
    api_key: str | None = field(default=None, repr=False)
    options: dict[str, Any] = field(default_factory=dict)

    @property
    def spec(self) -> str:
        return f"{self.provider.lower()}:{self.name}"


#: ``None`` (default provider table), a model object or spec string used for every model
#: step, a mapping keyed by node id / display name / ``"provider:name"`` / ``"*"``, or a
#: factory called with a ``ModelRef``.
ModelsArg = Any


def extract_model_ref(
    node_id: str,
    display_name: str,
    fields: Mapping[str, Any],
    api_key_variable: str | None,
    variables: Mapping[str, str],
) -> ModelRef:
    provider = ""
    name = ""
    options: dict[str, Any] = {}
    value = fields.get("model")
    if isinstance(value, list) and value:
        value = value[0]
    if isinstance(value, dict):
        provider = str(value.get("provider") or "")
        name = str(value.get("name") or value.get("model_name") or "")
        options = {k: v for k, v in value.items() if k in {"temperature", "max_tokens"}}
    elif isinstance(value, str) and value:
        name = value
    if not provider:
        provider = str(fields.get("agent_llm") or fields.get("provider") or "")
    if not name:
        name = str(fields.get("model_name") or "")
    key = variables.get(api_key_variable) if api_key_variable else None
    return ModelRef(node_id, display_name, provider, name, api_key_variable, key, options)


def _is_model_object(obj: Any) -> bool:
    return hasattr(obj, "generate")


def default_model(ref: ModelRef) -> Any:
    """Provider table: a ``provider:model`` spec for the SDK (or a provider with the key)."""
    prefix = _PROVIDER_PREFIX.get(ref.provider.strip().lower())
    if prefix is None or not ref.name:
        raise ModelMappingError(
            f"No SDK mapping for provider {ref.provider!r} model {ref.name!r} on node "
            f"{ref.node_id}; pass models={{...}} with a model or factory for it."
        )
    if ref.api_key and prefix == "openai":
        from nexura_agents.providers.openai import OpenAIProvider

        return OpenAIProvider(ref.name, api_key=ref.api_key)
    if ref.api_key and prefix == "anthropic":
        from nexura_agents.providers.anthropic import AnthropicNativeProvider

        return AnthropicNativeProvider(ref.name, api_key=ref.api_key)
    if ref.api_key and prefix == "gemini":
        from nexura_agents.providers.gemini import GeminiNativeProvider

        return GeminiNativeProvider(ref.name, api_key=ref.api_key)
    if ref.api_key and prefix == "sarvam":
        from nexura_agents.providers.sarvam import SarvamProvider

        # The caller's key, never the operator's SARVAM_API_KEY from the environment.
        return SarvamProvider(ref.name, api_key=ref.api_key)
    # Ollama needs no key and Vertex uses ambient credentials (ADC); keyless specs resolve
    # their key from the environment as before.
    return f"{prefix}:{ref.name}"


def resolve_model(ref: ModelRef, models: ModelsArg) -> Any:
    """Pick the SDK model for ``ref`` according to the ``models`` argument."""
    if models is None:
        return default_model(ref)
    if isinstance(models, str) or _is_model_object(models):
        return models
    if isinstance(models, Mapping):
        for key in (ref.node_id, ref.display_name, ref.spec, "*"):
            if key in models:
                chosen = models[key]
                if callable(chosen) and not _is_model_object(chosen):
                    return chosen(ref)
                return chosen
        return default_model(ref)
    if callable(models):
        factory: Callable[[ModelRef], Any] = models
        return factory(ref)
    raise ModelMappingError(f"Unsupported models argument of type {type(models).__name__}")


def needs_default_mapping(ref: ModelRef, models: ModelsArg) -> bool:
    """True when this model will be built by the provider table (so its key is required)."""
    if models is None:
        return True
    if isinstance(models, Mapping):
        return not any(k in models for k in (ref.node_id, ref.display_name, ref.spec, "*"))
    return False
