# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Abstract base provider and protocol for provider-neutral model execution."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.providers.errors import (
    CapabilityError,
    ProviderTimeoutError,
    StreamCancelledError,
)
from nexura_agents.providers.multimodal import attachments_in, reject_attachments
from nexura_agents.providers.types import (
    EmbeddingResult,
    GenerationRequest,
    ProviderCapabilities,
    StreamDelta,
)
from nexura_agents.types import Message, ModelResponse, Usage


@runtime_checkable
class ProviderProtocol(Protocol):
    """Protocol defining modern provider-neutral model generation, streaming and embedding."""

    capabilities: ProviderCapabilities
    model_id: str

    async def generate(
        self,
        request: GenerationRequest | list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        timeout: float | None = None,
        context: ExecutionContext | None = None,
    ) -> ModelResponse:
        """Generate a complete model response."""
        ...

    def stream(
        self,
        request: GenerationRequest | list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        timeout: float | None = None,
        context: ExecutionContext | None = None,
    ) -> AsyncIterator[StreamDelta]:
        """Stream real-time deltas from the model."""
        ...

    async def embed(
        self,
        texts: list[str],
        *,
        timeout: float | None = None,
        context: ExecutionContext | None = None,
    ) -> EmbeddingResult:
        """Generate vector embeddings for input texts."""
        ...


