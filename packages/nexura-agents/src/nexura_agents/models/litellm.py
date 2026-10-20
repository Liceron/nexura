# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""LiteLLM adapter: one model class that reaches 100+ providers.

Requires the optional extra: ``pip install "nexura-agents[litellm]"``. LiteLLM is
imported lazily on the first call, so importing this module never imports litellm.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from nexura_agents.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderRequestError,
    ProviderResponseError,
    SDKValidationError,
)
from nexura_agents.json_schema import openai_response_format
from nexura_agents.limits import ModelSettings, merge_extra, settings_from_request
from nexura_agents.models._streaming import ChatChunkDecoder
from nexura_agents.models.base import ModelProtocol
from nexura_agents.models.openai import OpenAIChatModel
from nexura_agents.providers.errors import (
    ProviderContextLengthError,
    _mask_secrets,
    http_status_error,
)
from nexura_agents.providers.types import GenerationRequest, ProviderCapabilities, StreamDelta
from nexura_agents.types import Message, ModelResponse, ToolCall, Usage

CompletionFn = Callable[..., Awaitable[Any]]

_INSTALL_HINT = (
    "LiteLLMModel needs the optional 'litellm' package. "
    'Install it with: pip install "nexura-agents[litellm]"'
)


def _as_dict(obj: Any) -> Any:
    """Plain dict view of a LiteLLM ``ModelResponse`` (pydantic) or a dict."""
    if isinstance(obj, dict):
        return obj
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        return dump()
    to_dict = getattr(obj, "dict", None)
    if callable(to_dict):
        return to_dict()
    return obj


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _usage_from_litellm(u: dict[str, Any]) -> Usage:
    comp_details = _as_dict(u.get("completion_tokens_details")) or {}
    prompt_details = _as_dict(u.get("prompt_tokens_details")) or {}
    return Usage(
        prompt_tokens=_int_or_none(u.get("prompt_tokens")),
        completion_tokens=_int_or_none(u.get("completion_tokens")),
        total_tokens=_int_or_none(u.get("total_tokens")),
        reasoning_tokens=_int_or_none(comp_details.get("reasoning_tokens"))
        if isinstance(comp_details, dict)
        else None,
        cached_prompt_tokens=_int_or_none(prompt_details.get("cached_tokens"))
        if isinstance(prompt_details, dict)
        else None,
        cache_creation_input_tokens=_int_or_none(u.get("cache_creation_input_tokens")),
        cache_read_input_tokens=_int_or_none(u.get("cache_read_input_tokens")),
    )


def _header(headers: Any, name: str) -> str | None:
    """Case-insensitive header lookup that works for httpx headers and plain dicts."""
    if headers is None:
        return None
    try:
        items = list(headers.items())
    except Exception:
        return None
    for key, value in items:
        if isinstance(key, str) and key.lower() == name:
            return str(value)
    return None


