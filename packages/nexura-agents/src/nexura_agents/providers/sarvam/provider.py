# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Native Sarvam text generation and translation provider implementation."""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx

from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.errors import (
    ProviderAuthError,
    ProviderRequestError,
    ProviderResponseError,
)
from nexura_agents.limits import merge_extra, warn_unsupported
from nexura_agents.models._streaming import _error_from_chunk
from nexura_agents.providers.base import BaseProvider
from nexura_agents.providers.errors import (
    CapabilityError,
    StreamCancelledError,
    error_body_codes,
    http_status_error,
    read_error_body,
    safe_response_text,
)
from nexura_agents.providers.openai.provider import openai_chat_usage
from nexura_agents.providers.sarvam.types import (
    IdentifierPreserver,
    TranslationRequest,
    TranslationResult,
    normalize_language_code,
)
from nexura_agents.providers.types import (
    EmbeddingResult,
    GenerationRequest,
    ProviderCapabilities,
    StreamDelta,
)
from nexura_agents.types import Message, ModelResponse, Usage, response_identity

_SARVAM_KEY_PATTERN = re.compile(r"(sarvam-[a-zA-Z0-9_-]{16,}|bearer\s+[a-zA-Z0-9_-]{16,})", re.I)


def _redact_sarvam_secrets(text: str) -> str:
    """Redact Sarvam API keys from strings and error bodies."""
    return _SARVAM_KEY_PATTERN.sub("[REDACTED_SARVAM_KEY]", text)


