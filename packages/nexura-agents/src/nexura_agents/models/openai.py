# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""OpenAI-compatible Chat Completions HTTP adapter.

Provides model interaction via HTTP with support for injected transports (for mocking),
secret masking, bounded timeouts, tool-call validation, and robust error mapping.
"""

from __future__ import annotations

import contextlib
import copy
import json
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx

from nexura_agents.errors import (
    ProviderAuthError,
    ProviderRequestError,
    ProviderResponseError,
    SDKValidationError,
)
from nexura_agents.json_schema import openai_response_format
from nexura_agents.limits import ModelSettings, merge_extra, settings_from_request, warn_unsupported
from nexura_agents.models._streaming import ChatChunkDecoder
from nexura_agents.models.base import ModelProtocol
from nexura_agents.providers.errors import error_body_codes, http_status_error, response_excerpt
from nexura_agents.providers.multimodal import openai_content_parts
from nexura_agents.providers.types import GenerationRequest, ProviderCapabilities, StreamDelta
from nexura_agents.types import Message, ModelResponse, ToolCall, Usage, response_identity


def _usage_from_openai(u_dict: dict[str, Any]) -> Usage:
    """Same mapping as ``OpenAIProvider``: cached, cache-write and reasoning breakdowns too."""
    from nexura_agents.providers.openai.provider import openai_chat_usage

    return openai_chat_usage(u_dict)


_RESERVED_BODY_KEYS = frozenset(
    {"model", "messages", "tools", "stream", "stream_options", "response_format"}
)
# Set by the SDK, or by the HTTP client to frame the request: never from user headers.
_ALWAYS_SDK_HEADERS = frozenset(
    {"content-type", "accept", "content-length", "host", "transfer-encoding", "connection"}
)
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


def _check_request_options(
    headers: dict[str, str] | None,
    extra_body: dict[str, Any] | None,
    query_params: dict[str, Any] | None,
    has_api_key: bool,
) -> tuple[dict[str, str], dict[str, Any], dict[str, Any]]:
    """Validate constructor request options and return private copies.

    Errors name keys only, never values, so a secret header cannot leak into a message.
    """
    for option, given in (
        ("headers", headers),
        ("extra_body", extra_body),
        ("query_params", query_params),
    ):
        if given is not None and not isinstance(given, dict):
            raise SDKValidationError(f"{option} must be a dict, not {type(given).__name__}.")
    hdrs: dict[str, str] = {}
    seen: set[str] = set()
    for name, value in (headers or {}).items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise SDKValidationError("headers must map non-empty text names to text values.")
        if not _HEADER_NAME.match(name):
            raise SDKValidationError("header names must be HTTP tokens (no spaces or colons).")
        if "\r" in value or "\n" in value:
            raise SDKValidationError(f"header {name!r} value must be a single line.")
        lowered = name.lower()
        if lowered in seen:
            raise SDKValidationError(f"headers repeat {name!r} with different letter case.")
        seen.add(lowered)
        if lowered in _ALWAYS_SDK_HEADERS or (lowered == "authorization" and has_api_key):
            raise SDKValidationError(
                f"headers cannot set {name!r}: the SDK sets it itself "
                "(Authorization is only accepted when api_key is not set)."
            )
        hdrs[name] = value
    body = dict(extra_body or {})
    if not all(isinstance(k, str) for k in body):
        raise SDKValidationError("extra_body must be a dict with string keys.")
    clash = sorted(k for k in body if k in _RESERVED_BODY_KEYS)
    if clash:
        raise SDKValidationError(
            f"extra_body cannot set {', '.join(clash)}, which the SDK sets for every request."
        )
    try:
        json.dumps(body)
    except (TypeError, ValueError) as exc:
        raise SDKValidationError(f"extra_body must be JSON-serialisable: {exc}") from exc
    query = dict(query_params or {})
    if not all(isinstance(k, str) for k in query):
        raise SDKValidationError("query_params must be a dict with string keys.")
    return hdrs, copy.deepcopy(body), copy.deepcopy(query)


class OpenAIChatModel(ModelProtocol):
    """Adapter for OpenAI-compatible chat completion endpoints.

    Secrets (API keys) are masked in string representations and error messages.
    Supports injected httpx.AsyncClient or httpx.AsyncBaseTransport for deterministic testing.

    ``headers``, ``extra_body`` and ``query_params`` are defaults sent with every request.
    ``headers`` cannot set Content-Type or Accept, nor Authorization when ``api_key`` is set.
    ``extra_body`` cannot set ``model``, ``messages``, ``tools``, ``stream``,
    ``stream_options`` or ``response_format``. Precedence in the body: SDK fields, then
    ``ModelSettings`` fields and per-call ``ModelSettings.extra``, then ``extra_body``.
    Header values are never logged, shown in ``repr`` or put in errors.
    """

    # Native structured output (response_format json_schema). Not sent together with tools:
    # OpenAI-compatible servers differ in whether they accept both, so tool agents keep the
    # schema in the prompt.
    supports_response_schema = True
    streams_response_schema = True
    response_schema_with_tools = False
    # Opt-in token streaming: ``stream()`` yields the same ``StreamDelta`` values the Runner
    # consumes from provider models. ``generate()`` is unchanged and returns whole responses.
    capabilities = ProviderCapabilities(
        can_stream=True, can_tools=True, can_structured_output=True, reports_usage=True
    )

    # Same rule as OpenAIProvider: ``max_completion_tokens`` for api.openai.com (reasoning
    # models reject the deprecated ``max_tokens``), ``max_tokens`` for other servers. Set the
    # instance attribute to override.
    max_tokens_param: str | None = None

    def _settings_params(self, model_settings: ModelSettings) -> dict[str, Any]:
        from nexura_agents.providers.openai.provider import _is_official_openai

        params = model_settings.to_openai_params()
        field = self.max_tokens_param or (
            "max_completion_tokens" if _is_official_openai(self.base_url) else "max_tokens"
        )
        if "max_tokens" in params and field != "max_tokens":
            params[field] = params.pop("max_tokens")
        return params

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        base_url: str = "https://api.openai.com/v1",
        timeout: float = 30.0,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        headers: dict[str, str] | None = None,
        extra_body: dict[str, Any] | None = None,
        query_params: dict[str, Any] | None = None,
    ) -> None:
        self.model = model
        self._headers, self._extra_body, self._query_params = _check_request_options(
            headers, extra_body, query_params, bool(api_key)
        )
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client = client
        self.transport = transport
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
        """Close internally managed HTTP client if present."""
        if self._internal_client is not None and not self._internal_client.is_closed:
            await self._internal_client.aclose()
            self._internal_client = None

    def _format_message(self, msg: Message) -> dict[str, Any]:
        """Convert SDK Message into OpenAI chat completion message format."""
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

    async def generate(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        timeout: float | None = None,
        response_schema: dict[str, Any] | None = None,
        model_settings: ModelSettings | None = None,
    ) -> ModelResponse:
        """Send chat completion request to the OpenAI-compatible endpoint."""
        client = self._get_client()
        request_timeout = timeout if timeout is not None else self.timeout

        headers = {**self._headers, "Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [self._format_message(m) for m in messages],
        }
        if tools:
            payload["tools"] = tools
        if response_schema is not None:
            payload["response_format"] = openai_response_format(response_schema)
        if model_settings is not None:
            payload.update(self._settings_params(model_settings))
            warn_unsupported("OpenAIChatModel", model_settings, ("thinking_budget_tokens",))
            if model_settings.extra:
                merge_extra(payload, model_settings.extra)
        for key, value in self._extra_body.items():
            if key not in payload:  # per-call settings and extra win over defaults
                payload[key] = copy.deepcopy(value)

        url = f"{self.base_url}/chat/completions"

        try:
            response = await client.post(
                url,
                json=payload,
                headers=headers,
                params=self._query_params or None,
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

        # Check HTTP status codes
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

        # Parse response body
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
        raw_tool_calls = msg_dict.get("tool_calls")
        finish_reason = choice.get("finish_reason")
        if content is not None and not isinstance(content, str):
            raise ProviderResponseError("Provider message content must be text or null")
        if raw_tool_calls is not None and not isinstance(raw_tool_calls, list):
            raise ProviderResponseError("Provider tool_calls must be a list or null")
        if finish_reason is not None and not isinstance(finish_reason, str):
            raise ProviderResponseError("Provider finish_reason must be text or null")
        if content is None and not raw_tool_calls:
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
                if tc.get("type", "function") != "function":
                    raise ProviderResponseError("Unsupported tool call type")
                fn_dict = tc["function"]
                if not isinstance(fn_dict, dict) or "name" not in fn_dict:
                    raise ProviderResponseError("Malformed tool_call function in response")
                tc_name = fn_dict["name"]
                if not isinstance(tc_name, str) or not tc_name.strip():
                    raise ProviderResponseError("Tool call name must be nonempty text")
                raw_args = fn_dict.get("arguments", "{}")

                try:
                    parsed_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    if not isinstance(parsed_args, dict):
                        raise ValueError(
                            f"Arguments must decode to a dict, got {type(parsed_args)}"
                        )
                except Exception as exc:
                    raise ProviderResponseError(
                        "Malformed tool call JSON arguments",
                        original_error=exc,
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

        # Extract usage if present; distinguish from unknown
        usage: Usage | None = None
        if "usage" in data and isinstance(data["usage"], dict):
            usage = _usage_from_openai(data["usage"])

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

    async def stream(
        self,
        request: GenerationRequest | list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        timeout: float | None = None,
        response_schema: dict[str, Any] | None = None,
        model_settings: ModelSettings | None = None,
    ) -> AsyncIterator[StreamDelta]:
        """Stream a chat completion (SSE) as ``StreamDelta`` values.

        Text arrives as ``text_delta``, tool-call fragments as ``tool_call_delta`` (the
        Runner assembles and validates them at the end), then ``usage_delta`` (requested
        with ``stream_options.include_usage``) and ``finish``. HTTP and transport errors map
        exactly as in :meth:`generate`; an error event, a malformed chunk or a stream that
        ends before completion raises the typed error, so partial text is never a final
        answer. Closing the iterator (cancellation) closes the HTTP stream.
        """
        if isinstance(request, GenerationRequest):
            messages = request.to_legacy_messages()
            tools = tools if tools is not None else request.tools
            timeout = timeout if timeout is not None else request.timeout
            response_schema = response_schema if response_schema is not None else request.schema
            if model_settings is None:
                model_settings = settings_from_request(request)
        else:
            messages = list(request)
        client = self._get_client()
        request_timeout = timeout if timeout is not None else self.timeout

        headers = {
            **self._headers,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [self._format_message(m) for m in messages],
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = tools
        if response_schema is not None:
            payload["response_format"] = openai_response_format(response_schema)
        if model_settings is not None:
            payload.update(self._settings_params(model_settings))
            warn_unsupported("OpenAIChatModel", model_settings, ("thinking_budget_tokens",))
            if model_settings.extra:
                merge_extra(payload, model_settings.extra)
        for key, value in self._extra_body.items():
            if key not in payload:  # per-call settings and extra win over defaults
                payload[key] = copy.deepcopy(value)

        decoder = ChatChunkDecoder(
            _usage_from_openai, api_key=self._api_key, secrets=self._headers.values()
        )
        try:
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=headers,
                params=self._query_params or None,
                timeout=request_timeout,
            ) as response:
                if response.status_code == 401:
                    raise ProviderAuthError(
                        "Provider authentication failed (HTTP 401: Invalid API Key)"
                    )
                if response.status_code >= 400:
                    # Read the body for every error (a 429's code tells a quota error apart).
                    with contextlib.suppress(Exception):
                        await response.aread()
                    raise http_status_error(
                        response.status_code,
                        "Rate limit exceeded or provider quota exhausted (HTTP 429)"
                        if response.status_code == 429
                        else f"Provider returned error HTTP {response.status_code}",
                        retry_after=response.headers.get("Retry-After"),
                        detail=response_excerpt(response),
                        codes=error_body_codes(response),
                    )
                data_lines: list[str] = []

                async def _lines() -> AsyncIterator[str]:
                    async for raw in response.aiter_lines():
                        yield raw
                    yield ""  # dispatch a trailing event lacking a final blank line

                async for line in _lines():
                    line = line.rstrip("\r\n")
                    if line.strip():
                        if line.startswith("data:"):
                            value = line[5:]
                            data_lines.append(value[1:] if value.startswith(" ") else value)
                        continue  # comments, other SSE fields
                    if not data_lines:
                        continue
                    data_str = "\n".join(data_lines).strip()
                    data_lines = []
                    if not data_str:
                        continue  # keep-alive / empty event
                    if data_str == "[DONE]":
                        decoder.done = True
                        break
                    try:
                        chunk = json.loads(data_str)
                    except ValueError as exc:
                        raise ProviderResponseError(
                            "Failed to parse provider stream chunk as JSON",
                            original_error=exc,
                        ) from exc
                    for delta in decoder.feed(chunk):
                        yield delta
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
        decoder.finish()

    def __repr__(self) -> str:
        key_repr = "'***'" if self._api_key else "None"
        return (
            f"OpenAIChatModel(model={self.model!r}, base_url={self.base_url!r}, "
            f"api_key={key_repr}, timeout={self.timeout}, headers={sorted(self._headers)})"
        )
