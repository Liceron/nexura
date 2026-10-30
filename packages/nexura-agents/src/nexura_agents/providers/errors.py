# SPDX-License-Identifier: Apache-2.0
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
    """Raised for HTTP 5xx responses; transient, so ``RetryModel`` retries it.

    ``retry_after`` holds the server's ``Retry-After`` seconds when it sent one (OpenAI 503
    ``server_is_overloaded`` and Anthropic 529 ``overloaded_error`` may), else ``None``.
    """

    def __init__(
        self,
        message: str,
        *,
        retry_after: float | None = None,
        original_error: BaseException | None = None,
    ) -> None:
        super().__init__(message, original_error=original_error)
        self.retry_after = retry_after


class ProviderClientError(_SDKProviderError):
    """Raised for non-auth, non-429 HTTP 4xx responses; the request itself is wrong.

    Never retried: sending the same request again fails the same way.
    """


class ProviderContentFilterError(ProviderClientError):
    """The provider blocked the prompt or the answer with its content or safety filter.

    Raised for a blocked prompt (Gemini ``promptFeedback.blockReason``, an HTTP 400 with code
    ``content_filter``) and for an answer that was withheld with nothing returned (OpenAI
    ``finish_reason: "content_filter"`` without content, Gemini ``SAFETY``-type finish
    reasons without parts). Not retried: the same input is blocked again. ``reason`` is the
    provider's own code, for example ``"SAFETY"`` or ``"content_filter"``.
    """

    def __init__(
        self,
        message: str,
        *,
        reason: str | None = None,
        original_error: BaseException | None = None,
    ) -> None:
        super().__init__(message, original_error=original_error)
        self.reason = reason


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


class ProviderQuotaExceededError(ProviderRateLimitedError):
    """A 429 for an exhausted quota, credit balance or spend limit, not a short throttle.

    It is still a :class:`ProviderRateLimitError` (callers that catch 429s keep working), but
    ``retryable`` is ``False``: OpenAI's rate-limit guide says not to retry quota and billing
    errors, and a Gemini per-day quota only resets the next day. ``RetryModel`` raises it at
    once; ``FallbackModel`` still moves on to the next model. ``code`` is the provider's code,
    for example ``"insufficient_quota"`` or a Gemini ``quotaId``.
    """

    retryable = False

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        retry_after: float | None = None,
        original_error: BaseException | None = None,
    ) -> None:
        super().__init__(message, retry_after=retry_after, original_error=original_error)
        self.code = code


# 429 codes that need the account owner to act (OpenAI error-codes guide, Sarvam ErrorDetails,
# OpenAI-compatible servers). Matched case-insensitively against the error ``code``/``type``.
QUOTA_ERROR_CODES = frozenset(
    {
        "insufficient_quota",
        "insufficient_quota_error",
        "credit_balance_exhausted",
        "organization_spend_limit_exceeded",
        "project_spend_limit_exceeded",
        "organization_usage_limit_exceeded",
        "billing_hard_limit_reached",
        "quota_exceeded",
    }
)

# Error codes meaning the input or output was blocked by a content filter (OpenAI/Azure
# ``content_filter``, Gemini block reasons).
CONTENT_FILTER_CODES = frozenset({"content_filter", "content_policy_violation"})


def error_body_codes(response: Any) -> list[str]:
    """Machine-readable codes in a JSON error body: ``error.code``, ``error.type``,
    ``error.status`` and Google ``details[].reason`` / ``details[].violations[].quotaId``.

    Never the free-text message. Empty when the body is not JSON or was never read.
    """
    try:
        data = response.json()
    except Exception:
        return []
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, dict):
        return []
    codes: list[str] = []
    for key in ("code", "type", "status"):
        value = error.get(key)
        if isinstance(value, str) and value:
            codes.append(value)
    for detail in error.get("details") or []:
        if not isinstance(detail, dict):
            continue
        reason = detail.get("reason")
        if isinstance(reason, str) and reason:
            codes.append(reason)
        for violation in detail.get("violations") or []:
            if isinstance(violation, dict) and isinstance(violation.get("quotaId"), str):
                codes.append(violation["quotaId"])
    return codes


def google_retry_delay(response: Any) -> float | None:
    """Seconds from a Google ``google.rpc.RetryInfo`` detail (``"retryDelay": "37s"``).

    Gemini sends its 429 wait time in the JSON body, not in a ``Retry-After`` header.
    """
    try:
        data = response.json()
    except Exception:
        return None
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, dict):
        return None
    for detail in error.get("details") or []:
        if not isinstance(detail, dict):
            continue
        if not str(detail.get("@type", "")).endswith("google.rpc.RetryInfo"):
            continue
        delay = detail.get("retryDelay")
        if isinstance(delay, str) and delay.endswith("s"):
            return parse_retry_after(delay[:-1])
    return None


def is_quota_code(codes: list[str]) -> bool:
    """True when a 429's codes say a quota or spend limit is exhausted (not a throttle)."""
    for code in codes:
        lowered = code.lower()
        if lowered in QUOTA_ERROR_CODES or "perday" in lowered:
            return True
    return False


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
    codes: list[str] | None = None,
    retry_after_seconds: float | None = None,
) -> _SDKProviderError:
    """Build the typed error for a failed HTTP status other than authentication.

    429 becomes a rate-limit error carrying ``retry_after`` (a
    :class:`ProviderQuotaExceededError`, not retried, when ``codes`` name an exhausted quota),
    5xx a :class:`ProviderServerError` (also carrying ``retry_after``), a 4xx about the context
    window a :class:`ProviderContextLengthError`, a 4xx whose ``codes`` name a content filter a
    :class:`ProviderContentFilterError`, and other 4xx a :class:`ProviderClientError`.
    ``detail`` (for example the start of the response body) is used only to recognise a
    context-window error; it never appears in the message. ``codes`` are the body's
    machine-readable error codes (see :func:`error_body_codes`). ``retry_after_seconds`` is a
    delay the caller already parsed (for example Google's ``RetryInfo``); the header wins.
    """
    seconds = parse_retry_after(retry_after)
    if seconds is None:
        seconds = retry_after_seconds
    if status_code == 429:
        suffix = f" (Retry-After: {seconds:g}s)" if seconds is not None else ""
        if codes and is_quota_code(codes):
            code = next(c for c in codes if is_quota_code([c]))
            return ProviderQuotaExceededError(
                f"{message} [{code}: quota or spend limit exhausted; not retried]{suffix}",
                code=code,
                retry_after=seconds,
                original_error=original_error,
            )
        return ProviderRateLimitedError(
            message + suffix, retry_after=seconds, original_error=original_error
        )
    if status_code >= 500:
        return ProviderServerError(message, retry_after=seconds, original_error=original_error)
    if codes and any(c.lower() in CONTENT_FILTER_CODES for c in codes):
        reason = next(c for c in codes if c.lower() in CONTENT_FILTER_CODES)
        return ProviderContentFilterError(
            f"{message} (blocked by the provider's content filter)",
            reason=reason,
            original_error=original_error,
        )
    if status_code in (400, 413, 422) and is_context_length_message(f"{message} {detail or ''}"):
        if not is_context_length_message(message):
            message += " (the prompt exceeds the model's context window)"
        return ProviderContextLengthError(message, original_error=original_error)
    return ProviderClientError(message, original_error=original_error)
