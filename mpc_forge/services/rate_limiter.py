"""Async rate limiter that enforces a minimum interval between operation *starts*.

The naive pattern ``async with lock: await sleep(dt); await do_work()`` serialises
every awaiter behind a single mutex AND holds that mutex during the (usually
long) network round-trip. That collapses effective throughput to
``1 / (dt + latency)`` regardless of how many callers try to run in parallel.

This limiter instead treats ``dt`` as the minimum spacing between when successive
callers are allowed to *begin* their work. Once acquire() returns, the caller
runs unlocked. N concurrent awaiters will start ``dt`` apart and their network
round-trips overlap, so aggregate throughput is close to ``1 / dt``.
"""
from __future__ import annotations

import asyncio
import time


class AsyncRateLimiter:
    """Token-bucket-lite: enforces min ``interval`` seconds between acquires."""

    __slots__ = ("_interval", "_lock", "_next_available")

    def __init__(self, interval: float) -> None:
        self._interval = interval
        self._lock = asyncio.Lock()
        self._next_available = 0.0

    async def acquire(self) -> None:
        """Block until the caller may start its next rate-limited operation."""
        async with self._lock:
            now = time.monotonic()
            wait = self._next_available - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next_available = now + self._interval
