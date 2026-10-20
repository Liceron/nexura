# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Model wrappers for retry with backoff and fallback across providers.

Both wrap anything with ``generate(messages, tools, *, timeout)`` and can be nested,
e.g. ``FallbackModel(RetryModel(primary), RetryModel(secondary))``.
"""

from __future__ import annotations

import inspect
import random
from collections.abc import AsyncIterator, Sequence
from typing import TYPE_CHECKING, Any

from nexura_agents._lazy import LazyModule
from nexura_agents.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderRequestError,
    ProviderResponseError,
    SDKValidationError,
)
from nexura_agents.models.base import ModelProtocol
from nexura_agents.providers.errors import ProviderContextLengthError, ProviderServerError
from nexura_agents.types import Message, ModelResponse

if TYPE_CHECKING:
    import asyncio
else:
    asyncio = LazyModule("asyncio")

# Transient failures worth retrying or failing over: throttling (429), network, 5xx
# (ProviderServerError) and malformed responses. Other 4xx (ProviderClientError) and
# authentication errors are not transient.
TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    ProviderRateLimitError,
    ProviderRequestError,
    ProviderResponseError,
    ProviderServerError,
    TimeoutError,
)


def _stream_kwargs(stream_fn: Any, model_settings: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Forward ``model_settings`` only to a ``stream`` that declares it."""
    if model_settings is not None:
        try:
            takes = "model_settings" in inspect.signature(stream_fn).parameters
        except (TypeError, ValueError):
            takes = False
        if takes:
            return {**kwargs, "model_settings": model_settings}
    return kwargs


async def _aclose(stream: Any) -> None:
    aclose = getattr(stream, "aclose", None)
    if aclose is not None:
        await aclose()


def trim_oldest_turn(messages: list[Message]) -> list[Message] | None:
    """Drop the oldest whole turn after the leading system messages, or None if none can go.

    A turn is a user message with everything up to the next user message, so an assistant
    tool call is never separated from its tool results. The latest turn is always kept.
    """
    start = 0
    while start < len(messages) and messages[start].role == "system":
        start += 1
    user_positions = [i for i in range(start, len(messages)) if messages[i].role == "user"]
    if len(user_positions) < 2:
        return None
    second = user_positions[1]
    # System messages inside the dropped turn are kept (a policy added mid-conversation).
    kept_system = [m for m in messages[start:second] if m.role == "system"]
    return messages[:start] + kept_system + messages[second:]


def _accepts_run_context(model: object) -> bool:
    """True when ``generate`` takes ``context=`` and a ``GenerationRequest`` (providers)."""
    from nexura_agents.providers.base import BaseProvider

    return isinstance(model, BaseProvider) or bool(getattr(model, "accepts_run_context", False))


def _accepts_model_settings(model: object) -> bool:
    """True when a legacy model's ``generate`` takes ``model_settings=``."""
    flag = getattr(model, "accepts_model_settings", None)
    if isinstance(flag, bool):
        return flag
    try:
        return "model_settings" in inspect.signature(model.generate).parameters  # type: ignore[attr-defined]
    except (AttributeError, TypeError, ValueError):
        return False


