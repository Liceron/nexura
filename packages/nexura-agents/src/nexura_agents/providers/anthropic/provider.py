# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Native Anthropic Messages API provider implementation."""

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
from nexura_agents.limits import anthropic_tool_choice, apply_anthropic_settings
from nexura_agents.providers.anthropic.types import (
    ANTHROPIC_THINKING_KEY,
    extract_anthropic_response,
    map_anthropic_stop_reason,
    to_anthropic_messages,
    to_anthropic_tools,
)
from nexura_agents.providers.base import BaseProvider
from nexura_agents.providers.errors import (
    CapabilityError,
    StreamCancelledError,
    http_status_error,
    read_error_body,
    safe_response_text,
)
from nexura_agents.providers.types import (
    ContinuationPart,
    EmbeddingResult,
    GenerationRequest,
    ProviderCapabilities,
    StreamDelta,
)
from nexura_agents.types import ModelResponse, Usage

_ANTHROPIC_KEY_PATTERN = re.compile(r"sk-ant-[a-zA-Z0-9_-]{20,}", re.IGNORECASE)


def _redact_anthropic_secrets(text: str) -> str:
    """Redact Anthropic credential tokens from error messages."""
    return _ANTHROPIC_KEY_PATTERN.sub("[REDACTED_ANTHROPIC_KEY]", text)


STRUCTURED_OUTPUT_TOOL = "structured_output"
_WRAPPED_KEY = "value"


def _is_wrapped(schema: dict[str, Any]) -> bool:
    return schema.get("type") != "object"


def _structured_output_tool(schema: dict[str, Any]) -> dict[str, Any]:
    """Synthetic tool whose input schema is the output schema (non-objects get wrapped)."""
    input_schema = schema
    if _is_wrapped(schema):
        inner = {k: v for k, v in schema.items() if k not in ("$defs", "definitions")}
        input_schema = {
            "type": "object",
            "properties": {_WRAPPED_KEY: inner},
            "required": [_WRAPPED_KEY],
        }
        for key in ("$defs", "definitions"):  # refs resolve from the new root
            if key in schema:
                input_schema[key] = schema[key]
    return {
        "name": STRUCTURED_OUTPUT_TOOL,
        "description": "Return the final answer. Call this tool with the answer as its input.",
        "input_schema": input_schema,
    }


def _structured_output_message(
    msg: Any, finish_reason: str, schema: dict[str, Any]
) -> tuple[Any, str]:
    """Turn the forced tool call's arguments into the final JSON text of the message."""
    calls = msg.tool_calls or []
    chosen = next((c for c in calls if c.name == STRUCTURED_OUTPUT_TOOL), None)
    if chosen is None:
        return msg, finish_reason
    value = chosen.arguments
    if _is_wrapped(schema) and isinstance(value, dict) and _WRAPPED_KEY in value:
        value = value[_WRAPPED_KEY]
    msg.content = json.dumps(value, ensure_ascii=False)
    msg.tool_calls = None
    return msg, ("stop" if finish_reason in ("tool_use", "tool_calls") else finish_reason)


