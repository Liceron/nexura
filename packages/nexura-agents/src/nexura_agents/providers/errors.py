# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Typed errors for model provider adapters and streaming protocols."""

from __future__ import annotations

import contextlib
import math
import re
from typing import Any

from nexura_agents.contracts.errors import ContractError, ContractErrorCode
from nexura_agents.errors import ProviderError as _SDKProviderError
from nexura_agents.errors import ProviderRateLimitError

_SECRET_PATTERNS = [
    re.compile(r"sk-[a-zA-Z0-9_-]{20,}", re.IGNORECASE),
    re.compile(r"bearer\s+[a-zA-Z0-9_\-\.]{20,}", re.IGNORECASE),
    re.compile(r"key-[a-zA-Z0-9_-]{20,}", re.IGNORECASE),
]


def _mask_secrets(text: str) -> str:
    """Mask known credential tokens in string output."""
    masked = text
    for pat in _SECRET_PATTERNS:
        masked = pat.sub("[REDACTED_SECRET]", masked)
    return masked


class ProviderError(ContractError):
    """Base error for all provider and streaming exceptions."""

    def __init__(
        self,
        message: str,
        *,
        code: ContractErrorCode | str = ContractErrorCode.UPSTREAM_FAILURE,
        details: dict[str, Any] | None = None,
        original_error: BaseException | None = None,
    ) -> None:
        safe_msg = _mask_secrets(message)
        safe_details = details or {}
        # Mask string values in details
        sanitized_details = {
            k: (_mask_secrets(v) if isinstance(v, str) else v) for k, v in safe_details.items()
        }
        super().__init__(
            safe_msg,
            code=code,
            details=sanitized_details,
            original_error=original_error,
        )


class CapabilityError(ProviderError):
    """Raised when a caller requests a capability unsupported by the provider."""

    def __init__(
        self,
        message: str,
        *,
        provider_name: str,
        capability: str,
    ) -> None:
        super().__init__(
            message,
            code=ContractErrorCode.CAPABILITY_UNSUPPORTED,
            details={"provider": provider_name, "capability": capability},
        )


class StreamCancelledError(ProviderError):
    """Raised when an ongoing token or delta stream is cancelled or disconnected."""

    def __init__(
        self,
        message: str = "Stream was cancelled or disconnected cleanly.",
        *,
        stream_id: str | None = None,
    ) -> None:
        super().__init__(
            message,
            code=ContractErrorCode.EXECUTION_CANCELLED,
            details={"stream_id": stream_id} if stream_id else {},
        )


class MalformedToolCallArgumentsError(ProviderError):
    """Raised when streamed or returned tool call arguments fail syntax or schema validation."""

    def __init__(
        self,
        tool_name: str,
        tool_call_id: str,
        raw_arguments: str,
        reason: str,
    ) -> None:
        super().__init__(
            f"Malformed tool call arguments for '{tool_name}' ({tool_call_id}): {reason}",
            code=ContractErrorCode.INVALID_SCHEMA,
            details={
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "raw_arguments": raw_arguments,
                "reason": reason,
            },
        )


class ProviderTimeoutError(ProviderError):
    """Raised when provider invocation exceeds specified deadline or timeout."""

    def __init__(self, provider_name: str, timeout_seconds: float) -> None:
        super().__init__(
            f"Provider '{provider_name}' timed out after {timeout_seconds}s.",
            code=ContractErrorCode.DEADLINE_EXCEEDED,
            details={"provider": provider_name, "timeout_seconds": timeout_seconds},
        )


class ProviderAuthError(ProviderError):
    """Raised when provider authentication fails, ensuring credentials are masked."""

    def __init__(self, provider_name: str, raw_error: str) -> None:
        super().__init__(
            f"Authentication failed for provider '{provider_name}': {_mask_secrets(raw_error)}",
            code=ContractErrorCode.AUTHENTICATION_FAILED,
            details={"provider": provider_name},
        )


class ProviderServerError(_SDKProviderError):
    """Raised for HTTP 5xx responses; transient, so ``RetryModel`` retries it."""


class ProviderClientError(_SDKProviderError):
    """Raised for non-auth, non-429 HTTP 4xx responses; the request itself is wrong.

    Never retried: sending the same request again fails the same way.
    """


