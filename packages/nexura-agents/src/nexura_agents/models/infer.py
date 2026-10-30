# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Build a model from a ``"provider:model"`` string, e.g. ``"vertex:gemini-2.5-flash"``."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from nexura_agents.errors import SDKValidationError


def _openai(name: str) -> Any:
    from nexura_agents.providers.openai import OpenAIProvider

    return OpenAIProvider(name)


def _anthropic(name: str) -> Any:
    from nexura_agents.providers.anthropic import AnthropicNativeProvider

    return AnthropicNativeProvider(name)


def _gemini(name: str) -> Any:
    from nexura_agents.providers.gemini import GeminiNativeProvider

    return GeminiNativeProvider(name)


def _vertex(name: str) -> Any:
    # Vertex AI is the separate nexura-vertex package (it depends on this one).
    try:
        from nexura_vertex import VertexAIProvider
    except ModuleNotFoundError as exc:
        if exc.name != "nexura_vertex":
            raise
        raise SDKValidationError(
            "'vertex:' models are provided by the 'nexura-vertex' package, which is not "
            "installed. Install it with: pip install nexura-vertex"
        ) from exc
    return VertexAIProvider(name)


def _sarvam(name: str) -> Any:
    from nexura_agents.providers.sarvam import SarvamProvider

    return SarvamProvider(name)


def _ollama(name: str) -> Any:
    from nexura_agents.providers.openai import OpenAIProvider

    # Ollama serves an OpenAI-compatible API locally and needs no key.
    return OpenAIProvider(name, base_url="http://localhost:11434/v1", api_key="ollama")


def _litellm(name: str) -> Any:
    from nexura_agents.models.litellm import LiteLLMModel

    # Requires the optional extra: pip install "nexura-agents[litellm]".
    return LiteLLMModel(name)


_FACTORIES: dict[str, Callable[[str], Any]] = {
    "openai": _openai,
    "anthropic": _anthropic,
    "gemini": _gemini,
    "google": _gemini,
    "vertex": _vertex,
    "sarvam": _sarvam,
    "ollama": _ollama,
    "litellm": _litellm,
}

KNOWN_PREFIXES = tuple(sorted(_FACTORIES))


def infer_model(spec: str) -> Any:
    """Create a model from ``"provider:model-name"``.

    Keys come from the usual environment variables (``OPENAI_API_KEY``,
    ``ANTHROPIC_API_KEY``, ``GEMINI_API_KEY``, ``SARVAM_API_KEY``); Vertex AI uses
    Google Cloud credentials and needs ``pip install nexura-vertex``. ``"litellm:<litellm model>"``
    (e.g. ``"litellm:bedrock/anthropic.claude-3-5-sonnet"``)
    uses ``LiteLLMModel`` and needs ``pip install "nexura-agents[litellm]"``.
    ``"test"`` returns an offline ``FakeModel``.
    """
    if spec == "test":
        from nexura_agents.models.fake import FakeModel

        return FakeModel(responses=["Hello from the offline test model."], repeat_last=True)
    prefix, sep, name = spec.partition(":")
    factory = _FACTORIES.get(prefix)
    if not sep or not name or factory is None:
        raise SDKValidationError(
            f"Unknown model '{spec}'. Use 'provider:model', where provider is one of "
            f"{', '.join(KNOWN_PREFIXES)} (e.g. 'vertex:gemini-2.5-flash'), or 'test'."
        )
    return factory(name)
