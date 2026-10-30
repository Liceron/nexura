# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Cancel an agent run from outside: ``Runner().run(agent, prompt, cancel=token)``.

``token.cancel()`` can be called from any thread or task. The run stops at the next
step, or interrupts the model call or ``async`` tool it is waiting on, and ends with a
``RUN_CANCELLED`` event and a ``RunResult`` whose ``status`` is ``"cancelled"``.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

from nexura_agents._lazy import LazyModule

if TYPE_CHECKING:
    import asyncio
else:
    asyncio = LazyModule("asyncio")


class CancelToken:
    """A one-way switch that asks one or more runs to stop."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]] = []
        self.reason: str | None = None

    @property
    def cancelled(self) -> bool:
        """True once :meth:`cancel` was called."""
        return self._event.is_set()

    def cancel(self, reason: str | None = None) -> None:
        """Ask every run using this token to stop. Safe from any thread; repeat calls do nothing."""
        with self._lock:
            if self._event.is_set():
                return
            self.reason = reason
            self._event.set()
            waiters, self._waiters = self._waiters, []
        for loop, future in waiters:
            if not loop.is_closed():
                loop.call_soon_threadsafe(_resolve, future)

    async def wait(self) -> None:
        """Return once the token is cancelled (used by the runner to interrupt waits)."""
        if self.cancelled:
            return
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()
        with self._lock:
            if self._event.is_set():
                return
            self._waiters.append((loop, future))
        try:
            await future
        finally:
            with self._lock:
                self._waiters = [w for w in self._waiters if w[1] is not future]

    def __repr__(self) -> str:
        return f"CancelToken(cancelled={self.cancelled}, reason={self.reason!r})"


def _resolve(future: Any) -> None:
    if not future.done():
        future.set_result(None)
