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
import bisect
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


class TieredRateLimiter:
    """Planificador de dos límites simultáneos: uno general y otro, más
    estricto, para un subconjunto de operaciones ("pesadas").

    Encadenar dos :class:`AsyncRateLimiter` no funciona: el tiempo que una
    operación pesada pasa esperando en el segundo limitador, después de haber
    consumido su turno en el primero, acorta el hueco real con la siguiente
    operación pesada. Con carga mixta se violaba el límite estricto.

    Aquí cada llamada **reserva** su instante de salida de forma atómica (sin
    ``await`` entre leer y escribir el estado, así que no hace falta lock):

    * a ``general`` segundos de cualquier otra reserva, y
    * si es pesada, a ``heavy`` segundos de la reserva pesada anterior.

    Las operaciones ligeras pueden ocupar los huecos libres entre reservas
    pesadas futuras, así que una cola de búsquedas no bloquea las consultas
    rápidas.
    """

    __slots__ = ("_general", "_heavy", "_slots", "_next_heavy")

    def __init__(self, general: float, heavy: float) -> None:
        self._general = general
        self._heavy = heavy
        self._slots: list[float] = []   # reservas recientes/futuras, ordenadas
        self._next_heavy = 0.0

    def reserve(self, heavy: bool = False) -> float:
        """Reserva un instante de salida (``time.monotonic()``) y lo devuelve."""
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