class AnthropicNativeProvider(BaseProvider):
    """Native Anthropic provider adapter communicating with Anthropic Messages API.

    Guarantees:
    - Native content-block ordering and tool IDs preserved across turns.
    - Mixed text and tool_use blocks survive turn replay.
    - Validates missing or duplicate tool IDs eagerly.
    - Max-token/refusal termination is mapped explicitly to 'length'/'refusal'.
    - Stream cancellation cleanly closes owned transports and connections.
    - Rejects unsupported audio or vector embedding requests via CapabilityError.
    - Credentials are redacted from representations and errors.
    """

    # Native structured output via a forced synthetic tool (only when no other tools are
    # offered: tool_choice can force one tool, so tool agents keep the prompt schema).
    supports_response_schema = True
    response_schema_with_tools = False

    def __init__(
        self,
        model: str = "claude-3-5-sonnet-20241022",
        *,
        api_key: str | None = None,
        base_url: str = "https://api.anthropic.com",
        timeout: float = 30.0,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        capabilities: ProviderCapabilities | None = None,
        default_max_tokens: int = 4096,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        caps = capabilities or ProviderCapabilities(
            can_generate=True,
            can_stream=True,
            can_tools=True,
            can_structured_output=True,
            can_multimodal=True,
            can_embed=False,
            reports_usage=True,
        )
        super().__init__(model_id=model, capabilities=caps)
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.default_max_tokens = default_max_tokens
        self.extra_headers = extra_headers or {}

        resolved_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
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
        """Return the underlying HTTP transport."""
        return self._transport

    def __repr__(self) -> str:
        masked_key = "***" if self._api_key else None
        return (
            f"AnthropicNativeProvider(model={self.model!r}, "
            f"api_key={masked_key!r}, timeout={self.timeout})"
        )

    def _build_headers(self) -> dict[str, str]:
        headers = {
            "x-api-key": self._api_key or "",
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        headers.update(self.extra_headers)
        return headers

    def _validate_request_capabilities(self, request: GenerationRequest) -> None:
        """Validate Anthropic-specific capability constraints."""
        meta = request.provider_metadata
        if meta.get("audio") or meta.get("media_type") == "audio":
            raise CapabilityError(
                "Anthropic native Messages API does not support native audio.",
                provider_name=self.model,
                capability="audio",
            )

    def _handle_http_error(self, exc: httpx.HTTPStatusError) -> None:
        """Translate HTTP status errors to typed exceptions with secret redaction."""
        status_code = exc.response.status_code
        raw_text = safe_response_text(exc.response)
        safe_text = _redact_anthropic_secrets(raw_text)

        if status_code in (401, 403):
            raise ProviderAuthError(
                f"Anthropic authentication failed (HTTP {status_code}): {safe_text}",
                original_error=exc,
            ) from exc

        if status_code == 429 or status_code >= 400:
            what = (
                "rate limit exceeded"
                if status_code == 429
                else "bad request"
                if status_code < 500
                else "server error"
            )
            raise http_status_error(
                status_code,
                f"Anthropic {what} (HTTP {status_code}): {safe_text}",
                retry_after=exc.response.headers.get("retry-after"),
                original_error=exc,
            ) from exc

    async def _generate_internal(
        self,
        request: GenerationRequest,
        context: ExecutionContext | None,
    ) -> ModelResponse:
        self._validate_request_capabilities(request)

        cache_policy = request.cache_policy or request.provider_metadata.get("cache_policy")
        if cache_policy:
            from nexura_agents.providers.cache.errors import UnsupportedCachePolicyError

            pref_mode = getattr(cache_policy, "preferred_mode", None) or (
                cache_policy.get("preferred_mode") if isinstance(cache_policy, dict) else None
            )
            if pref_mode and pref_mode not in ("auto", "ephemeral_routing"):
                raise UnsupportedCachePolicyError(
                    f"Anthropic adapter only supports ephemeral routing cache, not '{pref_mode}'.",
                    model_id=self.model,
                    option=pref_mode,
                )
            ttl = getattr(cache_policy, "ttl_seconds", None) or (
                cache_policy.get("ttl_seconds") if isinstance(cache_policy, dict) else None
            )
            if ttl is not None and ttl != 300:
                raise UnsupportedCachePolicyError(
                    f"Anthropic adapter does not support custom TTL ({ttl}s); "
                    "Anthropic prompt caching only supports fixed 5-minute ephemeral TTL.",
                    model_id=self.model,
                    option="ttl_seconds",
                )

        system_prompt, messages = to_anthropic_messages(request.messages)
        tools = to_anthropic_tools(request.tools)

        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": request.max_tokens or self.default_max_tokens,
            "messages": messages,
        }
        if system_prompt:
            if request.cache_plan or (cache_policy and getattr(cache_policy, "enabled", True)):
                payload["system"] = [
                    {
                        "type": "text",
                        "text": system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
            else:
                payload["system"] = system_prompt
        if request.schema:
            if tools:
                raise CapabilityError(
                    "Anthropic native structured output uses a forced tool and cannot be "
                    "combined with other tools.",
                    provider_name=self.model,
                    capability="structured_output_with_tools",
                )
            tools = [_structured_output_tool(request.schema)]
            payload["tool_choice"] = {"type": "tool", "name": STRUCTURED_OUTPUT_TOOL}
        if tools:
            payload["tools"] = tools
            if request.tool_choice is not None and "tool_choice" not in payload:
                payload["tool_choice"] = anthropic_tool_choice(request.tool_choice)
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.stop_sequences:
            payload["stop_sequences"] = request.stop_sequences
        apply_anthropic_settings(payload, request)

        url = f"{self.base_url}/v1/messages"
        headers = self._build_headers()

        try:
            response = await self._client.post(
                url,
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            self._handle_http_error(exc)
        except httpx.RequestError as exc:
            safe_err = _redact_anthropic_secrets(str(exc))
            raise ProviderRequestError(
                f"Network request to Anthropic failed: {safe_err}",
                original_error=exc,
            ) from exc

        try:
            data = response.json()
        except Exception as exc:
            raise ProviderResponseError(
                f"Failed to parse Anthropic JSON response: {exc}",
                original_error=exc,
            ) from exc

        msg, usage, finish_reason, metadata = extract_anthropic_response(data)
        if request.schema:
            msg, finish_reason = _structured_output_message(msg, finish_reason, request.schema)
        return ModelResponse(
            message=msg,
            usage=usage,
            finish_reason=finish_reason,
        )

    async def _stream_internal(
        self,
        request: GenerationRequest,
        context: ExecutionContext | None,
    ) -> AsyncIterator[StreamDelta]:
        self._validate_request_capabilities(request)

        cache_policy = request.cache_policy or request.provider_metadata.get("cache_policy")
        if cache_policy:
            from nexura_agents.providers.cache.errors import UnsupportedCachePolicyError

            pref_mode = getattr(cache_policy, "preferred_mode", None) or (
                cache_policy.get("preferred_mode") if isinstance(cache_policy, dict) else None
            )
            if pref_mode and pref_mode not in ("auto", "ephemeral_routing"):
                raise UnsupportedCachePolicyError(
                    f"Anthropic adapter only supports ephemeral routing cache, not '{pref_mode}'.",
                    model_id=self.model,
                    option=pref_mode,
                )
            ttl = getattr(cache_policy, "ttl_seconds", None) or (
                cache_policy.get("ttl_seconds") if isinstance(cache_policy, dict) else None
            )
            if ttl is not None and ttl != 300:
                raise UnsupportedCachePolicyError(
                    f"Anthropic adapter does not support custom TTL ({ttl}s); "
                    "Anthropic prompt caching only supports fixed 5-minute ephemeral TTL.",
                    model_id=self.model,
                    option="ttl_seconds",
                )

        system_prompt, messages = to_anthropic_messages(request.messages)
        tools = to_anthropic_tools(request.tools)

        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": request.max_tokens or self.default_max_tokens,
            "messages": messages,
            "stream": True,
        }
        if system_prompt:
            if request.cache_plan or (cache_policy and getattr(cache_policy, "enabled", True)):
                payload["system"] = [
                    {
                        "type": "text",
                        "text": system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
            else:
                payload["system"] = system_prompt
        if tools:
            payload["tools"] = tools
            if request.tool_choice is not None and "tool_choice" not in payload:
                payload["tool_choice"] = anthropic_tool_choice(request.tool_choice)
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.stop_sequences:
            payload["stop_sequences"] = request.stop_sequences
        apply_anthropic_settings(payload, request)

        url = f"{self.base_url}/v1/messages"
        headers = self._build_headers()

        response = None
        try:
            req = self._client.build_request("POST", url, json=payload, headers=headers)
            response = await self._client.send(req, stream=True)
            await read_error_body(response)
            response.raise_for_status()

            current_event: str | None = None
            active_tool_calls: dict[int, dict[str, Any]] = {}
            thinking_open: dict[int, dict[str, Any]] = {}  # thinking blocks being streamed
            total_input_tokens: int | None = None
            cache_creation_tokens: int | None = None
            cache_read_tokens: int | None = None
            stream_finished = False

            async for line in response.aiter_lines():
                if context is not None and context.is_cancelled:
                    raise StreamCancelledError(
                        f"Stream cancelled for provider '{self.model}'.",
                        stream_id=context.run_id,
                    )

                clean_line = line.strip()
                if not clean_line:
                    current_event = None
                    continue

                if clean_line.startswith("event:"):
                    current_event = clean_line[len("event:") :].strip()
                    continue

                if clean_line.startswith("data:"):
                    data_str = clean_line[len("data:") :].strip()
                    if not data_str:
                        continue

                    try:
                        event_data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    event_type = event_data.get("type") or current_event

                    if event_type == "error":
                        err = event_data.get("error", {})
                        safe_msg = _redact_anthropic_secrets(
                            err.get("message", "Anthropic stream error")
                        )
                        raise ProviderResponseError(
                            f"Anthropic streaming error ({err.get('type')}): {safe_msg}"
                        )

                    if event_type == "message_start":
                        msg_obj = event_data.get("message", {})
                        usage_obj = msg_obj.get("usage", {})
                        total_input_tokens = usage_obj.get("input_tokens")
                        cache_creation_tokens = usage_obj.get("cache_creation_input_tokens")
                        cache_read_tokens = usage_obj.get("cache_read_input_tokens")
                        continue

                    if event_type == "content_block_start":
                        index = event_data.get("index", 0)
                        block = event_data.get("content_block", {})
                        if block.get("type") == "tool_use":
                            tc_id = block.get("id", "")
                            tc_name = block.get("name", "")
                            active_tool_calls[index] = {
                                "id": tc_id,
                                "name": tc_name,
                            }
                        elif block.get("type") in ("thinking", "redacted_thinking"):
                            thinking_open[index] = dict(block)
                        continue

                    if event_type == "content_block_delta":
                        index = event_data.get("index", 0)
                        delta = event_data.get("delta", {})
                        delta_type = delta.get("type")

                        if delta_type == "text_delta":
                            yield StreamDelta(
                                kind="text_delta",
                                text=delta.get("text", ""),
                            )
                        elif delta_type == "thinking_delta" and index in thinking_open:
                            open_block = thinking_open[index]
                            open_block["thinking"] = open_block.get("thinking", "") + delta.get(
                                "thinking", ""
                            )
                        elif delta_type == "signature_delta" and index in thinking_open:
                            open_block = thinking_open[index]
                            open_block["signature"] = open_block.get("signature", "") + delta.get(
                                "signature", ""
                            )
                        elif delta_type == "input_json_delta":
                            tc_info = active_tool_calls.get(index, {})
                            yield StreamDelta(
                                kind="tool_call_delta",
                                tool_call_index=index,
                                tool_call_id=tc_info.get("id"),
                                tool_name=tc_info.get("name"),
                                tool_arguments_delta=delta.get("partial_json", ""),
                            )
                        continue

                    if event_type == "content_block_stop":
                        finished = thinking_open.pop(event_data.get("index", 0), None)
                        if finished is not None:
                            yield StreamDelta(
                                kind="continuation_delta",
                                continuation=ContinuationPart(
                                    token=ANTHROPIC_THINKING_KEY,
                                    provider_metadata={"block": finished},
                                ),
                            )
                        continue

                    if event_type == "message_delta":
                        delta = event_data.get("delta", {})
                        stop_reason = delta.get("stop_reason")
                        finish_reason = map_anthropic_stop_reason(stop_reason)
                        usage_obj = event_data.get("usage", {})
                        output_tokens = usage_obj.get("output_tokens")

                        total_tokens = None
                        if total_input_tokens is not None or output_tokens is not None:
                            total_tokens = (total_input_tokens or 0) + (output_tokens or 0)

                        stream_usage = Usage(
                            prompt_tokens=total_input_tokens,
                            completion_tokens=output_tokens,
                            total_tokens=total_tokens,
                            is_complete=True,
                            cached_prompt_tokens=cache_read_tokens,
                            cache_creation_input_tokens=cache_creation_tokens,
                            cache_read_input_tokens=cache_read_tokens,
                        )
                        yield StreamDelta(
                            kind="finish",
                            finish_reason=finish_reason,
                            usage=stream_usage,
                        )
                        stream_finished = True
                        continue

                    if event_type == "message_stop":
                        stream_finished = True
                        break

            if not stream_finished:
                raise ProviderResponseError(
                    "Anthropic stream ended before message_delta/message_stop (truncated stream)."
                )

        except httpx.HTTPStatusError as exc:
            self._handle_http_error(exc)
        except httpx.RequestError as exc:
            safe_err = _redact_anthropic_secrets(str(exc))
            raise ProviderRequestError(
                f"Network request to Anthropic failed: {safe_err}",
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
        """Reject vector embedding requests as Anthropic does not offer native embeddings."""
        raise CapabilityError(
            "Anthropic does not offer native vector embedding endpoints. "
            "Use a dedicated embedding provider (e.g. OpenAI or local models).",
            provider_name=self.model,
            capability="embed",
        )

    async def aclose(self) -> None:
        """Close owned HTTP client and release underlying connections."""
        if self._owns_client and hasattr(self, "_client"):
            await self._client.aclose()

    def close(self) -> None:
        """Synchronously close client if event loop is not active."""
        if self._owns_client and hasattr(self, "_client"):
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.aclose())
            except RuntimeError:
                asyncio.run(self.aclose())
