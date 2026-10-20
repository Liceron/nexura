# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Client-side rate limiting and a concurrency cap for model calls.

``RateLimiter`` is a shareable object: give the same limiter to several
``RateLimitedModel`` wrappers (or reuse one wrapped model across runs) and they are
limited together. It works across event loops, so the sync ``Agent.run`` (one loop per
call) and threads can share it.
"""

from __future__ import annotations

import contextlib
import inspect
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING, Any

from nexura_agents._lazy import LazyModule
from nexura_agents.errors import SDKValidationError
from nexura_agents.models.base import ModelProtocol
from nexura_agents.models.resilience import _accepts_model_settings, _accepts_run_context
from nexura_agents.types import ModelResponse, Usage

if TYPE_CHECKING:
    import asyncio
else:
    asyncio = LazyModule("asyncio")

_WINDOW = 60.0


def _positive_int(name: str, value: int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SDKValidationError(f"{name} must be an integer >= 1 or None.")
    return value


def _usage_tokens(usage: Usage | None) -> int:
    """Tokens a call reported; unknown usage counts as 0 (never guessed)."""
    if usage is None:
        return 0
    if usage.total_tokens is not None:
        return max(0, usage.total_tokens)
    return max(0, (usage.prompt_tokens or 0) + (usage.completion_tokens or 0))


class RateLimiter:
    """Cap concurrent model calls and pace requests and tokens per minute.

    - ``max_concurrent``: at most this many calls (or open streams) at once.
    - ``requests_per_minute``: at most this many call starts in any 60 second window.
    - ``tokens_per_minute``: after reported usage reaches this many tokens in the last
      60 seconds, new calls wait until older usage ages out. This is approximate: a
      call's tokens are only known after it finishes, so calls already in flight can
      push usage past the budget, and calls that report no usage count as 0 tokens.

    Waiting never busy-loops and is cancellable; a cancelled wait takes nothing. ``clock``
    returns seconds (default ``time.monotonic``) and ``sleep`` is the awaitable used for
    timed waits (default ``asyncio.sleep``); both exist so tests can run without delays.
    """

    def __init__(
        self,
        *,
        max_concurrent: int | None = None,
        requests_per_minute: int | None = None,
        tokens_per_minute: int | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Any] | None = None,
    ) -> None:
        self.max_concurrent = _positive_int("max_concurrent", max_concurrent)
        self.requests_per_minute = _positive_int("requests_per_minute", requests_per_minute)
        self.tokens_per_minute = _positive_int("tokens_per_minute", tokens_per_minute)
        self._clock = clock or time.monotonic
        self._sleep = sleep
        self._lock = threading.Lock()
        self._in_flight = 0
        self._starts: deque[float] = deque()
        self._usage: deque[tuple[float, int]] = deque()
        self._waiters: list[tuple[Any, Any]] = []  # (loop, future)

    @property
    def in_flight(self) -> int:
        """Calls currently holding a slot."""
        return self._in_flight

    def _delay_locked(self) -> float | None:
        """Seconds to wait (0.0 means wait for a release); ``None`` when a slot is free."""
        if self.max_concurrent is not None and self._in_flight >= self.max_concurrent:
            return 0.0
        now = self._clock()
        cutoff = now - _WINDOW
        while self._starts and self._starts[0] <= cutoff:
            self._starts.popleft()
        while self._usage and self._usage[0][0] <= cutoff:
            self._usage.popleft()
        delay = 0.0
        rpm = self.requests_per_minute
        if rpm is not None and len(self._starts) >= rpm:
            index = len(self._starts) - rpm
            delay = max(delay, self._starts[index] + _WINDOW - now)
        tpm = self.tokens_per_minute
        if tpm is not None:
            used = sum(tokens for _, tokens in self._usage)
            if used >= tpm:
                for stamp, tokens in self._usage:
                    used -= tokens
                    if used < tpm:
                        delay = max(delay, stamp + _WINDOW - now)
                        break
        if delay > 0.0:
            return delay
        self._in_flight += 1
        self._starts.append(now)
        return None

    def _wake_all(self) -> None:
        with self._lock:
            waiters, self._waiters = self._waiters, []
        for loop, fut in waiters:
            with contextlib.suppress(RuntimeError):  # that loop may be closed
                loop.call_soon_threadsafe(_resolve, fut)

    async def acquire(self) -> None:
        """Wait until a call may start, then take a slot. Pair with :meth:`release`."""
        loop = asyncio.get_running_loop()
        while True:
            with self._lock:
                delay = self._delay_locked()
                if delay is None:
                    return
                fut = None
                if delay == 0.0:
                    fut = loop.create_future()
                    self._waiters.append((loop, fut))
            try:
                if fut is not None:
                    await fut
                else:
                    await (self._sleep or asyncio.sleep)(delay)
            finally:
                if fut is not None:
                    with self._lock:
                        self._waiters = [w for w in self._waiters if w[1] is not fut]

    def release(self, usage: Usage | None = None) -> None:
        """Give back a slot taken by :meth:`acquire`, recording the call's reported usage."""
        with self._lock:
            if self._in_flight > 0:
                self._in_flight -= 1
            self._record_locked(usage)
        self._wake_all()

    def record_usage(self, usage: Usage | None) -> None:
        """Count a call's reported tokens toward ``tokens_per_minute``."""
        with self._lock:
            self._record_locked(usage)

    def _record_locked(self, usage: Usage | None) -> None:
        tokens = _usage_tokens(usage)
        if tokens and self.tokens_per_minute is not None:
            self._usage.append((self._clock(), tokens))

    def __repr__(self) -> str:
        return (
            f"RateLimiter(max_concurrent={self.max_concurrent}, "
            f"requests_per_minute={self.requests_per_minute}, "
            f"tokens_per_minute={self.tokens_per_minute})"
        )


