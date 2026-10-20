# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Operator-owned embedding configuration; construction makes no model calls."""

from __future__ import annotations

import os
from collections.abc import Mapping

import httpx
from nexura_agents.providers.base import ProviderProtocol
from nexura_agents.providers.gemini import GeminiNativeProvider
from nexura_agents.providers.openai import OpenAIProvider

from nexura_runtime.knowledge.chunks.models import IndexLineage
from nexura_runtime.knowledge.embeddings.embedder import EmbeddingOrchestrator

_DIMENSIONS = {
    ("openai", "text-embedding-3-small"): 1536,
    ("openai", "text-embedding-3-large"): 3072,
    ("openai", "text-embedding-ada-002"): 1536,
    ("gemini", "text-embedding-004"): 768,
    ("gemini", "gemini-embedding-001"): 3072,
    ("vertex", "text-embedding-004"): 768,
    ("vertex", "text-embedding-005"): 768,
    ("vertex", "text-multilingual-embedding-002"): 768,
    ("vertex", "gemini-embedding-001"): 3072,
}


def configured_embedder(
    spec: str | None = None,
    *,
    dimensions: int | None = None,
    environ: Mapping[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> EmbeddingOrchestrator:
    """Explicit spec overrides NEXURA_EMBEDDINGS; absent config selects the fixture.

    Unknown models need NEXURA_EMBEDDINGS_DIMENSIONS (the expected native output
    size, not a request to truncate vectors). Bad configuration never falls back.
    ``transport`` supports keyless HTTP contract tests.
    """
    env = os.environ if environ is None else environ
    value = spec if spec is not None else env.get("NEXURA_EMBEDDINGS", "")
    if not value.strip() or value.strip() == "fixture":
        return EmbeddingOrchestrator()
    provider_name, sep, model = value.strip().partition(":")
    provider_name, model = provider_name.strip(), model.strip()
    if not sep or provider_name not in {"openai", "gemini", "vertex"} or not model:
        raise ValueError("NEXURA_EMBEDDINGS must be openai:MODEL, gemini:MODEL or vertex:MODEL.")
    if dimensions is None:
        raw_dim = env.get("NEXURA_EMBEDDINGS_DIMENSIONS")
        dimensions = int(raw_dim) if raw_dim else _DIMENSIONS.get((provider_name, model))
    if dimensions is None or dimensions <= 0:
        raise ValueError("Set NEXURA_EMBEDDINGS_DIMENSIONS to the model's positive output size.")

    provider: ProviderProtocol
    if provider_name == "vertex":
        # Optional extra. Vertex uses ADC and the existing provider's project resolution.
        from nexura_vertex import VertexAIProvider

        provider = VertexAIProvider(
            model=model,
            project=env.get("GOOGLE_CLOUD_PROJECT") or env.get("CLOUDSDK_CORE_PROJECT"),
            location=env.get("GOOGLE_CLOUD_LOCATION", "global"),
            transport=transport,
        )
    else:
        key_names = (
            ("OPENAI_API_KEY",)
            if provider_name == "openai"
            else ("GEMINI_API_KEY", "GOOGLE_API_KEY")
        )
        key = next((env[n] for n in key_names if env.get(n)), None)
        if not key:
            raise ValueError(f"Configured embeddings require {' or '.join(key_names)}.")
        if provider_name == "openai":
            provider = OpenAIProvider(model=model, api_key=key, transport=transport)
        else:
            provider = GeminiNativeProvider(model=model, api_key=key, transport=transport)
    return EmbeddingOrchestrator(
        provider,
        lineage=IndexLineage(
            chunker_name="default",
            chunker_config_hash="default",
            model_name=model,
            model_revision="v1",
            dimensions=dimensions,
        ),
    )


def describe_embedder(embedder: EmbeddingOrchestrator) -> dict[str, str | int | bool]:
    """Stable, credential-free vector-space identity, also used for durable lineage."""
    from nexura_runtime.knowledge.embeddings.provider import DeterministicEmbeddingProvider

    provider = embedder.provider
    fixture = isinstance(provider, DeterministicEmbeddingProvider)
    module = type(provider).__module__
    name = f"{module}.{type(provider).__qualname__}"
    if fixture:
        name = "deterministic"
    elif module.startswith("nexura_vertex."):
        name = "vertex"
    elif module.startswith("nexura_agents.providers.openai."):
        name = "openai"
    elif module.startswith("nexura_agents.providers.gemini."):
        name = "gemini"
    return {
        "provider": name,
        "model": embedder.lineage.model_name,
        "revision": embedder.lineage.model_revision,
        "dimension": embedder.lineage.dimensions,
        "is_test_fixture": fixture,
    }
