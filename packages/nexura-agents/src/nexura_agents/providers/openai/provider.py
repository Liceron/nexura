# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""OpenAI-compatible and local model adapter with streaming and resilience."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx

from nexura_agents.contracts.context import ExecutionContext
from nexura_agents.errors import (
    ProviderAuthError,
    ProviderRequestError,
    ProviderResponseError,
)
from nexura_agents.json_schema import openai_response_format
from nexura_agents.limits import apply_openai_settings, openai_tool_choice
from nexura_agents.models._streaming import _error_from_chunk
from nexura_agents.providers.base import BaseProvider
from nexura_agents.providers.errors import (
    CapabilityError,
    ProviderContentFilterError,
    _mask_secrets,
    error_body_codes,
    http_status_error,
    read_error_body,
    response_excerpt,
)
from nexura_agents.providers.multimodal import openai_content_parts
from nexura_agents.providers.types import (
    ContinuationPart,
    EmbeddingResult,
    GenerationRequest,
    ProviderCapabilities,
    ProviderMessage,
    StreamDelta,
)
from nexura_agents.types import Message, ModelResponse, ToolCall, Usage, response_identity


def _reasoning_tokens(usage: dict[str, Any]) -> int | None:
    """Reasoning tokens, already included in OpenAI's completion_tokens."""
    details = usage.get("completion_tokens_details")
    value = details.get("reasoning_tokens") if isinstance(details, dict) else None
    return value if isinstance(value, int) else None


def openai_chat_usage(u_dict: dict[str, Any]) -> Usage:
    """Chat Completions ``usage`` as a Nexura ``Usage``.

    ``prompt_tokens`` already includes ``prompt_tokens_details.cached_tokens`` (read from the
    cache) and ``cache_write_tokens`` (written to it), so they are reported as breakdowns and
    never added on top. ``reasoning_tokens`` is likewise inside ``completion_tokens``.
    """
    details = u_dict.get("prompt_tokens_details")
    cached: int | None = None
    written: int | None = None
    if isinstance(details, dict):
        value = details.get("cached_tokens")
        cached = value if isinstance(value, int) else None
        value = details.get("cache_write_tokens")
        written = value if isinstance(value, int) else None
    return Usage(
        prompt_tokens=u_dict.get("prompt_tokens"),
        completion_tokens=u_dict.get("completion_tokens"),
        reasoning_tokens=_reasoning_tokens(u_dict),
        total_tokens=u_dict.get("total_tokens"),
        cached_prompt_tokens=cached,
        cache_read_input_tokens=cached,
        cache_creation_input_tokens=written,
    )


def _is_official_openai(base_url: str) -> bool:
    """True for api.openai.com (and its regional hosts); False for compatible servers."""
    host = httpx.URL(base_url).host
    return host == "api.openai.com" or host.endswith(".api.openai.com")