class SarvamProvider(BaseProvider):
    """Sarvam AI native provider for multilingual Indic text generation and translation.

    Guarantees:
    - Chat and translation modeled as separate native capabilities.
    - Supported Indic language codes validated eagerly via CapabilityError.
    - Preserves templated identifiers, order IDs, URLs, and currencies across translation.
    - Omitted usage is not fabricated as zero cost.
    - Rate limits (429) extract Retry-After headers cleanly.
    - Secret keys redacted from representations, logs, and error messages.
    """

    def __init__(
        self,
        model: str = "sarvam-105b",
        *,
        api_key: str | None = None,
        base_url: str = "https://api.sarvam.ai",
        timeout: float = 30.0,
        max_input_length: int = 25000,
        output_limit_bytes: int = 2_000_000,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        capabilities: ProviderCapabilities | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        caps = capabilities or ProviderCapabilities(
            can_generate=True,
            can_stream=True,
            can_tools=False,
            can_structured_output=False,
            can_multimodal=False,
            can_embed=False,
            reports_usage=True,
        )
        super().__init__(model_id=model, capabilities=caps)
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_input_length = max_input_length
        self.output_limit_bytes = output_limit_bytes
        self.extra_headers = extra_headers or {}

        resolved_key = api_key or os.environ.get("SARVAM_API_KEY")
        self._api_key = resolved_key

        self._owns_client = client is None
        self._transport = transport
        if client is not None:
            self._client = client
        else:
            self._client = httpx.AsyncClient(
                transport=transport,
                timeout=httpx.Timeout(timeout),
            )

    @property
    def transport(self) -> httpx.AsyncBaseTransport | None:
        """Return underlying HTTP transport."""
        return self._transport

    def __repr__(self) -> str:
        masked_key = "***" if self._api_key else None
        return (
            f"SarvamProvider(model={self.model!r}, api_key={masked_key!r}, timeout={self.timeout})"
        )

    def _build_headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
        }
        if self._api_key:
            headers["api-subscription-key"] = self._api_key
        headers.update(self.extra_headers)
        return headers

    def _handle_http_error(self, exc: httpx.HTTPStatusError) -> None:
        """Translate HTTP status errors to typed exceptions with secret redaction."""
        status_code = exc.response.status_code
        raw_text = safe_response_text(exc.response)
        safe_text = _redact_sarvam_secrets(raw_text)

        if status_code in (401, 403):
            raise ProviderAuthError(
                f"Sarvam authentication failed (HTTP {status_code}): {safe_text}",
                original_error=exc,
            ) from exc

        if status_code == 429 or status_code >= 400:
            what = (
                "rate limit exceeded"
                if status_code == 429
                else "bad request"
                if status_code < 500
                else "upstream failure"
            )
            raise http_status_error(
                status_code,
                f"Sarvam {what} (HTTP {status_code}): {safe_text}",
                retry_after=exc.response.headers.get("retry-after"),
                original_error=exc,
                codes=error_body_codes(exc.response),
            ) from exc

    async def translate(
        self,
        request: TranslationRequest,
        *,
        context: ExecutionContext | None = None,
    ) -> TranslationResult:
        """Execute Indic text translation via Sarvam translation endpoint."""
        if context is not None and context.is_cancelled:
            raise StreamCancelledError("Translation cancelled.", stream_id=context.run_id)

        # 1. Validate languages
        src_lang = normalize_language_code(request.source_language_code)
        tgt_lang = normalize_language_code(request.target_language_code)

        # 2. Check input length
        input_len = len(request.input_text)
        if input_len > self.max_input_length:
            raise ProviderRequestError(
                f"Translation input length ({input_len}) exceeds configured maximum "
                f"limit of {self.max_input_length} characters."
            )

        # 3. Apply identifier masking if requested
        masked_input = request.input_text
        replacements: dict[str, str] = {}
        if request.preserve_identifiers:
            masked_input, replacements = IdentifierPreserver.mask(request.input_text)

        payload: dict[str, Any] = {
            "input": masked_input,
            "source_language_code": src_lang,
            "target_language_code": tgt_lang,
            "speaker_gender": request.speaker_gender or "Female",
            "mode": request.mode,
            "model": request.model,
            "enable_preprocessing": request.enable_preprocessing,
        }

        url = f"{self.base_url}/translate"
        headers = self._build_headers()

        try:
            response = await self._client.post(url, json=payload, headers=headers)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            self._handle_http_error(exc)
        except httpx.RequestError as exc:
            safe_err = _redact_sarvam_secrets(str(exc))
            raise ProviderRequestError(
                f"Network request to Sarvam translation endpoint failed: {safe_err}",
                original_error=exc,
            ) from exc

        # Check response size limit
        if len(response.content) > self.output_limit_bytes:
            raise ProviderResponseError(
                f"Sarvam translation response ({len(response.content)} bytes) exceeded "
                f"limit of {self.output_limit_bytes} bytes."
            )

        try:
            data = response.json()
        except Exception as exc:
            raise ProviderResponseError(
                f"Failed to parse Sarvam translation response: {exc}",
                original_error=exc,
            ) from exc

        raw_translated = data.get("translated_text", "")

        # 4. Unmask identifiers if enabled
        final_translated = raw_translated
        if request.preserve_identifiers and replacements:
            final_translated = IdentifierPreserver.unmask(raw_translated, replacements)

        usage = None
        if "usage" in data and isinstance(data["usage"], dict):
            u_dict = data["usage"]
            usage = Usage(
                prompt_tokens=u_dict.get("prompt_tokens"),
                completion_tokens=u_dict.get("completion_tokens"),
                total_tokens=u_dict.get("total_tokens"),
                is_complete=True,
            )

        return TranslationResult(
            translated_text=final_translated,
            source_language_code=src_lang,
            target_language_code=tgt_lang,
            model=request.model,
            characters_translated=input_len,
            usage=usage,
            provider_metadata=data,
        )

    async def _generate_internal(
        self,
        request: GenerationRequest,
        context: ExecutionContext | None,
    ) -> ModelResponse:
        """Execute chat completion request via Sarvam chat endpoint."""
        cache_policy = request.cache_policy or request.provider_metadata.get("cache_policy")
        if request.cache_plan or (cache_policy and getattr(cache_policy, "enabled", True)):
            from nexura_agents.providers.cache.errors import UnsupportedCachePolicyError

            raise UnsupportedCachePolicyError(
                f"Sarvam AI adapter ('{self.model}') does not support prompt caching.",
                model_id=self.model,
            )

        raw_msgs = request.to_legacy_messages()
        api_messages = [{"role": m.role, "content": m.content or ""} for m in raw_msgs]

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": api_messages,
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        warn_unsupported(
            "Sarvam",
            request,
            ("top_p", "seed", "presence_penalty", "frequency_penalty", "reasoning_effort"),
        )
        warn_unsupported("Sarvam", request, ("thinking_budget_tokens",))
        if request.extra:
            merge_extra(payload, request.extra)

        url = f"{self.base_url}/v1/chat/completions"
        headers = self._build_headers()

        try:
            response = await self._client.post(url, json=payload, headers=headers)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            self._handle_http_error(exc)
        except httpx.RequestError as exc:
            safe_err = _redact_sarvam_secrets(str(exc))
            raise ProviderRequestError(
                f"Network request to Sarvam chat endpoint failed: {safe_err}",
                original_error=exc,
            ) from exc

        if len(response.content) > self.output_limit_bytes:
            raise ProviderResponseError(
                f"Sarvam chat response ({len(response.content)} bytes) exceeded "
                f"limit of {self.output_limit_bytes} bytes."
            )

        try:
            data = response.json()
        except Exception as exc:
            raise ProviderResponseError(
                f"Failed to parse Sarvam chat response: {exc}",
                original_error=exc,
            ) from exc

        choices = data.get("choices", [])
        if not choices:
            raise ProviderResponseError("Sarvam response contained empty choices.")

        first_choice = choices[0]
        msg_obj = first_choice.get("message", {})
        content = msg_obj.get("content", "")
        finish_reason = first_choice.get("finish_reason", "stop")

        usage = None
        if "usage" in data and isinstance(data["usage"], dict):
            usage = openai_chat_usage(data["usage"])

        return ModelResponse(
            message=Message(role="assistant", content=content),
            usage=usage,
            finish_reason=finish_reason,
            **response_identity(data),
        )

    async def _stream_internal(
        self,
        request: GenerationRequest,
        context: ExecutionContext | None,
    ) -> AsyncIterator[StreamDelta]:
        """Execute chat completion request with SSE streaming."""
        raw_msgs = request.to_legacy_messages()
        api_messages = [{"role": m.role, "content": m.content or ""} for m in raw_msgs]

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": api_messages,
            "stream": True,
            # Sarvam sends usage (with reasoning_tokens) in a final chunk with no choices.
            "stream_options": {"include_usage": True},
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        warn_unsupported(
            "Sarvam",
            request,
            ("top_p", "seed", "presence_penalty", "frequency_penalty", "reasoning_effort"),
        )
        warn_unsupported("Sarvam", request, ("thinking_budget_tokens",))
        if request.extra:
            merge_extra(payload, request.extra)

        url = f"{self.base_url}/v1/chat/completions"
        headers = self._build_headers()

        response = None
        stream_done = False
        finish_seen = False
        try:
            req = self._client.build_request("POST", url, json=payload, headers=headers)
            response = await self._client.send(req, stream=True)
            await read_error_body(response)
            response.raise_for_status()

            async for line in response.aiter_lines():
                if context is not None and context.is_cancelled:
                    raise StreamCancelledError(
                        f"Stream cancelled for provider '{self.model}'.",
                        stream_id=context.run_id,
                    )

                clean = line.strip()
                if not clean or not clean.startswith("data:"):
                    continue
                data_str = clean[len("data:") :].strip()
                if data_str == "[DONE]":
                    stream_done = True
                    break
                try:
                    event = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("error") is not None:
                    raise _error_from_chunk(event["error"], _redact_sarvam_secrets)
                if isinstance(event.get("usage"), dict):
                    yield StreamDelta(kind="finish", usage=openai_chat_usage(event["usage"]))
                choices = event.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.get("delta") or {}
                finish = choice.get("finish_reason")
                if "content" in delta and delta["content"]:
                    yield StreamDelta(kind="text_delta", text=delta["content"])
                if finish:
                    finish_seen = True
                    yield StreamDelta(kind="finish", finish_reason=finish)
            if not (stream_done or finish_seen):
                raise ProviderResponseError("Sarvam stream ended before completion")
        except httpx.HTTPStatusError as exc:
            self._handle_http_error(exc)
        except httpx.RequestError as exc:
            safe_err = _redact_sarvam_secrets(str(exc))
            raise ProviderRequestError(
                f"Network request to Sarvam chat endpoint failed: {safe_err}",
                original_error=exc,
            ) from exc
        except (asyncio.CancelledError, GeneratorExit):
            raise
        finally:
            if response is not None:
                await response.aclose()

    async def _embed_internal(
        self,
        texts: list[str],
        context: ExecutionContext | None,
    ) -> EmbeddingResult:
        """Reject vector embedding requests eagerly as Sarvam chat has no embeddings."""
        raise CapabilityError(
            "Sarvam text adapter does not offer native vector embeddings.",
            provider_name=self.model,
            capability="embed",
        )

    async def aclose(self) -> None:
        """Close owned client and connections."""
        if self._owns_client and hasattr(self, "_client"):
            await self._client.aclose()

    def close(self) -> None:
        """Synchronously close owned client if no loop is active."""
        if self._owns_client and hasattr(self, "_client"):
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.aclose())
            except RuntimeError:
                asyncio.run(self.aclose())
