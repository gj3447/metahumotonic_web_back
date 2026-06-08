"""Tiny in-process async TTL cache — bounded (LRU) so attacker-controlled cache
keys (e.g. ?cycle=/?domain=/?offset=) can't grow memory without bound.

PROM16 C1 (A2S1/A2S2/A2S3): the KG stats query is a full-graph `count(n)` scan;
the data is near-static, so cache the result for a short window. In-process is
sufficient at single-replica; swap for Redis when the service scales out.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")


class TTLCache:
    def __init__(self, ttl_seconds: float, max_entries: int = 512) -> None:
        self.ttl = ttl_seconds
        self.max_entries = max(1, int(max_entries))
        # OrderedDict = LRU: most-recently-used at the end, evict from the front
        self._store: "OrderedDict[str, tuple[float, object]]" = OrderedDict()
        self._locks: "OrderedDict[str, asyncio.Lock]" = OrderedDict()

    def _now(self) -> float:
        return time.monotonic()

    def _fresh(self, key: str):
        hit = self._store.get(key)
        if hit is not None and (self._now() - hit[0]) < self.ttl:
            self._store.move_to_end(key)  # mark recently used
            return hit
        if hit is not None:  # expired → drop eagerly
            self._store.pop(key, None)
        return None

    def _lock_for(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        self._locks.move_to_end(key)
        # bound the lock map the same way as the store (never-purged locks were
        # the real leak: a flood of distinct keys left a permanent Lock each)
        while len(self._locks) > self.max_entries:
            self._locks.popitem(last=False)
        return lock

    def _evict(self) -> None:
        while len(self._store) > self.max_entries:
            old_key, _ = self._store.popitem(last=False)
            self._locks.pop(old_key, None)

    async def get_or_set(self, key: str, producer: Callable[[], Awaitable[T]]) -> T:
        if self.ttl <= 0:
            return await producer()
        hit = self._fresh(key)
        if hit is not None:
            return hit[1]  # type: ignore[return-value]
        # single-flight: only one coroutine recomputes a given key
        lock = self._lock_for(key)
        async with lock:
            hit = self._fresh(key)
            if hit is not None:
                return hit[1]  # type: ignore[return-value]
            value = await producer()
            self._store[key] = (self._now(), value)
            self._store.move_to_end(key)
            self._evict()
            return value

    def clear(self) -> None:
        self._store.clear()
        self._locks.clear()