class OpenAIProvider(BaseProvider):
    """Adapter for OpenAI, Ollama, vLLM, LocalAI, and other OpenAI-compatible endpoints.

    Features:
    - Streaming delta parser for Server-Sent Events (SSE).
    - Preserves server continuation values (id, system_fingerprint).
    - Handles rate limits (429) with Retry-After header extraction.
    - Handles model refusal responses explicitly.
    - Preserves usage omission without zero-cost fabrication.
    - Rejects unsupported features early (CapabilityError) rather than silently downgrading.
    - Configurable for local compatible stubs without API keys (e.g. Ollama/vLLM).
    """

    # Native structured output (response_format json_schema), also alongside tools.
    # Set the instance attribute response_schema_with_tools = False for a compatible
    # server that rejects the combination.
    supports_response_schema = True
    streams_response_schema = True  # stream() sends the schema too (partial output events)
    response_schema_with_tools = True

    # The request field for the output-token limit. OpenAI deprecated ``max_tokens`` for
    # ``max_completion_tokens`` (which also counts reasoning tokens) and reasoning models
    # reject ``max_tokens``; compatible servers such as Ollama document only ``max_tokens``.
    # ``None`` (the default) picks ``max_completion_tokens`` for api.openai.com and
    # ``max_tokens`` for any other ``base_url``. Set the instance attribute to override.
    max_tokens_param: str | None = None

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        base_url: str = "https://api.openai.com/v1",
        organization: str | None = None,
        timeout: float = 30.0,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        capabilities: ProviderCapabilities | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        caps = capabilities or ProviderCapabilities(
            can_generate=True,
            can_stream=True,
            can_tools=True,
            can_structured_output=True,
            can_embed=True,
            can_multimodal=True,
            reports_usage=True,
        )
        super().__init__(model_id=model, capabilities=caps)
        self.model = model
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.base_url = base_url.rstrip("/")
        self.organization = organization
        self.timeout = timeout
        self._client = client
        self.transport = transport
        self.extra_headers = extra_headers or {}
        self._internal_client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        if self._internal_client is None or self._internal_client.is_closed:
            self._internal_client = httpx.AsyncClient(
                transport=self.transport,
                timeout=httpx.Timeout(self.timeout),
            )
        return self._internal_client

    async def close(self) -> None:
        """Close internally managed HTTP client."""
        if self._internal_client is not None and not self._internal_client.is_closed:
            await self._internal_client.aclose()
            self._internal_client = None

    def _build_headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        if self.organization:
            headers["OpenAI-Organization"] = self.organization
        headers.update(self.extra_headers)
        return headers

    def _format_message(self, msg: Message | ProviderMessage) -> dict[str, Any]:
        """Convert SDK Message or ProviderMessage into OpenAI API format."""
        if isinstance(msg, ProviderMessage):
            msg = msg.to_legacy()

        formatted: dict[str, Any] = {"role": msg.role}
        if msg.attachments:
            formatted["content"] = openai_content_parts(msg.content, msg.attachments)
        elif msg.content is not None:
            formatted["content"] = msg.content

        if msg.tool_calls:
            formatted["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": tc.raw_arguments
                        if tc.raw_arguments is not None
                        else json.dumps(tc.arguments, ensure_ascii=False),
                    },
                }
                for tc in msg.tool_calls
            ]

        if msg.tool_call_id is not None:
            formatted["tool_call_id"] = msg.tool_call_id

        if msg.name is not None:
            formatted["name"] = msg.name

        return formatted

    def _max_tokens_field(self) -> str:
        if self.max_tokens_param:
            return self.max_tokens_param
        return "max_completion_tokens" if _is_official_openai(self.base_url) else "max_tokens"

    def _handle_error_response(self, response: httpx.Response) -> None:
        """Map HTTP error status codes into typed exceptions with secret masking."""
        if response.status_code == 401:
            raise ProviderAuthError("Provider authentication failed (HTTP 401: Invalid API Key)")
        if response.status_code >= 400:
            raise http_status_error(
                response.status_code,
                "Rate limit exceeded or provider quota exhausted (HTTP 429)"
                if response.status_code == 429
                else f"Provider returned error HTTP {response.status_code}",
                retry_after=response.headers.get("Retry-After"),
                detail=response_excerpt(response),
                codes=error_body_codes(response),
            )

    async def _generate_internal(
        self,
        request: GenerationRequest,
        context: ExecutionContext | None,
    ) -> ModelResponse:
        """Execute non-streaming chat completion request."""
        # Unsupported feature check: do not silently downgrade
        if request.provider_metadata.get("unsupported_feature"):
            raise CapabilityError(
                f"Feature '{request.provider_metadata['unsupported_feature']}' is not supported.",
                provider_name=self.model,
                capability=request.provider_metadata["unsupported_feature"],
            )

        cache_policy = request.cache_policy or request.provider_metadata.get("cache_policy")
        if cache_policy:
            from nexura_agents.providers.cache.errors import UnsupportedCachePolicyError

            pref_mode = getattr(cache_policy, "preferred_mode", None) or (
                cache_policy.get("preferred_mode") if isinstance(cache_policy, dict) else None
            )
            if pref_mode and pref_mode not in ("auto", "implicit_prefix"):
                raise UnsupportedCachePolicyError(
                    f"OpenAI adapter only supports implicit prefix caching, not '{pref_mode}'.",
                    model_id=self.model,
                    option=pref_mode,
                )
            ttl = getattr(cache_policy, "ttl_seconds", None) or (
                cache_policy.get("ttl_seconds") if isinstance(cache_policy, dict) else None
            )
            if ttl is not None:
                raise UnsupportedCachePolicyError(
                    f"OpenAI adapter does not support custom TTL ({ttl}s); caching is automatic.",
                    model_id=self.model,
                    option="ttl_seconds",
                )

        client = self._get_client()
        request_timeout = request.timeout or self.timeout
        headers = self._build_headers()

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [self._format_message(m) for m in request.messages],
        }
        if request.tools:
            payload["tools"] = request.tools
            if request.tool_choice is not None:
                payload["tool_choice"] = openai_tool_choice(request.tool_choice)
        if request.schema and self.capabilities.can_structured_output:
            payload["response_format"] = openai_response_format(request.schema)
        if request.max_tokens:
            payload[self._max_tokens_field()] = request.max_tokens
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.stop_sequences:
            payload["stop"] = request.stop_sequences
        apply_openai_settings(payload, request)

        url = f"{self.base_url}/chat/completions"

        try:
            response = await client.post(
                url,
                json=payload,
                headers=headers,
                timeout=request_timeout,
            )
        except httpx.TimeoutException as exc:
            raise ProviderRequestError(
                f"Request to {self.base_url} timed out ({request_timeout}s)",
                original_error=exc,
            ) from exc
        except httpx.RequestError as exc:
            raise ProviderRequestError(
                f"HTTP network error contacting provider: {exc}",
                original_error=exc,
            ) from exc

        self._handle_error_response(response)

        try:
            data = response.json()
        except Exception as exc:
            raise ProviderResponseError(
                "Failed to parse provider response as JSON",
                original_error=exc,
            ) from exc

        if (
            not isinstance(data, dict)
            or not isinstance(data.get("choices"), list)
            or not data["choices"]
        ):
            raise ProviderResponseError("Provider response requires a nonempty choices list")

        choice = data["choices"][0]
        if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
            raise ProviderResponseError("Provider choice requires a message object")

        msg_dict = choice["message"]
        content = msg_dict.get("content")
        refusal = msg_dict.get("refusal")
        raw_tool_calls = msg_dict.get("tool_calls")
        finish_reason = choice.get("finish_reason")

        # Refusal handling
        if refusal:
            content = f"Model Refusal: {refusal}"
            finish_reason = "refusal"

        if content is None and not raw_tool_calls and not refusal:
            if finish_reason == "content_filter":
                # The answer was withheld by the content filter; the same input is blocked
                # again, so this is not a retryable malformed response.
                raise ProviderContentFilterError(
                    "The provider's content filter withheld the answer "
                    "(finish_reason: content_filter)",
                    reason="content_filter",
                )
            raise ProviderResponseError("Provider message has neither content nor tool calls")

        tool_calls: list[ToolCall] | None = None
        if raw_tool_calls:
            tool_calls = []
            seen_ids: set[str] = set()
            for tc in raw_tool_calls:
                if not isinstance(tc, dict) or "id" not in tc or "function" not in tc:
                    raise ProviderResponseError("Malformed tool_call item in response")
                tc_id = tc["id"]
                if not isinstance(tc_id, str) or not tc_id.strip() or tc_id in seen_ids:
                    raise ProviderResponseError("Tool call IDs must be nonempty and unique")
                seen_ids.add(tc_id)
                fn_dict = tc["function"]
                if not isinstance(fn_dict, dict) or "name" not in fn_dict:
                    raise ProviderResponseError("Malformed tool_call function in response")
                tc_name = fn_dict["name"]
                raw_args = fn_dict.get("arguments", "{}")
                try:
                    parsed_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    if not isinstance(parsed_args, dict):
                        raise ValueError("Arguments must decode to dict")
                except Exception as exc:
                    raise ProviderResponseError(
                        "Malformed tool call JSON arguments", original_error=exc
                    ) from exc

                raw_repr = (
                    raw_args
                    if isinstance(raw_args, str)
                    else json.dumps(raw_args, ensure_ascii=False)
                )
                tool_calls.append(
                    ToolCall(
                        id=tc_id,
                        name=tc_name,
                        arguments=parsed_args,
                        raw_arguments=raw_repr,
                    )
                )

        usage: Usage | None = None
        if "usage" in data and isinstance(data["usage"], dict):
            usage = openai_chat_usage(data["usage"])

        return ModelResponse(
            message=Message(
                role="assistant",
                content=content,
                tool_calls=tool_calls,
            ),
            usage=usage,
            finish_reason=finish_reason,
            **response_identity(data),
        )

    def _stream_internal(
        self,
        request: GenerationRequest,
        context: ExecutionContext | None,
    ) -> AsyncIterator[StreamDelta]:
        """Stream chat completions using SSE protocol."""
        # Unsupported feature check
        if request.provider_metadata.get("unsupported_feature"):
            raise CapabilityError(
                f"Feature '{request.provider_metadata['unsupported_feature']}' is not supported.",
                provider_name=self.model,
                capability=request.provider_metadata["unsupported_feature"],
            )

        client = self._get_client()
        request_timeout = request.timeout or self.timeout
        headers = self._build_headers()
        headers["Accept"] = "text/event-stream"

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [self._format_message(m) for m in request.messages],
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if request.tools:
            payload["tools"] = request.tools
            if request.tool_choice is not None:
                payload["tool_choice"] = openai_tool_choice(request.tool_choice)
        # Same request options as generate(): schema, limits, sampling and stop sequences.
        if request.schema and self.capabilities.can_structured_output:
            payload["response_format"] = openai_response_format(request.schema)
        if request.max_tokens:
            payload[self._max_tokens_field()] = request.max_tokens
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.stop_sequences:
            payload["stop"] = request.stop_sequences
        apply_openai_settings(payload, request)

        url = f"{self.base_url}/chat/completions"

        async def _generator() -> AsyncIterator[StreamDelta]:
            stream_done = False
            finish_seen = False

            def _redact(text: str) -> str:
                if self._api_key:
                    text = text.replace(self._api_key, "[REDACTED_SECRET]")
                return _mask_secrets(text)

            try:
                async with client.stream(
                    "POST",
                    url,
                    json=payload,
                    headers=headers,
                    timeout=request_timeout,
                ) as response:
                    await read_error_body(response)
                    self._handle_error_response(response)

                    async for line in response.aiter_lines():
                        line = line.strip()
                        if not line or line.startswith(":"):
                            continue
                        if not line.startswith("data: "):
                            continue

                        data_str = line[6:].strip()
                        if data_str == "[DONE]":
                            stream_done = True
                            break

                        try:
                            chunk = json.loads(data_str)
                        except json.JSONDecodeError as exc:
                            raise ProviderResponseError(
                                "Malformed SSE JSON chunk", original_error=exc
                            ) from exc

                        if not isinstance(chunk, dict):
                            raise ProviderResponseError(
                                "Malformed stream chunk (expected a JSON object)"
                            )
                        if chunk.get("error") is not None:
                            raise _error_from_chunk(chunk["error"], _redact)

                        # Continuation token / server metadata
                        server_id = chunk.get("id")
                        system_fingerprint = chunk.get("system_fingerprint")
                        if server_id:
                            yield StreamDelta(
                                kind="continuation_delta",
                                continuation=ContinuationPart(
                                    token=server_id,
                                    provider_metadata={"system_fingerprint": system_fingerprint},
                                ),
                            )

                        # Choices
                        choices = chunk.get("choices")
                        if choices is None:
                            choices = []
                        if not isinstance(choices, list):
                            raise ProviderResponseError(
                                "Provider stream chunk choices must be a list"
                            )
                        for choice in choices:
                            if not isinstance(choice, dict):
                                raise ProviderResponseError(
                                    "Provider stream choice must be an object"
                                )
                            delta = choice.get("delta")
                            if delta is None:
                                delta = {}
                            if not isinstance(delta, dict):
                                raise ProviderResponseError(
                                    "Provider stream delta must be an object"
                                )
                            # Text content
                            content_piece = delta.get("content")
                            if content_piece:
                                yield StreamDelta(kind="text_delta", text=content_piece)

                            # Refusal
                            refusal_piece = delta.get("refusal")
                            if refusal_piece:
                                finish_seen = True
                                yield StreamDelta(
                                    kind="finish",
                                    finish_reason="refusal",
                                    text=refusal_piece,
                                )

                            # Tool calls
                            tool_calls_deltas = delta.get("tool_calls")
                            if tool_calls_deltas is None:
                                tool_calls_deltas = []
                            if not isinstance(tool_calls_deltas, list):
                                raise ProviderResponseError("Provider tool_calls must be a list")
                            for tc in tool_calls_deltas:
                                if not isinstance(tc, dict):
                                    raise ProviderResponseError(
                                        "Malformed tool_call item in response"
                                    )
                                fn = tc.get("function")
                                if fn is None:
                                    fn = {}
                                if not isinstance(fn, dict):
                                    raise ProviderResponseError(
                                        "Malformed tool_call function in response"
                                    )
                                yield StreamDelta(
                                    kind="tool_call_delta",
                                    tool_call_index=tc.get("index"),
                                    tool_call_id=tc.get("id"),
                                    tool_name=fn.get("name"),
                                    tool_arguments_delta=fn.get("arguments"),
                                )

                            # Finish reason
                            finish_reason = choice.get("finish_reason")
                            if finish_reason:
                                finish_seen = True
                                yield StreamDelta(kind="finish", finish_reason=finish_reason)

                        # Top-level stream usage
                        if "usage" in chunk and isinstance(chunk["usage"], dict):
                            usage_obj = openai_chat_usage(chunk["usage"])
                            yield StreamDelta(kind="finish", usage=usage_obj)
                    if not (stream_done or finish_seen):
                        raise ProviderResponseError("Provider stream ended before completion")
            except httpx.TimeoutException as exc:
                raise ProviderRequestError(
                    f"Streaming from {self.base_url} timed out", original_error=exc
                ) from exc
            except httpx.RequestError as exc:
                raise ProviderRequestError(
                    f"HTTP network error in stream: {exc}", original_error=exc
                ) from exc

        return _generator()

    async def _embed_internal(
        self,
        texts: list[str],
        context: ExecutionContext | None,
    ) -> EmbeddingResult:
        """Generate vector embeddings using /embeddings endpoint."""
        client = self._get_client()
        headers = self._build_headers()
        url = f"{self.base_url}/embeddings"
        payload = {"model": self.model, "input": texts}

        try:
            response = await client.post(
                url,
                json=payload,
                headers=headers,
                timeout=self.timeout,
            )
        except httpx.RequestError as exc:
            raise ProviderRequestError(
                f"HTTP error during embedding: {exc}", original_error=exc
            ) from exc

        self._handle_error_response(response)

        try:
            data = response.json()
        except Exception as exc:
            raise ProviderResponseError(
                "Malformed embedding response JSON", original_error=exc
            ) from exc

        raw_data = data.get("data", [])
        embeddings = [item["embedding"] for item in raw_data if "embedding" in item]

        usage: Usage | None = None
        if "usage" in data and isinstance(data["usage"], dict):
            u = data["usage"]
            usage = Usage(
                prompt_tokens=u.get("prompt_tokens"),
                total_tokens=u.get("total_tokens"),
            )

        return EmbeddingResult(
            embeddings=embeddings,
            model=data.get("model", self.model),
            usage=usage,
        )

    def __repr__(self) -> str:
        key_repr = "'***'" if self._api_key else "None"
        return (
            f"OpenAIProvider(model={self.model!r}, base_url={self.base_url!r}, "
            f"api_key={key_repr}, timeout={self.timeout})"
        )
