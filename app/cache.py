"""Tiny in-process async TTL cache.

PROM16 C1 (A2S1/A2S2/A2S3): the KG stats query is a full-graph `count(n)` scan;
the data is near-static, so cache the result for a short window. In-process is
sufficient at single-replica; swap for Redis when the service scales out.
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")


class TTLCache:
    def __init__(self, ttl_seconds: float) -> None:
        self.ttl = ttl_seconds
        self._store: dict[str, tuple[float, object]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _now(self) -> float:
        return time.monotonic()

    async def get_or_set(self, key: str, producer: Callable[[], Awaitable[T]]) -> T:
        if self.ttl <= 0:
            return await producer()
        hit = self._store.get(key)
        if hit is not None and (self._now() - hit[0]) < self.ttl:
            return hit[1]  # type: ignore[return-value]
        # single-flight: only one coroutine recomputes a given key
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            hit = self._store.get(key)
            if hit is not None and (self._now() - hit[0]) < self.ttl:
                return hit[1]  # type: ignore[return-value]
            value = await producer()
            self._store[key] = (self._now(), value)
            return value

    def clear(self) -> None:
        self._store.clear()