class RetryModel:
    """Retry transient provider failures with exponential backoff and jitter.

    Authentication and request-validation errors are never retried. Retrying a model
    call repeats its token cost; set ``max_attempts`` with that in mind.

    ``trim_history=N`` (default 0, off) recovers from a :class:`ProviderContextLengthError`
    by dropping the oldest conversation turn and calling again, up to ``N`` times. System
    messages and the latest turn are always kept; trimming applies to plain message lists
    (not to provider ``GenerationRequest`` calls). ``turns_trimmed`` counts what was dropped.
    """

    def __init__(
        self,
        model: ModelProtocol,
        *,
        max_attempts: int = 3,
        base_delay: float = 0.5,
        max_delay: float = 8.0,
        retry_on: tuple[type[BaseException], ...] = TRANSIENT_ERRORS,
        trim_history: int = 0,
    ) -> None:
        if max_attempts < 1:
            raise SDKValidationError("max_attempts must be >= 1.")
        if trim_history < 0:
            raise SDKValidationError("trim_history must be >= 0.")
        self.model = model
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.retry_on = retry_on
        self.trim_history = trim_history
        self.attempts_made = 0
        self.turns_trimmed = 0

    @property
    def capabilities(self) -> Any:
        return getattr(self.model, "capabilities", None)

    @property
    def accepts_run_context(self) -> bool:
        return _accepts_run_context(self.model)

    @property
    def accepts_model_settings(self) -> bool:
        return _accepts_model_settings(self.model)

    @property
    def supports_response_schema(self) -> bool:
        return bool(getattr(self.model, "supports_response_schema", False))

    @property
    def response_schema_with_tools(self) -> bool:
        return bool(getattr(self.model, "response_schema_with_tools", False))

    @property
    def streams_response_schema(self) -> bool:
        return bool(getattr(self.model, "streams_response_schema", False))

    def _delay(self, attempt: int, error: BaseException) -> float:
        retry_after = getattr(error, "retry_after", None)
        if isinstance(retry_after, (int, float)) and retry_after >= 0:
            return float(min(retry_after, self.max_delay))
        backoff = min(self.max_delay, self.base_delay * 2 ** (attempt - 1))
        return float(backoff * (0.5 + random.random() / 2))

    async def generate(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> ModelResponse:
        """Call the wrapped model, retrying transient errors with backoff."""
        trims_left = self.trim_history
        attempt = 1
        while True:
            self.attempts_made += 1
            try:
                return await self.model.generate(messages, tools, timeout=timeout, **kwargs)
            except ProviderAuthError:
                raise
            except ProviderContextLengthError:
                shorter = trim_oldest_turn(messages) if isinstance(messages, list) else None
                if trims_left <= 0 or shorter is None:
                    raise
                trims_left -= 1
                self.turns_trimmed += 1
                messages = shorter  # trimming is not a transient retry: no backoff, no attempt
            except self.retry_on as exc:
                if attempt == self.max_attempts:
                    raise
                await asyncio.sleep(self._delay(attempt, exc))
                attempt += 1

    async def stream(
        self, *args: Any, model_settings: Any = None, **kwargs: Any
    ) -> AsyncIterator[Any]:
        """Stream from the wrapped model, retrying only failures before the first delta.

        Once a delta has been yielded the caller already holds partial output, and a
        retry would repeat it, so a failure after that point is raised unchanged.
        """
        stream_fn = self.model.stream  # type: ignore[attr-defined]
        kwargs = _stream_kwargs(stream_fn, model_settings, kwargs)
        trims_left = self.trim_history
        attempt = 1
        while True:
            self.attempts_made += 1
            yielded = False
            inner: Any = None
            try:
                inner = stream_fn(*args, **kwargs)
                async for delta in inner:
                    yielded = True
                    yield delta
                return
            except ProviderAuthError:
                raise
            except ProviderContextLengthError:
                # Like generate(): trim before the first delta and retry the stream.
                shorter = trim_oldest_turn(args[0]) if args and isinstance(args[0], list) else None
                if yielded or trims_left <= 0 or shorter is None:
                    raise
                trims_left -= 1
                self.turns_trimmed += 1
                args = (shorter, *args[1:])
                continue  # not a transient retry: no backoff, no attempt consumed
            except self.retry_on as exc:
                if yielded or attempt == self.max_attempts:
                    raise
                delay = self._delay(attempt, exc)
            finally:
                await _aclose(inner)
            await asyncio.sleep(delay)
            attempt += 1

    def __repr__(self) -> str:
        trim = f", trim_history={self.trim_history}" if self.trim_history else ""
        return f"RetryModel({self.model!r}, max_attempts={self.max_attempts}{trim})"


class FallbackModel:
    """Try each model in order, moving on after a provider failure.

    ``fallback_on`` defaults to every :class:`ProviderError` except authentication,
    so a misconfigured key is reported instead of silently switching providers.
    ``last_used`` records which model produced the most recent response.
    """

    def __init__(
        self,
        *models: ModelProtocol,
        fallback_on: tuple[type[BaseException], ...] = (ProviderError, TimeoutError),
    ) -> None:
        if not models:
            raise SDKValidationError("FallbackModel needs at least one model.")
        self.models: Sequence[ModelProtocol] = models
        self.fallback_on = fallback_on
        self.last_used: ModelProtocol | None = None

    @property
    def capabilities(self) -> Any:
        """Capabilities every model shares (a feature is on only if all models have it)."""
        caps: list[Any] = [getattr(m, "capabilities", None) for m in self.models]
        if any(c is None for c in caps):
            return None
        from dataclasses import fields, replace

        return replace(
            caps[0], **{f.name: all(getattr(c, f.name) for c in caps) for f in fields(caps[0])}
        )

    @property
    def accepts_run_context(self) -> bool:
        return all(_accepts_run_context(m) for m in self.models)

    @property
    def accepts_model_settings(self) -> bool:
        return all(_accepts_model_settings(m) for m in self.models)

    @property
    def supports_response_schema(self) -> bool:
        return all(getattr(m, "supports_response_schema", False) for m in self.models)

    @property
    def response_schema_with_tools(self) -> bool:
        return all(getattr(m, "response_schema_with_tools", False) for m in self.models)

    @property
    def streams_response_schema(self) -> bool:
        return all(getattr(m, "streams_response_schema", False) for m in self.models)

    async def generate(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> ModelResponse:
        """Return the first successful response, trying each model in order."""
        errors: list[BaseException] = []
        for model in self.models:
            try:
                response = await model.generate(messages, tools, timeout=timeout, **kwargs)
            except ProviderAuthError:
                raise
            except self.fallback_on as exc:
                errors.append(exc)
                continue
            self.last_used = model
            return response
        summary = "; ".join(f"{type(e).__name__}: {e}" for e in errors)
        raise ProviderError(
            f"All {len(self.models)} fallback models failed ({summary})",
            original_error=errors[-1],
        )

    async def stream(
        self, *args: Any, model_settings: Any = None, **kwargs: Any
    ) -> AsyncIterator[Any]:
        """Stream from the first model that starts; fall back only before the first delta.

        After a delta has been yielded the failure is raised, because switching models
        would repeat or contradict text the caller already received.
        """
        errors: list[BaseException] = []
        for model in self.models:
            stream_fn = model.stream  # type: ignore[attr-defined]
            yielded = False
            inner: Any = None
            try:
                inner = stream_fn(*args, **_stream_kwargs(stream_fn, model_settings, kwargs))
                async for delta in inner:
                    yielded = True
                    yield delta
                self.last_used = model
                return
            except ProviderAuthError:
                raise
            except self.fallback_on as exc:
                if yielded:
                    raise
                errors.append(exc)
            finally:
                await _aclose(inner)
        summary = "; ".join(f"{type(e).__name__}: {e}" for e in errors)
        raise ProviderError(
            f"All {len(self.models)} fallback models failed ({summary})",
            original_error=errors[-1],
        )

    def __repr__(self) -> str:
        return f"FallbackModel({', '.join(repr(m) for m in self.models)})"
