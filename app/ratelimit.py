"""Rate limiting.

PROM16 C2 (A3S1/A3S2/A3S3/A3S4, unanimous): an in-process limiter is wrong the
moment there are multiple replicas (limit multiplies) or a restart (counter
resets). `RateLimiter` uses a Redis sliding-window when `redis_url` is set and
reachable, and falls back to the in-process limiter otherwise — so it stays
correct at scale yet runs with zero infra in CI / offline.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from collections import defaultdict, deque

from .breaker import Breaker

log = logging.getLogger("mhb.ratelimit")


class RateLimitUnavailable(RuntimeError):
    """Raised when a fail-closed distributed limiter cannot use Redis."""


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

    def __init__(
        self,
        max_events: int,
        window_seconds: int,
        redis_url: str = "",
        *,
        fail_closed: bool = False,
    ) -> None:
        self._mem = SlidingWindowRateLimiter(max_events, window_seconds)
        self.redis_url = redis_url
        self.fail_closed = fail_closed
        self._redis = None
        self._redis_lock = asyncio.Lock()
        self._breaker = Breaker()

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
        if not self.redis_url or self._breaker.is_open():
            return None
        if self._redis is not None:
            return self._redis
        async with self._redis_lock:
            if self._redis is not None:
                return self._redis
            client = None
            try:
                import redis.asyncio as aioredis

                client = aioredis.from_url(self.redis_url, decode_responses=True)
                await client.ping()
                self._redis = client
                return client
            except Exception as e:  # noqa: BLE001  # pragma: no cover - infra boundary
                await self._discard_redis(client)
                log.warning("redis rate-limit unavailable, using in-process: %s", e)
                return None

    async def allow(self, key: str) -> bool:
        client = await self._get_redis()
        if client is None:
            if self.fail_closed:
                raise RateLimitUnavailable("distributed rate limiter is unavailable")
            return self._mem.allow(key)
        try:
            result = await self._allow_redis(client, key)
            self._breaker.reset()  # healthy again → re-share across replicas
            return result
        except Exception as e:  # pragma: no cover - infra dependent
            await self._discard_redis(client)
            log.warning("redis rate-limit error, falling back in-process: %s", e)
            if self.fail_closed:
                raise RateLimitUnavailable(
                    "distributed rate limiter is unavailable"
                ) from e
            return self._mem.allow(key)

    async def ready(self) -> bool:
        """Return whether the configured backend can enforce this limiter."""

        if not self.redis_url:
            return not self.fail_closed
        client = await self._get_redis()
        if client is None:
            return False
        try:
            await client.ping()
        except Exception as exc:  # noqa: BLE001 - dependency health boundary
            await self._discard_redis(client)
            log.warning("redis rate-limit readiness failed: %s", exc)
            return False
        self._breaker.reset()
        return True

    async def _allow_redis(self, client, key: str) -> bool:
        now_ms = time.time() * 1000.0
        window_ms = self.window_seconds * 1000.0
        rk = f"mhb:rl:{key}"
        # globally-unique member: a class counter resets to 0 per process, so two
        # replicas could mint identical members → ZADD dedups → undercount.
        member = f"{now_ms:.0f}-{uuid.uuid4().hex}"
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

    async def _discard_redis(self, client) -> None:
        self._breaker.trip()
        if self._redis is client:
            self._redis = None
        if client is None:
            return
        try:
            await client.aclose()
        except Exception as close_exc:  # noqa: BLE001 - cleanup boundary
            log.debug("failed to close broken Redis client: %s", close_exc)

    async def close(self) -> None:
        if self._redis is not None:
            client = self._redis
            self._redis = None
            await client.aclose()
