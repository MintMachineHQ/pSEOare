"""Async rate limiting primitives."""
from __future__ import annotations

import asyncio
import time
from collections import deque


class RateLimiter:
    """Serialises calls so at most `per_minute` requests happen in any 60s window."""

    def __init__(self, per_minute: float = 14.5) -> None:
        self.min_interval = 60.0 / per_minute if per_minute > 0 else 0.0
        self._calls: deque[float] = deque()
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def acquire(self) -> None:
        if self.min_interval <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            while self._calls and now - self._calls[0] >= 60.0:
                self._calls.popleft()
            wait_from_window = 0.0
            if len(self._calls) >= int(60.0 / self.min_interval):
                wait_from_window = 60.0 - (now - self._calls[0]) + 0.05
            gap = now - self._last
            wait_gap = max(0.0, self.min_interval - gap)
            delay = max(wait_gap, wait_from_window)
            if delay > 0:
                await asyncio.sleep(delay)
            now = time.monotonic()
            self._calls.append(now)
            self._last = now


class TokenBucket:
    """Bursty limiter for outbound scrapes (e.g. 4 req/s sustained)."""

    def __init__(self, rate_per_second: float = 4.0) -> None:
        self.rate = rate_per_second
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = max(0.0, 1.0 / self.rate - (now - self._last))
            if delay:
                await asyncio.sleep(delay)
            self._last = time.monotonic()


class CallBudget:
    """Hard cap on total calls per run so a cron job can never burn the quota."""

    def __init__(self, limit: int, seen: set[str] | None = None) -> None:
        self.limit = limit
        self.used = 0
        # Page paths already published by earlier runs. Sources order their output so
        # unseen pages come first, which makes each scheduled run add new pages
        # instead of regenerating the same alphabetical head of the list.
        self.seen: set[str] = seen if seen is not None else set()

    def is_new(self, path: str) -> bool:
        return path not in self.seen

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    def take(self, n: int = 1) -> bool:
        if self.used + n > self.limit:
            return False
        self.used += n
        return True