# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Native Gemini provider implementation preserving capabilities and thought parts."""

from __future__ import annotations

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
from nexura_agents.json_schema import to_gemini_schema
from nexura_agents.limits import apply_gemini_settings, gemini_tool_config
from nexura_agents.providers.base import BaseProvider
from nexura_agents.providers.errors import (
    CapabilityError,
    http_status_error,
    read_error_body,
    response_excerpt,
)
from nexura_agents.providers.gemini.types import (
    GeminiThoughtMetadata,
    extract_gemini_tool_calls,
    to_gemini_contents,
    to_gemini_tools,
)
from nexura_agents.providers.types import (
    ContinuationPart,
    EmbeddingResult,
    GenerationRequest,
    ProviderCapabilities,
    StreamDelta,
)
from nexura_agents.types import Message, ModelResponse, Usage

_INVALID_CLI_ALIASES = {"gemini-cli", "cli", "gemini", "gemini-code", "antigravity-gemini"}


_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")


def _billed_output_tokens(usage_metadata: dict[str, Any]) -> int | None:
    """Visible output plus thinking tokens: Gemini bills thoughts at the output rate."""
    candidates = usage_metadata.get("candidatesTokenCount")
    thoughts = usage_metadata.get("thoughtsTokenCount")
    if candidates is None and thoughts is None:
        return None
    return int(candidates or 0) + int(thoughts or 0)


def _google_error_codes(response: httpx.Response) -> str:
    """Google's machine-readable status and reason codes, e.g. ``[PERMISSION_DENIED,
    SERVICE_DISABLED]``. Free-text error messages are deliberately not included."""
    try:
        error = response.json().get("error", {})
    except Exception:  # invalid JSON, or a streamed body that was not read
        return ""
    if not isinstance(error, dict):
        return ""
    codes = [error.get("status")]
    for detail in error.get("details") or []:
        if isinstance(detail, dict):
            codes.append(detail.get("reason"))
    safe = [c for c in dict.fromkeys(codes) if isinstance(c, str) and _CODE.match(c)]
    return f" [{', '.join(safe)}]" if safe else ""


