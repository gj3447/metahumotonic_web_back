"""Rate limiting.

PROM16 C2 (A3S1/A3S2/A3S3/A3S4, unanimous): an in-process limiter is wrong the
moment there are multiple replicas (limit multiplies) or a restart (counter
resets). `RateLimiter` uses a Redis sliding-window when `redis_url` is set and
reachable, and falls back to the in-process limiter otherwise — so it stays
correct at scale yet runs with zero infra in CI / offline.
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from collections import defaultdict, deque

log = logging.getLogger("mhb.ratelimit")


class SlidingWindowRateLimiter:
    """In-process sliding window (single process). Fallback engine + CI."""

    def __init__(self, max_events: int, window_seconds: int) -> None:
        self.max_events = max_events
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        cutoff = now - self.window_seconds
        with self._lock:
            q = self._hits[key]
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= self.max_events:
                return False
            q.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


class RateLimiter:
    """Async limiter: Redis sliding-window backend, in-process fallback."""

    _counter = itertools.count()

    def __init__(self, max_events: int, window_seconds: int, redis_url: str = "") -> None:
        self._mem = SlidingWindowRateLimiter(max_events, window_seconds)
        self.redis_url = redis_url
        self._redis = None
        self._redis_failed = False

    # max_events / window_seconds proxy the in-process engine so there is a
    # single source of truth (and tests can mutate them on the instance).
    @property
    def max_events(self) -> int:
        return self._mem.max_events

    @max_events.setter
    def max_events(self, v: int) -> None:
        self._mem.max_events = v

    @property
    def window_seconds(self) -> int:
        return self._mem.window_seconds

    @window_seconds.setter
    def window_seconds(self, v: int) -> None:
        self._mem.window_seconds = v

    async def _get_redis(self):
        if not self.redis_url or self._redis_failed:
            return None
        if self._redis is not None:
            return self._redis
        try:
            import redis.asyncio as aioredis

            client = aioredis.from_url(self.redis_url, decode_responses=True)
            await client.ping()
            self._redis = client
            return client
        except Exception as e:  # pragma: no cover - infra dependent
            self._redis_failed = True
            log.warning("redis rate-limit unavailable, using in-process: %s", e)
            return None

    async def allow(self, key: str) -> bool:
        client = await self._get_redis()
        if client is None:
            return self._mem.allow(key)
        try:
            return await self._allow_redis(client, key)
        except Exception as e:  # pragma: no cover - infra dependent
            log.warning("redis rate-limit error, falling back in-process: %s", e)
            return self._mem.allow(key)

    async def _allow_redis(self, client, key: str) -> bool:
        now_ms = time.time() * 1000.0
        window_ms = self.window_seconds * 1000.0
        rk = f"mhb:rl:{key}"
        member = f"{now_ms:.0f}-{next(self._counter)}"
        async with client.pipeline(transaction=True) as pipe:
            pipe.zremrangebyscore(rk, 0, now_ms - window_ms)
            pipe.zadd(rk, {member: now_ms})
            pipe.zcard(rk)
            pipe.expire(rk, self.window_seconds + 1)
            results = await pipe.execute()
        count = results[2]
        if count > self.max_events:
            # don't let a denied request count against future windows
            await client.zrem(rk, member)
            return False
        return True

    def reset(self) -> None:
        self._mem.reset()

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None
