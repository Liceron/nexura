# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Per-key sliding-window rate limiter (in memory, per process).

Used by the knowledge and Studio evaluation routes. It lived in the publishing package until
publishing moved to the closed edition (EDITIONS_PLAN step 4); it stays here as shared core.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable


class SlidingWindowRateLimiter:
    """Allow at most ``limit`` hits per ``window`` seconds for each key."""

    def __init__(self, window: float = 60.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.window = window
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str, limit: int) -> tuple[bool, float]:
        """Record a hit. Returns ``(allowed, retry_after_seconds)``."""
        now = self._clock()
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and now - hits[0] >= self.window:
                hits.popleft()
            if len(hits) >= limit:
                return False, max(0.0, self.window - (now - hits[0]))
            hits.append(now)
            return True, 0.0

    def reset(self, key_prefix: str) -> None:
        """Forget all hits for keys starting with ``key_prefix`` (used on revocation)."""
        with self._lock:
            for key in [k for k in self._hits if k.startswith(key_prefix)]:
                del self._hits[key]
