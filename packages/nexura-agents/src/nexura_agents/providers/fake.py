# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Deterministic fake streaming provider for offline testing and protocol verification."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator

from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.providers.base import BaseProvider
from nexura_agents.providers.errors import StreamCancelledError
from nexura_agents.providers.types import (
    EmbeddingResult,
    GenerationRequest,
    ProviderCapabilities,
    StreamDelta,
)
from nexura_agents.types import Message, ModelResponse, Usage


class StreamingFakeProvider(BaseProvider):
    """Deterministic, keyless provider adapter supporting token streaming and tool fragmentation.

    Does not perform network calls or require credentials. Supports custom capabilities,
    queued streams of StreamDelta, interleaved tool call simulation, and simulated disconnects.
    """

    def __init__(
        self,
        model_id: str = "fake-streaming-v1",
        *,
        capabilities: ProviderCapabilities | None = None,
        queued_deltas: list[list[StreamDelta]] | None = None,
        canned_responses: list[ModelResponse | Message | str] | None = None,
        canned_embeddings: list[list[float]] | None = None,
        disconnect_after_chunks: int | None = None,
        default_usage: Usage | None = None,
    ) -> None:
        caps = capabilities or ProviderCapabilities(
            can_generate=True,
            can_stream=True,
            can_embed=True,
            can_tools=True,
            can_structured_output=True,
            reports_usage=True,
        )
        super().__init__(model_id=model_id, capabilities=caps)
        self._queued_deltas = list(queued_deltas) if queued_deltas is not None else []
        self._canned_responses = list(canned_responses) if canned_responses is not None else []
        self._canned_embeddings = canned_embeddings
        self._disconnect_after_chunks = disconnect_after_chunks
        self._default_usage = default_usage or Usage(
            prompt_tokens=10,
            completion_tokens=20,
            total_tokens=30,
            is_complete=True,
        )
        self.recorded_requests: list[GenerationRequest] = []
        self.active_streams_count: int = 0

    @classmethod
    def final_only(cls, model_id: str = "fake-final-only") -> StreamingFakeProvider:
        """Create a provider instance that explicitly lacks streaming capability."""
        return cls(
            model_id=model_id,
            capabilities=ProviderCapabilities(
                can_generate=True,
                can_stream=False,
                can_embed=False,
                can_tools=True,
                reports_usage=True,
            ),
        )

    async def _generate_internal(
        self,
        request: GenerationRequest,
        context: ExecutionContext | None,
    ) -> ModelResponse:
        """Return next canned response or echo input."""
        self.recorded_requests.append(request)

        if self._canned_responses:
            item = self._canned_responses.pop(0)
            if isinstance(item, ModelResponse):
                return item
            if isinstance(item, Message):
                return ModelResponse(message=item, usage=self._default_usage)
            if isinstance(item, str):
                return ModelResponse(
                    message=Message(role="assistant", content=item),
                    usage=self._default_usage,
                )

        # Default echo response
        last_msg = request.messages[-1] if request.messages else None
        if last_msg is None:
            text = "Hello from StreamingFakeProvider"
        elif isinstance(last_msg, Message):
            text = f"Echo: {last_msg.content or ''}"
        else:
            text = f"Echo: {last_msg.text_content}"

        return ModelResponse(
            message=Message(role="assistant", content=text),
            usage=self._default_usage,
            finish_reason="stop",
        )

    async def _stream_internal(
        self,
        request: GenerationRequest,
        context: ExecutionContext | None,
    ) -> AsyncIterator[StreamDelta]:
        """Stream queued deltas or chunked echo response."""
        self.recorded_requests.append(request)
        self.active_streams_count += 1
        chunks_emitted = 0

        try:
            if self._queued_deltas:
                delta_stream = self._queued_deltas.pop(0)
                for delta in delta_stream:
                    if (
                        self._disconnect_after_chunks is not None
                        and chunks_emitted >= self._disconnect_after_chunks
                    ):
                        raise StreamCancelledError(
                            "Simulated network transport disconnect during streaming.",
                            stream_id="stream_disc_001",
                        )
                    if context is not None and context.is_cancelled:
                        raise StreamCancelledError(
                            "Stream cancelled via ExecutionContext.",
                            stream_id=context.run_id,
                        )
                    chunks_emitted += 1
                    yield delta
                    await asyncio.sleep(0.001)
                return

            # Default text chunking
            sample_text = "Streaming test completion from fake provider."
            for word in sample_text.split(" "):
                if (
                    self._disconnect_after_chunks is not None
                    and chunks_emitted >= self._disconnect_after_chunks
                ):
                    raise StreamCancelledError(
                        "Simulated network transport disconnect during streaming.",
                        stream_id="stream_disc_001",
                    )
                if context is not None and context.is_cancelled:
                    raise StreamCancelledError(
                        "Stream cancelled via ExecutionContext.",
                        stream_id=context.run_id,
                    )
                chunks_emitted += 1
                yield StreamDelta(kind="text_delta", text=word + " ")
                await asyncio.sleep(0.001)

            yield StreamDelta(
                kind="finish",
                finish_reason="stop",
                usage=self._default_usage,
            )
        finally:
            self.active_streams_count -= 1

    async def _embed_internal(
        self,
        texts: list[str],
        context: ExecutionContext | None,
    ) -> EmbeddingResult:
        """Return canned or hash-derived vector embeddings."""
        if self._canned_embeddings is not None:
            return EmbeddingResult(
                embeddings=self._canned_embeddings,
                model=self.model_id,
                usage=self._default_usage,
            )

        # Generate deterministic embeddings using sha256 hash digests
        embeddings: list[list[float]] = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            # Normalize first 8 bytes into floats between -1.0 and 1.0
            vec = [(b / 128.0) - 1.0 for b in digest[:8]]
            embeddings.append(vec)

        total_words = sum(len(t.split()) for t in texts)
        usage = Usage(prompt_tokens=total_words, total_tokens=total_words, is_complete=True)
        return EmbeddingResult(
            embeddings=embeddings,
            model=self.model_id,
            usage=usage,
        )