class GeminiNativeProvider(BaseProvider):
    """Native Gemini provider adapter communicating with Google Generative Language APIs.

    Guarantees:
    - Preserves thought and reasoning signatures without discarding metadata.
    - Translates native Gemini functionCall/functionResponse tool wire contracts.
    - Emits real-time SSE stream deltas for text and fragmented tool arguments.
    - Rejects CLI aliases (e.g. 'gemini-cli') early.
    - Preserves omitted usage without zero-cost fabrication.
    - Rejects unsupported native capabilities eagerly via CapabilityError.
    """

    # Native structured output (responseSchema + responseMimeType). Gemini models do not
    # all accept function calling together with a response schema, so tool agents keep the
    # schema in the prompt.
    supports_response_schema = True
    streams_response_schema = True  # stream() sends the schema too (partial output events)
    response_schema_with_tools = False

    def __init__(
        self,
        model: str = "gemini-2.5-flash",
        *,
        api_key: str | None = None,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        timeout: float = 30.0,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        capabilities: ProviderCapabilities | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        if model.lower() in _INVALID_CLI_ALIASES:
            raise CapabilityError(
                f"Model ID '{model}' is invalid. Never hardcode coding-agent CLI aliases "
                "as API model IDs. Use concrete models such as 'gemini-2.5-flash'.",
                provider_name=model,
                capability="model_id",
            )

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
        self._api_key = (
            api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        )
        self.base_url = base_url.rstrip("/")
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
            headers["x-goog-api-key"] = self._api_key
        headers.update(self.extra_headers)
        return headers

    def _handle_error_response(self, response: httpx.Response) -> None:
        """Map Gemini error status codes into typed exceptions."""
        if response.status_code in (401, 403):
            raise ProviderAuthError(
                "Gemini authentication failed: invalid API key or access denied"
                + _google_error_codes(response)
            )
        if response.status_code >= 400:
            suffix = _google_error_codes(response) if response.status_code != 429 else ""
            raise http_status_error(
                response.status_code,
                (
                    "Gemini quota exhausted or rate limit reached (HTTP 429)"
                    if response.status_code == 429
                    else f"Gemini returned error HTTP {response.status_code}"
                )
                + suffix,
                detail=response_excerpt(response),
                retry_after=response.headers.get("Retry-After"),
            )

    async def _generate_internal(
        self,
        request: GenerationRequest,
        context: ExecutionContext | None,
    ) -> ModelResponse:
        """Execute non-streaming content generation."""
        if request.provider_metadata.get("unsupported_feature"):
            feat = request.provider_metadata["unsupported_feature"]
            raise CapabilityError(
                f"Gemini does not support feature: {feat}",
                provider_name=self.model,
                capability=feat,
            )

        cache_policy = request.cache_policy or request.provider_metadata.get("cache_policy")
        if cache_policy:
            from nexura_agents.providers.cache.errors import UnsupportedCachePolicyError

            pref_mode = getattr(cache_policy, "preferred_mode", None) or (
                cache_policy.get("preferred_mode") if isinstance(cache_policy, dict) else None
            )
            if pref_mode and pref_mode not in ("auto", "explicit_stored_context"):
                raise UnsupportedCachePolicyError(
                    f"Gemini adapter only supports explicit stored context caching, "
                    f"not '{pref_mode}'.",
                    model_id=self.model,
                    option=pref_mode,
                )

        client = self._get_client()
        headers = self._build_headers()
        url = f"{self.base_url}/models/{self.model}:generateContent"

        contents = to_gemini_contents(request.messages)
        tools = to_gemini_tools(request.tools)

        payload: dict[str, Any] = {"contents": contents}
        if request.cache_plan and getattr(request.cache_plan, "cache_id", None):
            payload["cachedContent"] = request.cache_plan.cache_id
        if tools:
            payload["tools"] = tools
            if request.tool_choice is not None:
                payload["toolConfig"] = gemini_tool_config(request.tool_choice)

        gen_config: dict[str, Any] = {}
        if request.temperature is not None:
            gen_config["temperature"] = request.temperature
        if request.max_tokens:
            gen_config["maxOutputTokens"] = request.max_tokens
        if request.stop_sequences:
            gen_config["stopSequences"] = request.stop_sequences
        if request.schema and self.capabilities.can_structured_output:
            gen_config["responseMimeType"] = "application/json"
            gen_config["responseSchema"] = to_gemini_schema(request.schema)
        apply_gemini_settings(payload, gen_config, request)

        req_timeout = request.timeout or self.timeout
        try:
            response = await client.post(
                url,
                json=payload,
                headers=headers,
                timeout=req_timeout,
            )
        except httpx.TimeoutException as exc:
            raise ProviderRequestError(
                f"Request to Gemini timed out ({req_timeout}s)", original_error=exc
            ) from exc
        except httpx.RequestError as exc:
            raise ProviderRequestError(
                f"Network error contacting Gemini: {exc}", original_error=exc
            ) from exc

        if response.status_code == 404 and payload.get("cachedContent"):
            from nexura_agents.providers.cache.errors import CacheExpiredError

            raise CacheExpiredError(
                f"Gemini cached context '{payload['cachedContent']}' "
                "not found or expired (HTTP 404).",
                cache_id=payload["cachedContent"],
            )

        self._handle_error_response(response)

        try:
            data = response.json()
        except Exception as exc:
            raise ProviderResponseError(
                "Malformed JSON response from Gemini", original_error=exc
            ) from exc

        candidates = data.get("candidates", [])
        if not candidates or not isinstance(candidates, list):
            raise ProviderResponseError("Gemini response missing valid candidates list")

        first_cand = candidates[0]
        content_dict = first_cand.get("content", {})
        parts = content_dict.get("parts", [])

        text_pieces: list[str] = []
        thought_metadata: GeminiThoughtMetadata | None = None
        for p in parts:
            if "text" in p:
                text_pieces.append(p["text"])
            if "thought" in p:
                thought_metadata = GeminiThoughtMetadata(
                    thought=p["thought"],
                    signature=p.get("signature"),
                )
                _ = thought_metadata  # Preserved in parse

        tool_calls = extract_gemini_tool_calls(parts)
        finish_reason = first_cand.get("finishReason", "STOP").lower()

        usage: Usage | None = None
        if "usageMetadata" in data and isinstance(data["usageMetadata"], dict):
            um = data["usageMetadata"]
            cached_tokens = um.get("cachedContentTokenCount")
            usage = Usage(
                prompt_tokens=um.get("promptTokenCount"),
                completion_tokens=_billed_output_tokens(um),
                total_tokens=um.get("totalTokenCount"),
                reasoning_tokens=um.get("thoughtsTokenCount"),
                cached_prompt_tokens=cached_tokens,
                cache_read_input_tokens=cached_tokens,
                is_complete=True,
            )

        resp_content = "".join(text_pieces) if text_pieces else None
        msg = Message(
            role="assistant",
            content=resp_content,
            tool_calls=tool_calls if tool_calls else None,
        )

        return ModelResponse(
            message=msg,
            usage=usage,
            finish_reason=finish_reason,
        )

    def _stream_internal(
        self,
        request: GenerationRequest,
        context: ExecutionContext | None,
    ) -> AsyncIterator[StreamDelta]:
        """Stream chunks from Gemini via SSE protocol."""
        if request.provider_metadata.get("unsupported_feature"):
            feat = request.provider_metadata["unsupported_feature"]
            raise CapabilityError(
                f"Gemini does not support feature: {feat}",
                provider_name=self.model,
                capability=feat,
            )

        client = self._get_client()
        headers = self._build_headers()
        headers["Accept"] = "text/event-stream"
        url = f"{self.base_url}/models/{self.model}:streamGenerateContent?alt=sse"

        contents = to_gemini_contents(request.messages)
        tools = to_gemini_tools(request.tools)
        payload: dict[str, Any] = {"contents": contents}
        if tools:
            payload["tools"] = tools
            if request.tool_choice is not None:
                payload["toolConfig"] = gemini_tool_config(request.tool_choice)
        # Same generation options as generate(): sampling, limits, stop and schema.
        gen_config: dict[str, Any] = {}
        if request.temperature is not None:
            gen_config["temperature"] = request.temperature
        if request.max_tokens:
            gen_config["maxOutputTokens"] = request.max_tokens
        if request.stop_sequences:
            gen_config["stopSequences"] = request.stop_sequences
        if request.schema and self.capabilities.can_structured_output:
            gen_config["responseMimeType"] = "application/json"
            gen_config["responseSchema"] = to_gemini_schema(request.schema)
        apply_gemini_settings(payload, gen_config, request)

        req_timeout = request.timeout or self.timeout

        async def _generator() -> AsyncIterator[StreamDelta]:
            tool_call_count = 0
            saw_finish = False
            try:
                async with client.stream(
                    "POST",
                    url,
                    json=payload,
                    headers=headers,
                    timeout=req_timeout,
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
                            break

                        try:
                            chunk = json.loads(data_str)
                        except json.JSONDecodeError as exc:
                            raise ProviderResponseError(
                                "Malformed SSE chunk from Gemini", original_error=exc
                            ) from exc

                        if not isinstance(chunk, dict):
                            raise ProviderResponseError("Malformed SSE chunk from Gemini")
                        err = chunk.get("error")
                        if err:
                            err_obj = err if isinstance(err, dict) else {}
                            err_msg = err_obj.get("message") if err_obj else str(err)
                            code = err_obj.get("code")
                            text = f"Gemini stream error: {err_msg or 'unknown error'}"
                            if isinstance(code, int) and not isinstance(code, bool) and code >= 400:
                                raise http_status_error(code, text)
                            raise ProviderResponseError(text)

                        candidates = chunk.get("candidates") or []
                        for cand in candidates:
                            if not isinstance(cand, dict):
                                raise ProviderResponseError("Malformed candidate from Gemini")
                            content = cand.get("content") or {}
                            parts = content.get("parts") or [] if isinstance(content, dict) else []
                            for p in parts:
                                if not isinstance(p, dict):
                                    raise ProviderResponseError("Malformed part from Gemini")
                                if "text" in p:
                                    yield StreamDelta(kind="text_delta", text=p["text"])
                                if "thought" in p:
                                    yield StreamDelta(
                                        kind="continuation_delta",
                                        continuation=ContinuationPart(
                                            token=p.get("signature", "thought_sig"),
                                            provider_metadata={"thought": p["thought"]},
                                        ),
                                    )
                                if "functionCall" in p:
                                    fn = p["functionCall"]
                                    fn_args = fn.get("args", {})
                                    args_str = (
                                        json.dumps(fn_args)
                                        if isinstance(fn_args, dict)
                                        else str(fn_args)
                                    )
                                    call_idx = tool_call_count
                                    tool_call_count += 1
                                    yield StreamDelta(
                                        kind="tool_call_delta",
                                        tool_call_index=call_idx,
                                        tool_call_id=f"call_{fn.get('name', 'tool')}_{call_idx}",
                                        tool_name=fn.get("name"),
                                        tool_arguments_delta=args_str,
                                    )

                            finish_reason = cand.get("finishReason")
                            if finish_reason:
                                saw_finish = True
                                yield StreamDelta(
                                    kind="finish",
                                    finish_reason=finish_reason.lower(),
                                )

                        if "usageMetadata" in chunk and isinstance(chunk["usageMetadata"], dict):
                            um = chunk["usageMetadata"]
                            cached_tokens = um.get("cachedContentTokenCount")
                            usage = Usage(
                                prompt_tokens=um.get("promptTokenCount"),
                                completion_tokens=_billed_output_tokens(um),
                                total_tokens=um.get("totalTokenCount"),
                                reasoning_tokens=um.get("thoughtsTokenCount"),
                                cached_prompt_tokens=cached_tokens,
                                cache_read_input_tokens=cached_tokens,
                                is_complete=True,
                            )
                            yield StreamDelta(kind="finish", usage=usage)
                if not saw_finish:
                    raise ProviderResponseError(
                        "Gemini stream ended without a finishReason (truncated stream)"
                    )
            except httpx.TimeoutException as exc:
                raise ProviderRequestError(
                    f"Gemini streaming timed out ({req_timeout}s)", original_error=exc
                ) from exc
            except httpx.RequestError as exc:
                raise ProviderRequestError(
                    f"Network error during Gemini streaming: {exc}", original_error=exc
                ) from exc

        return _generator()

    async def _embed_internal(
        self,
        texts: list[str],
        context: ExecutionContext | None,
    ) -> EmbeddingResult:
        """Generate vector embeddings using Gemini embedContent API."""
        client = self._get_client()
        headers = self._build_headers()
        url = f"{self.base_url}/models/{self.model}:batchEmbedContents"

        requests = [
            {"model": f"models/{self.model}", "content": {"parts": [{"text": t}]}} for t in texts
        ]
        payload = {"requests": requests}

        try:
            response = await client.post(
                url,
                json=payload,
                headers=headers,
                timeout=self.timeout,
            )
        except httpx.RequestError as exc:
            raise ProviderRequestError(
                f"Network error during Gemini embedding: {exc}", original_error=exc
            ) from exc

        self._handle_error_response(response)

        try:
            data = response.json()
        except Exception as exc:
            raise ProviderResponseError(
                "Malformed embedding response from Gemini", original_error=exc
            ) from exc

        raw_embeddings = data.get("embeddings", [])
        vectors = [item.get("values", []) for item in raw_embeddings]
        # batchEmbedContents reports no token counts; leave usage unknown, never estimated.
        usage = Usage(is_complete=False)

        return EmbeddingResult(
            embeddings=vectors,
            model=self.model,
            usage=usage,
        )

    async def delete_cached_content(self, cached_content_id: str) -> bool:
        """Explicitly delete a stored cached content resource.

        Raises CacheCleanupError on provider deletion failure.
        """
        from nexura_agents.providers.cache.errors import CacheCleanupError

        client = self._get_client()
        clean_id = cached_content_id.lstrip("/")
        url = f"{self.base_url}/{clean_id}"
        headers = self._build_headers()

        try:
            resp = await client.delete(url, headers=headers, timeout=self.timeout)
            if resp.status_code >= 400:
                raise CacheCleanupError(
                    f"Gemini failed to delete cached content '{cached_content_id}': "
                    f"HTTP {resp.status_code}",
                    cache_id=cached_content_id,
                )
            return True
        except Exception as exc:
            if isinstance(exc, CacheCleanupError):
                raise
            raise CacheCleanupError(
                f"Network or client error deleting Gemini cached content "
                f"'{cached_content_id}': {exc}",
                cache_id=cached_content_id,
                original_error=exc,
            ) from exc

    def __repr__(self) -> str:
        key_repr = "'***'" if self._api_key else "None"
        return (
            f"GeminiNativeProvider(model={self.model!r}, "
            f"api_key={key_repr}, timeout={self.timeout})"
        )