def _resolve(fut: Any) -> None:
    if not fut.done():
        fut.set_result(None)


class RateLimitedModel:
    """Wrap a model so every ``generate`` and ``stream`` call goes through a limiter.

    A stream holds its slot until it finishes, fails, is closed or is cancelled. Pass one
    ``RateLimiter`` to several wrappers to limit them together. Nest it inside
    ``RetryModel`` so each retry waits for a slot: ``RetryModel(RateLimitedModel(m, lim))``.
    """

    def __init__(self, model: ModelProtocol, limiter: RateLimiter) -> None:
        if not isinstance(limiter, RateLimiter):
            raise SDKValidationError("limiter must be a RateLimiter.")
        self.model = model
        self.limiter = limiter

    @property
    def capabilities(self) -> Any:
        return getattr(self.model, "capabilities", None)

    @property
    def model_name(self) -> str | None:
        name = getattr(self.model, "model", None) or getattr(self.model, "model_name", None)
        return name if isinstance(name, str) and name else None

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

    async def generate(self, *args: Any, **kwargs: Any) -> ModelResponse:
        """Wait for a slot, call the wrapped model, then release and record usage."""
        await self.limiter.acquire()
        usage: Usage | None = None
        try:
            response: ModelResponse = await self.model.generate(*args, **kwargs)
            usage = response.usage
            return response
        finally:
            self.limiter.release(usage)

    async def stream(
        self, *args: Any, model_settings: Any = None, **kwargs: Any
    ) -> AsyncIterator[Any]:
        """Stream from the wrapped model while holding a slot until the stream ends."""
        stream_fn = self.model.stream  # type: ignore[attr-defined]
        if model_settings is not None:
            try:
                takes = "model_settings" in inspect.signature(stream_fn).parameters
            except (TypeError, ValueError):
                takes = False
            if takes:
                kwargs["model_settings"] = model_settings
        await self.limiter.acquire()
        usage: Usage | None = None
        inner: Any = None
        try:
            inner = stream_fn(*args, **kwargs)
            async for delta in inner:
                reported = getattr(delta, "usage", None)
                if reported is not None:  # any delta may carry usage, as the runner reads it
                    usage = reported
                yield delta
        finally:
            try:
                aclose = getattr(inner, "aclose", None)
                if aclose is not None:
                    await aclose()
            finally:
                self.limiter.release(usage)

    def __repr__(self) -> str:
        return f"RateLimitedModel({self.model!r}, {self.limiter!r})"