class BaseProvider(ABC):
    """Base class for all provider adapters.

    Enforces capability validation, cancellation tracking, secret masking,
    and backwards compatibility with ModelProtocol.
    """

    def __init__(self, model_id: str, capabilities: ProviderCapabilities) -> None:
        self.model_id = model_id
        self.capabilities = capabilities

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} model_id={self.model_id!r}>"

    def __str__(self) -> str:
        return f"{self.__class__.__name__}({self.model_id})"

    def _normalize_request(
        self,
        request: GenerationRequest | list[Message],
        tools: list[dict[str, Any]] | None = None,
        timeout: float | None = None,
    ) -> GenerationRequest:
        """Normalize inputs into a standard GenerationRequest."""
        if isinstance(request, GenerationRequest):
            return request
        return GenerationRequest(
            messages=list(request),
            tools=tools,
            timeout=timeout,
        )

    def _validate_capabilities(self, req: GenerationRequest) -> None:
        """Validate request against declared provider capabilities."""
        if req.tools and not self.capabilities.can_tools:
            raise CapabilityError(
                f"Provider '{self.model_id}' does not support tool calling.",
                provider_name=self.model_id,
                capability="tools",
            )
        if req.schema and not self.capabilities.can_structured_output:
            raise CapabilityError(
                f"Provider '{self.model_id}' does not support structured JSON schema output.",
                provider_name=self.model_id,
                capability="structured_output",
            )
        if not self.capabilities.can_multimodal:
            reject_attachments(self.model_id, req.messages)

    def _estimate_tokens(self, request: GenerationRequest) -> int:
        """Conservative token estimate for pre-dispatch admission buffer."""
        chars = sum(len(m.content or "") for m in request.messages if hasattr(m, "content"))
        # Attachments: providers bill images at roughly 1-2k tokens and documents by size.
        # Over-estimating only reserves more; settlement uses the reported usage.
        attached = 0
        for a in attachments_in(request.messages):
            size = a.size_bytes
            if a.kind == "image":
                attached += 1600
            elif size is None:
                attached += 4000
            else:
                attached += max(1000, size // 4)
        return max(256, chars // 4 + attached + 256)

    def _estimate_embed_tokens(self, texts: list[str]) -> int:
        """Conservative token estimate for vector embedding admission buffer."""
        chars = sum(len(t) for t in texts)
        return max(64, chars // 4)

    async def generate(
        self,
        messages: GenerationRequest | list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        timeout: float | None = None,
        context: ExecutionContext | None = None,
    ) -> ModelResponse:
        """Generate a complete model response.

        Compatible with both GenerationRequest and legacy ModelProtocol(messages, tools).
        """
        req = self._normalize_request(messages, tools=tools, timeout=timeout)
        self._validate_capabilities(req)

        effective_timeout = req.timeout or timeout
        if context is not None and context.is_cancelled:
            raise StreamCancelledError(
                f"Generation cancelled for provider '{self.model_id}'.",
                stream_id=context.run_id,
            )

        if req.context_policy is not None:
            from nexura_agents.context.selector import ContextSelector

            selector = ContextSelector(policy=req.context_policy)
            bundle = selector.select_messages(
                req.messages,
                tools=req.tools,
                current_workspace_id=context.workspace_id if context else None,
                run_id=context.run_id if context else None,
                node_id=context.node_id if context else None,
            )
            req.context_bundle = bundle
            req.context_decision = bundle.decision_record
            req.messages = bundle.messages

        budget = context.budget if context else None
        res_id: str | None = None
        if budget is not None:
            est_tokens = self._estimate_tokens(req)
            if hasattr(budget, "admit_provider_call_async"):
                res_id = await budget.admit_provider_call_async(
                    self.model_id,
                    estimated_tokens=est_tokens,
                    metadata={"provider": self.model_id, "method": "generate"},
                )
            elif hasattr(budget, "admit_provider_call"):
                res_id = budget.admit_provider_call(
                    self.model_id,
                    estimated_tokens=est_tokens,
                    metadata={"provider": self.model_id, "method": "generate"},
                )

        try:
            if effective_timeout is not None:
                resp = await asyncio.wait_for(
                    self._generate_internal(req, context),
                    timeout=effective_timeout,
                )
            else:
                resp = await self._generate_internal(req, context)

            if budget is not None and res_id is not None:
                if hasattr(budget, "settle_provider_call_async"):
                    await budget.settle_provider_call_async(
                        res_id,
                        usage=resp.usage,
                        model_id=self.model_id,
                        is_cancelled=False,
                        description=f"Completed generate on {self.model_id}",
                    )
                elif hasattr(budget, "settle_provider_call"):
                    budget.settle_provider_call(
                        res_id,
                        usage=resp.usage,
                        model_id=self.model_id,
                        is_cancelled=False,
                        description=f"Completed generate on {self.model_id}",
                    )
            return resp
        except TimeoutError as exc:
            if budget is not None and res_id is not None:
                if hasattr(budget, "settle_provider_call_async"):
                    await budget.settle_provider_call_async(
                        res_id,
                        usage=None,
                        model_id=self.model_id,
                        is_cancelled=True,
                        is_pre_dispatch=False,
                        description=f"Timeout during generate on {self.model_id}",
                    )
                elif hasattr(budget, "settle_provider_call"):
                    budget.settle_provider_call(
                        res_id,
                        usage=None,
                        model_id=self.model_id,
                        is_cancelled=True,
                        is_pre_dispatch=False,
                        description=f"Timeout during generate on {self.model_id}",
                    )
            raise ProviderTimeoutError(self.model_id, effective_timeout or 0.0) from exc
        except (StreamCancelledError, asyncio.CancelledError):
            if budget is not None and res_id is not None:
                if hasattr(budget, "settle_provider_call_async"):
                    await budget.settle_provider_call_async(
                        res_id,
                        usage=None,
                        model_id=self.model_id,
                        is_cancelled=True,
                        is_pre_dispatch=False,
                        description=f"Cancelled during generate on {self.model_id}",
                    )
                elif hasattr(budget, "settle_provider_call"):
                    budget.settle_provider_call(
                        res_id,
                        usage=None,
                        model_id=self.model_id,
                        is_cancelled=True,
                        is_pre_dispatch=False,
                        description=f"Cancelled during generate on {self.model_id}",
                    )
            raise
        except Exception:
            if budget is not None and res_id is not None:
                if hasattr(budget, "settle_provider_call_async"):
                    await budget.settle_provider_call_async(
                        res_id,
                        usage=None,
                        model_id=self.model_id,
                        is_cancelled=True,
                        is_pre_dispatch=False,
                        description=f"Error during generate on {self.model_id}",
                    )
                elif hasattr(budget, "settle_provider_call"):
                    budget.settle_provider_call(
                        res_id,
                        usage=None,
                        model_id=self.model_id,
                        is_cancelled=True,
                        is_pre_dispatch=False,
                        description=f"Error during generate on {self.model_id}",
                    )
            raise

    async def stream(
        self,
        request: GenerationRequest | list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        timeout: float | None = None,
        context: ExecutionContext | None = None,
    ) -> AsyncIterator[StreamDelta]:
        """Stream real-time deltas from the provider.

        Rejects final-only providers early. Handles cancellation cleanly.
        """
        if not self.capabilities.can_stream:
            raise CapabilityError(
                f"Provider '{self.model_id}' does not support token streaming. "
                "Final-only providers are not advertised as token-streaming.",
                provider_name=self.model_id,
                capability="stream",
            )

        req = self._normalize_request(request, tools=tools, timeout=timeout)
        self._validate_capabilities(req)

        if context is not None and context.is_cancelled:
            raise StreamCancelledError(
                f"Streaming cancelled for provider '{self.model_id}'.",
                stream_id=context.run_id,
            )

        if req.context_policy is not None:
            from nexura_agents.context.selector import ContextSelector

            selector = ContextSelector(policy=req.context_policy)
            bundle = selector.select_messages(
                req.messages,
                tools=req.tools,
                current_workspace_id=context.workspace_id if context else None,
                run_id=context.run_id if context else None,
                node_id=context.node_id if context else None,
            )
            req.context_bundle = bundle
            req.context_decision = bundle.decision_record
            req.messages = bundle.messages

        budget = context.budget if context else None
        res_id: str | None = None
        if budget is not None:
            est_tokens = self._estimate_tokens(req)
            if hasattr(budget, "admit_provider_call_async"):
                res_id = await budget.admit_provider_call_async(
                    self.model_id,
                    estimated_tokens=est_tokens,
                    metadata={"provider": self.model_id, "method": "stream"},
                )
            elif hasattr(budget, "admit_provider_call"):
                res_id = budget.admit_provider_call(
                    self.model_id,
                    estimated_tokens=est_tokens,
                    metadata={"provider": self.model_id, "method": "stream"},
                )

        latest_usage: Usage | None = None
        has_error = False
        try:
            async for delta in self._stream_internal(req, context):
                if delta.usage is not None:
                    latest_usage = delta.usage
                if context is not None and context.is_cancelled:
                    raise StreamCancelledError(
                        f"Streaming cancelled during iteration for '{self.model_id}'.",
                        stream_id=context.run_id,
                    )
                yield delta
        except (StreamCancelledError, asyncio.CancelledError):
            has_error = True
            if budget is not None and res_id is not None:
                if hasattr(budget, "settle_provider_call_async"):
                    await budget.settle_provider_call_async(
                        res_id,
                        usage=latest_usage,
                        model_id=self.model_id,
                        is_cancelled=True,
                        is_pre_dispatch=False,
                        description=f"Cancelled stream on {self.model_id}",
                    )
                elif hasattr(budget, "settle_provider_call"):
                    budget.settle_provider_call(
                        res_id,
                        usage=latest_usage,
                        model_id=self.model_id,
                        is_cancelled=True,
                        is_pre_dispatch=False,
                        description=f"Cancelled stream on {self.model_id}",
                    )
            raise
        except Exception:
            has_error = True
            if budget is not None and res_id is not None:
                if hasattr(budget, "settle_provider_call_async"):
                    await budget.settle_provider_call_async(
                        res_id,
                        usage=latest_usage,
                        model_id=self.model_id,
                        is_cancelled=True,
                        is_pre_dispatch=False,
                        description=f"Error during stream on {self.model_id}",
                    )
                elif hasattr(budget, "settle_provider_call"):
                    budget.settle_provider_call(
                        res_id,
                        usage=latest_usage,
                        model_id=self.model_id,
                        is_cancelled=True,
                        is_pre_dispatch=False,
                        description=f"Error during stream on {self.model_id}",
                    )
            raise
        finally:
            if not has_error and budget is not None and res_id is not None:
                if hasattr(budget, "settle_provider_call_async"):
                    await budget.settle_provider_call_async(
                        res_id,
                        usage=latest_usage,
                        model_id=self.model_id,
                        is_cancelled=False,
                        description=f"Completed stream on {self.model_id}",
                    )
                elif hasattr(budget, "settle_provider_call"):
                    budget.settle_provider_call(
                        res_id,
                        usage=latest_usage,
                        model_id=self.model_id,
                        is_cancelled=False,
                        description=f"Completed stream on {self.model_id}",
                    )

    async def embed(
        self,
        texts: list[str],
        *,
        timeout: float | None = None,
        context: ExecutionContext | None = None,
    ) -> EmbeddingResult:
        """Generate vector embeddings for input texts."""
        if not self.capabilities.can_embed:
            raise CapabilityError(
                f"Provider '{self.model_id}' does not support vector embeddings.",
                provider_name=self.model_id,
                capability="embed",
            )

        if context is not None and context.is_cancelled:
            raise StreamCancelledError(
                f"Embedding cancelled for provider '{self.model_id}'.",
                stream_id=context.run_id,
            )

        budget = context.budget if context else None
        res_id: str | None = None
        if budget is not None:
            est_tokens = self._estimate_embed_tokens(texts)
            if hasattr(budget, "admit_provider_call_async"):
                res_id = await budget.admit_provider_call_async(
                    self.model_id,
                    estimated_tokens=est_tokens,
                    metadata={"provider": self.model_id, "method": "embed"},
                )
            elif hasattr(budget, "admit_provider_call"):
                res_id = budget.admit_provider_call(
                    self.model_id,
                    estimated_tokens=est_tokens,
                    metadata={"provider": self.model_id, "method": "embed"},
                )

        try:
            if timeout is not None:
                result = await asyncio.wait_for(
                    self._embed_internal(texts, context),
                    timeout=timeout,
                )
            else:
                result = await self._embed_internal(texts, context)

            if budget is not None and res_id is not None:
                if hasattr(budget, "settle_provider_call_async"):
                    await budget.settle_provider_call_async(
                        res_id,
                        usage=result.usage,
                        model_id=self.model_id,
                        is_cancelled=False,
                        description=f"Completed embed on {self.model_id}",
                    )
                elif hasattr(budget, "settle_provider_call"):
                    budget.settle_provider_call(
                        res_id,
                        usage=result.usage,
                        model_id=self.model_id,
                        is_cancelled=False,
                        description=f"Completed embed on {self.model_id}",
                    )
            return result
        except TimeoutError as exc:
            if budget is not None and res_id is not None:
                if hasattr(budget, "settle_provider_call_async"):
                    await budget.settle_provider_call_async(
                        res_id,
                        usage=None,
                        model_id=self.model_id,
                        is_cancelled=True,
                        is_pre_dispatch=False,
                        description=f"Timeout during embed on {self.model_id}",
                    )
                elif hasattr(budget, "settle_provider_call"):
                    budget.settle_provider_call(
                        res_id,
                        usage=None,
                        model_id=self.model_id,
                        is_cancelled=True,
                        is_pre_dispatch=False,
                        description=f"Timeout during embed on {self.model_id}",
                    )
            raise ProviderTimeoutError(self.model_id, timeout or 0.0) from exc
        except (StreamCancelledError, asyncio.CancelledError):
            if budget is not None and res_id is not None:
                if hasattr(budget, "settle_provider_call_async"):
                    await budget.settle_provider_call_async(
                        res_id,
                        usage=None,
                        model_id=self.model_id,
                        is_cancelled=True,
                        is_pre_dispatch=False,
                        description=f"Cancelled during embed on {self.model_id}",
                    )
                elif hasattr(budget, "settle_provider_call"):
                    budget.settle_provider_call(
                        res_id,
                        usage=None,
                        model_id=self.model_id,
                        is_cancelled=True,
                        is_pre_dispatch=False,
                        description=f"Cancelled during embed on {self.model_id}",
                    )
            raise
        except Exception:
            if budget is not None and res_id is not None:
                if hasattr(budget, "settle_provider_call_async"):
                    await budget.settle_provider_call_async(
                        res_id,
                        usage=None,
                        model_id=self.model_id,
                        is_cancelled=True,
                        is_pre_dispatch=False,
                        description=f"Error during embed on {self.model_id}",
                    )
                elif hasattr(budget, "settle_provider_call"):
                    budget.settle_provider_call(
                        res_id,
                        usage=None,
                        model_id=self.model_id,
                        is_cancelled=True,
                        is_pre_dispatch=False,
                        description=f"Error during embed on {self.model_id}",
                    )
            raise

    @abstractmethod
    async def _generate_internal(
        self,
        request: GenerationRequest,
        context: ExecutionContext | None,
    ) -> ModelResponse:
        """Subclass implementation of generate."""
        ...

    @abstractmethod
    def _stream_internal(
        self,
        request: GenerationRequest,
        context: ExecutionContext | None,
    ) -> AsyncIterator[StreamDelta]:
        """Subclass implementation of stream."""
        ...

    @abstractmethod
    async def _embed_internal(
        self,
        texts: list[str],
        context: ExecutionContext | None,
    ) -> EmbeddingResult:
        """Subclass implementation of embed."""
        ...
