"""Redis-backed sliding-window limiter (PROM16 C2) — fakeredis, no infra."""

import pytest

from app.ratelimit import RateLimiter


def _fake_redis():
    from fakeredis import FakeAsyncRedis

    return FakeAsyncRedis(decode_responses=True)


async def test_redis_limiter_enforces_window():
    rl = RateLimiter(max_events=3, window_seconds=60, redis_url="redis://fake")
    rl._redis = _fake_redis()  # inject → _get_redis returns it, no real connect

    key = "1.2.3.4"
    assert [await rl.allow(key) for _ in range(3)] == [True, True, True]
    assert await rl.allow(key) is False  # 4th over the window


async def test_redis_limiter_is_per_key():
    rl = RateLimiter(max_events=1, window_seconds=60, redis_url="redis://fake")
    rl._redis = _fake_redis()
    assert await rl.allow("a") is True
    assert await rl.allow("b") is True  # different key, own budget
    assert await rl.allow("a") is False


async def test_falls_back_to_memory_when_no_redis():
    rl = RateLimiter(max_events=2, window_seconds=60, redis_url="")  # no redis
    assert await rl.allow("k") is True
    assert await rl.allow("k") is True
    assert await rl.allow("k") is False  # in-process engine still enforces
