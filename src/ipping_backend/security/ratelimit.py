from __future__ import annotations

import time
from collections import defaultdict, deque


class SlidingWindowLimiter:
    """In-memory limiter. Fine while there is a single backend process (see the README)."""

    def __init__(self, limit: int, window_s: float) -> None:
        self.limit = limit
        self.window_s = window_s
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        hits = self._hits[key]
        while hits and now - hits[0] > self.window_s:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True

    def prune(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        for key in [
            k for k, hits in self._hits.items() if not hits or now - hits[-1] > self.window_s
        ]:
            del self._hits[key]


class Limiters:
    """The per-application limiters for unauthenticated, abuse-prone endpoints."""

    def __init__(self) -> None:
        import asyncio

        self.login = SlidingWindowLimiter(limit=10, window_s=60)
        self.setup = SlidingWindowLimiter(limit=5, window_s=60)
        self.enroll = SlidingWindowLimiter(limit=10, window_s=60)
        self.setup_lock = asyncio.Lock()