class ProviderContextLengthError(ProviderClientError):
    """A 4xx saying the request is longer than the model's context window.

    Not transient: sending the same messages fails again. ``RetryModel(trim_history=N)`` can
    recover by dropping the oldest conversation turns and retrying.
    """


# Wording providers use when the prompt exceeds the context window (OpenAI/Azure, Anthropic,
# Gemini/Vertex, Mistral, vLLM, LiteLLM). Matched case-insensitively. Deliberately specific:
# a generic phrase such as "context window" also appears in echoed user text and in unrelated
# validation errors, and a false match would make ``RetryModel(trim_history=...)`` drop turns.
_CONTEXT_LENGTH_PHRASES = (
    "context_length_exceeded",
    "maximum context length",
    "prompt is too long",
    "input is too long for requested model",
    "exceeds the maximum number of tokens",
    "input token count",
    "reduce the length of the messages",
    "exceeds the model's context window",
    "exceeds the context window",
    "decrease input length",
    "exceed context limit",  # Anthropic newer wording
    "maximum prompt length",  # xAI
    "exceeds the available context size",  # llama.cpp server / Ollama OpenAI-compat
)


def is_context_length_message(message: str) -> bool:
    """True when a provider error message says the prompt is longer than the context window."""
    text = message.lower()
    return any(phrase in text for phrase in _CONTEXT_LENGTH_PHRASES)


class ProviderRateLimitedError(ProviderRateLimitError):
    """A 429 that carries the server's ``Retry-After`` (seconds) when it sent one."""

    def __init__(
        self,
        message: str,
        *,
        retry_after: float | None = None,
        original_error: BaseException | None = None,
    ) -> None:
        super().__init__(message, original_error=original_error)
        self.retry_after = retry_after


def parse_retry_after(value: str | None) -> float | None:
    """Parse a ``Retry-After`` seconds header; ``None`` when absent or not a number.

    ``"0"`` is a valid value (retry now) and is returned as ``0.0``. HTTP-date values
    are not supported and are treated as absent.
    """
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


async def read_error_body(response: Any) -> None:
    """Read the body of an unread streamed error response so it can be classified.

    Errors are swallowed: the status code alone still yields a typed error.
    """
    if getattr(response, "status_code", 0) < 400:
        return
    with contextlib.suppress(Exception):
        await response.aread()


def safe_response_text(response: Any) -> str:
    """``response.text`` or ``""`` when the body was never read (never ``ResponseNotRead``)."""
    try:
        text = response.text
    except Exception:
        return ""
    return text if isinstance(text, str) else ""


def response_excerpt(response: Any, limit: int = 2048) -> str:
    """The start of an error response body, for classification only (never shown)."""
    try:
        text = response.text
    except Exception:  # streamed responses that were not read
        return ""
    return text[:limit] if isinstance(text, str) else ""


def http_status_error(
    status_code: int,
    message: str,
    *,
    retry_after: str | None = None,
    original_error: BaseException | None = None,
    detail: str | None = None,
) -> _SDKProviderError:
    """Build the typed error for a failed HTTP status other than authentication.

    429 becomes a rate-limit error carrying ``retry_after``, 5xx a
    :class:`ProviderServerError`, a 4xx about the context window a
    :class:`ProviderContextLengthError`, and other 4xx a :class:`ProviderClientError`.
    ``detail`` (for example the start of the response body) is used only to recognise a
    context-window error; it never appears in the message.
    """
    if status_code == 429:
        seconds = parse_retry_after(retry_after)
        detail = f" (Retry-After: {seconds:g}s)" if seconds is not None else ""
        return ProviderRateLimitedError(
            message + detail, retry_after=seconds, original_error=original_error
        )
    if status_code >= 500:
        return ProviderServerError(message, original_error=original_error)
    if status_code in (400, 413, 422) and is_context_length_message(f"{message} {detail or ''}"):
        if not is_context_length_message(message):
            message += " (the prompt exceeds the model's context window)"
        return ProviderContextLengthError(message, original_error=original_error)
    return ProviderClientError(message, original_error=original_error)
