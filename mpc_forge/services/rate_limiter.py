from __future__ import annotations

import asyncio
import bisect
import time


class AsyncRateLimiter:
    __slots__ = ("_interval", "_lock", "_next_available")

    def __init__(self, interval: float) -> None:
        self._interval = interval
        self._lock = asyncio.Lock()
        self._next_available = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._next_available - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next_available = now + self._interval


class TieredRateLimiter:
    __slots__ = ("_general", "_heavy", "_next_heavy", "_slots")

    def __init__(self, general: float, heavy: float) -> None:
        self._general = general
        self._heavy = heavy
        self._slots: list[float] = []
        self._next_heavy = 0.0

    def reserve(self, heavy: bool = False) -> float:
        gap = self._general
        now = time.monotonic()
        self._slots = [s for s in self._slots if s > now - gap]
        t = max(now, self._next_heavy) if heavy else now
        for s in self._slots:
            if t + gap <= s:
                break
            if t < s + gap:
                t = s + gap
        bisect.insort(self._slots, t)
        if heavy:
            self._next_heavy = t + self._heavy
        return t

    async def acquire(self, heavy: bool = False) -> None:
        wait = self.reserve(heavy) - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
