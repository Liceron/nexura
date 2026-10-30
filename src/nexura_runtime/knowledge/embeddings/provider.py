# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Deterministic vector embedding provider fixture labeled non-semantic."""

from __future__ import annotations

import hashlib
import math
from collections.abc import AsyncIterator

from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.providers.base import BaseProvider
from nexura_agents.providers.types import (
    EmbeddingResult,
    GenerationRequest,
    ProviderCapabilities,
    StreamDelta,
)
from nexura_agents.types import Message, ModelResponse, Usage


class DeterministicEmbeddingProvider(BaseProvider):
    """Deterministic, keyless embedding provider clearly labeled non-semantic.

    Generates reproducible unit-normalized vector embeddings using SHA-256
    hash projections. Does not perform network calls or require external models.
    """

    def __init__(
        self,
        model_id: str = "deterministic-vector-fixture-non-semantic",
        *,
        revision: str = "v1",
        dimensions: int = 8,
        capabilities: ProviderCapabilities | None = None,
        dimension_override: int | None = None,
    ) -> None:
        caps = capabilities or ProviderCapabilities(
            can_generate=False,
            can_stream=False,
            can_embed=True,
            can_tools=False,
            reports_usage=True,
        )
        super().__init__(model_id=model_id, capabilities=caps)
        self.revision = revision
        self.dimensions = dimensions
        self.dimension_override = dimension_override  # For testing dimension mismatches
        self.call_count: int = 0
        self.embedded_texts_count: int = 0

    def _generate_vector(self, text: str, dims: int) -> list[float]:
        """Produce a deterministic L2-normalized pseudo-vector of length dims."""
        raw_values: list[float] = []
        for i in range(dims):
            seed = f"{text}:{i}".encode()
            digest = hashlib.sha256(seed).digest()
            # Convert first 4 bytes into an unsigned 32-bit int, map to [-1.0, 1.0]
            val = int.from_bytes(digest[:4], byteorder="big", signed=False)
            norm_val = (val / 2147483647.5) - 1.0
            raw_values.append(norm_val)

        # L2-normalize
        sq_sum = sum(v * v for v in raw_values)
        norm = math.sqrt(sq_sum) if sq_sum > 0 else 1.0
        return [round(v / norm, 6) for v in raw_values]

    async def _embed_internal(
        self,
        texts: list[str],
        context: ExecutionContext | None,
    ) -> EmbeddingResult:
        self.call_count += 1
        self.embedded_texts_count += len(texts)

        target_dims = (
            self.dimension_override if self.dimension_override is not None else self.dimensions
        )

        embeddings = [self._generate_vector(t, target_dims) for t in texts]
        total_tokens = sum(max(1, len(t.split())) for t in texts)
        usage = Usage(prompt_tokens=total_tokens, total_tokens=total_tokens, is_complete=True)

        return EmbeddingResult(
            embeddings=embeddings,
            model=self.model_id,
            usage=usage,
        )

    async def _generate_internal(
        self,
        request: GenerationRequest,
        context: ExecutionContext | None,
    ) -> ModelResponse:
        return ModelResponse(
            message=Message(
                role="assistant",
                content="DeterministicEmbeddingProvider does not support generation.",
            )
        )

    async def _stream_internal(
        self,
        request: GenerationRequest,
        context: ExecutionContext | None,
    ) -> AsyncIterator[StreamDelta]:
        yield StreamDelta(kind="finish", finish_reason="stop")