def _status_of(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    return status if isinstance(status, int) and not isinstance(status, bool) else None


def _class_names(exc: BaseException) -> set[str]:
    return {c.__name__ for c in type(exc).__mro__}


def _redact(text: str, api_key: str | None) -> str:
    """Mask known secret patterns plus the caller's own api_key, whatever its format."""
    if api_key:
        text = text.replace(api_key, "[REDACTED_SECRET]")
    return _mask_secrets(text)


def map_litellm_exception(
    exc: BaseException, *, timeout: float | None = None, api_key: str | None = None
) -> BaseException:
    """Translate a LiteLLM/OpenAI-style exception into the SDK's provider errors.

    Matching is by ``status_code`` and class name, not ``isinstance`` on litellm
    classes, so it works without litellm installed. Unrecognised exceptions are
    returned unchanged.
    """
    if isinstance(exc, ProviderError):
        return exc
    message = _redact(str(exc) or type(exc).__name__, api_key)
    # Never keep the raw exception: it may carry the key in its args/message.
    safe_original = RuntimeError(f"{type(exc).__name__}: {message}")
    names = _class_names(exc)
    if isinstance(exc, TimeoutError) or any("Timeout" in n for n in names):
        limit = f" ({timeout}s)" if timeout is not None else ""
        return ProviderRequestError(
            f"LiteLLM request timed out{limit}: {message}", original_error=safe_original
        )
    status = _status_of(exc)
    if "ContextWindowExceededError" in names and (status is None or status in (400, 413, 422)):
        return ProviderContextLengthError(
            f"Provider returned error HTTP {status or 400}: {message}"
            " (the prompt exceeds the model's context window)",
            original_error=safe_original,
        )
    if status in (401, 403):
        return ProviderAuthError(f"Provider authentication failed (HTTP {status}): {message}")
    if status is not None and status >= 400:
        response = getattr(exc, "response", None)
        retry_after = _header(getattr(response, "headers", None), "retry-after")
        return http_status_error(
            status,
            f"Provider returned error HTTP {status}: {message}",
            retry_after=retry_after,
            original_error=safe_original,
        )
    if "ServiceUnavailableError" in names or "InternalServerError" in names:
        return http_status_error(
            503, f"Provider unavailable: {message}", original_error=safe_original
        )
    if any("ConnectionError" in n or n == "ConnectError" for n in names) or isinstance(
        exc, ConnectionError
    ):
        return ProviderRequestError(
            f"Network error contacting provider: {message}", original_error=safe_original
        )
    return exc


class LiteLLMModel(ModelProtocol):
    """Adapter that sends chat requests through ``litellm.acompletion``.

    ``model`` is any LiteLLM model string, e.g. ``"bedrock/anthropic.claude-3-5-sonnet"``,
    ``"azure/my-deployment"``, ``"ollama/llama3.1"`` or ``"groq/llama-3.1-8b-instant"``.
    ``api_key``, ``api_base`` and ``timeout`` are passed per call; extra ``**options``
    (``temperature``, ``max_tokens``, ...) are forwarded unchanged. ``completion`` can be
    any async callable with ``litellm.acompletion``'s signature (used by tests).

    Retries are owned by the SDK: every call passes ``num_retries=0`` and
    ``max_retries=0`` so ``RetryModel``/``FallbackModel`` decide, not LiteLLM. The
    adapter never mutates ``os.environ`` or LiteLLM globals. Set
    ``LITELLM_LOCAL_MODEL_COST_MAP=True`` in your environment *before* the first
    import of litellm to stop it fetching its model-cost map over HTTP at import.

    ``generate()`` returns whole responses. Token streaming is opt-in through
    :meth:`stream`, which ``Runner.stream_text`` / ``stream_async(stream_text=True)`` use
    (not for structured-output turns): it calls ``litellm.acompletion`` with ``stream=True``
    and ``stream_options={"include_usage": True}`` and yields ``StreamDelta`` values.
    Users still cannot pass ``stream=True`` in ``**options``.
    """

    # response_format json_schema is passed through; LiteLLM maps it per backend. Tools and a
    # schema are not combined because backends differ.
    supports_response_schema = True
    streams_response_schema = True
    response_schema_with_tools = False
    capabilities = ProviderCapabilities(
        can_stream=True, can_tools=True, can_structured_output=True, reports_usage=True
    )

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        api_base: str | None = None,
        timeout: float | None = None,
        completion: CompletionFn | None = None,
        **options: Any,
    ) -> None:
        self.model = model
        self._api_key = api_key
        self.api_base = api_base
        self.timeout = timeout
        self._completion = completion
        if options.get("stream"):
            raise SDKValidationError(
                "LiteLLMModel does not accept stream=True in options; "
                "use Runner.stream_text for token streaming"
            )
        self.options = options
        # Message translation is shared with OpenAIChatModel (no client is created).
        self._formatter = OpenAIChatModel(model)

    def _resolve_completion(self) -> CompletionFn:
        if self._completion is not None:
            return self._completion
        try:
            import litellm  # type: ignore[import-not-found,unused-ignore]
        except ImportError as exc:
            raise ImportError(_INSTALL_HINT) from exc
        fn: CompletionFn = litellm.acompletion
        return fn

    async def generate(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        timeout: float | None = None,
        response_schema: dict[str, Any] | None = None,
        model_settings: ModelSettings | None = None,
    ) -> ModelResponse:
        """Send a chat completion through LiteLLM and map the response."""
        completion = self._resolve_completion()
        request_timeout = timeout if timeout is not None else self.timeout
        kwargs = self._build_kwargs(
            messages, tools, request_timeout, response_schema, model_settings
        )

        mapped: BaseException | None = None
        try:
            raw = await completion(**kwargs)
        except Exception as exc:
            mapped = map_litellm_exception(exc, timeout=request_timeout, api_key=self._api_key)
            if mapped is exc:
                raise
        if mapped is not None:
            # Raised outside the handler so the unredacted exception is not chained.
            raise mapped from None
        if hasattr(raw, "__aiter__") and not isinstance(raw, dict):
            aclose = getattr(raw, "aclose", None)
            if callable(aclose):
                await aclose()
            raise ProviderResponseError(
                "LiteLLM returned a stream; streaming is not supported by LiteLLMModel"
            )
        return self._parse(raw)

    def _build_kwargs(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None,
        request_timeout: float | None,
        response_schema: dict[str, Any] | None,
        model_settings: ModelSettings | None = None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = dict(self.options)
        # Runner/agent settings override the constructor options for this call.
        if model_settings is not None:
            kwargs.update(model_settings.to_openai_params())
            if model_settings.thinking_budget_tokens is not None:
                # LiteLLM maps this to each provider's thinking option.
                kwargs["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": model_settings.thinking_budget_tokens,
                }
            if model_settings.extra:
                merge_extra(kwargs, model_settings.extra)
        kwargs.update(
            model=self.model,
            messages=[self._formatter._format_message(m) for m in messages],
            num_retries=0,
            max_retries=0,
        )
        if tools:
            kwargs["tools"] = tools
        if response_schema is not None:
            kwargs["response_format"] = openai_response_format(response_schema)
        if self._api_key is not None:
            kwargs["api_key"] = self._api_key
        if self.api_base is not None:
            kwargs["api_base"] = self.api_base
        if request_timeout is not None:
            kwargs["timeout"] = request_timeout
        return kwargs

    async def stream(
        self,
        request: GenerationRequest | list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        timeout: float | None = None,
        response_schema: dict[str, Any] | None = None,
        model_settings: ModelSettings | None = None,
    ) -> AsyncIterator[StreamDelta]:
        """Stream a chat completion through LiteLLM as ``StreamDelta`` values.

        Uses ``stream=True`` plus ``stream_options.include_usage``; retries stay at zero.
        Errors, including those raised mid-stream, map exactly as in :meth:`generate`, and
        a stream that ends early raises instead of returning partial text. Closing the
        iterator (cancellation) closes LiteLLM's stream wrapper.
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
        completion = self._resolve_completion()
        request_timeout = timeout if timeout is not None else self.timeout
        kwargs = self._build_kwargs(
            messages, tools, request_timeout, response_schema, model_settings
        )
        kwargs["stream"] = True
        stream_options = kwargs.get("stream_options")
        kwargs["stream_options"] = {
            **(stream_options if isinstance(stream_options, dict) else {}),
            "include_usage": True,
        }

        headers = self.options.get("extra_headers")
        decoder = ChatChunkDecoder(
            _usage_from_litellm,
            api_key=self._api_key,
            # Header values (gateway keys) must never be echoed by an in-stream error.
            secrets=[v for v in headers.values() if isinstance(v, str)]
            if isinstance(headers, dict)
            else (),
        )
        mapped: BaseException | None = None
        raw: Any = None
        try:
            raw = await completion(**kwargs)
            if not hasattr(raw, "__aiter__") or isinstance(raw, dict):
                raise ProviderResponseError("LiteLLM did not return a stream for a streaming call")
            async for chunk in raw:
                for delta in decoder.feed(_as_dict(chunk)):
                    yield delta
            decoder.finish()
        except Exception as exc:
            mapped = map_litellm_exception(exc, timeout=request_timeout, api_key=self._api_key)
            if mapped is exc:
                raise
        finally:
            aclose = getattr(raw, "aclose", None)
            if callable(aclose):
                closed = aclose()
                if inspect.isawaitable(closed):
                    await closed
        if mapped is not None:
            # Raised outside the handler so the unredacted exception is not chained.
            raise mapped from None

    @staticmethod
    def _parse(raw: Any) -> ModelResponse:
        data = _as_dict(raw)
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("choices"), list)
            or not data["choices"]
        ):
            raise ProviderResponseError("LiteLLM response requires a nonempty choices list")
        choice = _as_dict(data["choices"][0])
        msg = _as_dict(choice.get("message")) if isinstance(choice, dict) else None
        if not isinstance(msg, dict):
            raise ProviderResponseError("LiteLLM choice requires a message object")
        content = msg.get("content")
        raw_tool_calls = msg.get("tool_calls")
        finish_reason = choice.get("finish_reason")
        if content is not None and not isinstance(content, str):
            raise ProviderResponseError("Provider message content must be text or null")
        if raw_tool_calls is not None and not isinstance(raw_tool_calls, list):
            raise ProviderResponseError("Provider tool_calls must be a list or null")
        if finish_reason is not None and not isinstance(finish_reason, str):
            finish_reason = None
        if content is None and not raw_tool_calls:
            raise ProviderResponseError("Provider message has neither content nor tool calls")

        tool_calls: list[ToolCall] | None = None
        if raw_tool_calls:
            tool_calls = []
            seen: set[str] = set()
            for item in raw_tool_calls:
                tc = _as_dict(item)
                fn = _as_dict(tc.get("function")) if isinstance(tc, dict) else None
                if not isinstance(tc, dict) or not isinstance(fn, dict):
                    raise ProviderResponseError("Malformed tool_call item in response")
                tc_id, name = tc.get("id"), fn.get("name")
                if not isinstance(tc_id, str) or not tc_id.strip() or tc_id in seen:
                    raise ProviderResponseError("Tool call IDs must be nonempty and unique")
                seen.add(tc_id)
                if not isinstance(name, str) or not name.strip():
                    raise ProviderResponseError("Tool call name must be nonempty text")
                raw_args = fn.get("arguments", "{}")
                if raw_args is None or raw_args == "":
                    raw_args = "{}"
                try:
                    parsed = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    if not isinstance(parsed, dict):
                        raise ValueError(f"Arguments must decode to a dict, got {type(parsed)}")
                except Exception as exc:
                    raise ProviderResponseError(
                        "Malformed tool call JSON arguments", original_error=exc
                    ) from exc
                tool_calls.append(
                    ToolCall(
                        id=tc_id,
                        name=name,
                        arguments=parsed,
                        raw_arguments=raw_args
                        if isinstance(raw_args, str)
                        else json.dumps(raw_args, ensure_ascii=False),
                    )
                )

        u = _as_dict(data.get("usage")) if data.get("usage") is not None else None
        usage = _usage_from_litellm(u) if isinstance(u, dict) else None

        return ModelResponse(
            message=Message(role="assistant", content=content, tool_calls=tool_calls),
            usage=usage,
            finish_reason=finish_reason,
        )

    def __repr__(self) -> str:
        key_repr = "'***'" if self._api_key else "None"
        return (
            f"LiteLLMModel(model={self.model!r}, api_base={self.api_base!r}, "
            f"api_key={key_repr}, timeout={self.timeout})"
        )
