"""Redis-backed sliding-window limiter (PROM16 C2) — fakeredis, no infra."""

import pytest

from app.ratelimit import RateLimiter, RateLimitUnavailable


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


async def test_fail_closed_rejects_when_redis_is_not_configured():
    rl = RateLimiter(max_events=2, window_seconds=60, redis_url="", fail_closed=True)
    with pytest.raises(RateLimitUnavailable):
        await rl.allow("k")
    assert await rl.ready() is False


async def test_ready_pings_and_discards_a_stale_cached_client():
    class BrokenRedis:
        def __init__(self) -> None:
            self.closed = False

        async def ping(self):
            raise ConnectionError("redis went away")

        async def aclose(self):
            self.closed = True

    rl = RateLimiter(
        max_events=2,
        window_seconds=60,
        redis_url="redis://fake",
        fail_closed=True,
    )
    broken = BrokenRedis()
    rl._redis = broken

    assert await rl.ready() is False
    assert broken.closed is True
    assert rl._redis is None
    assert rl._breaker.is_open() is True


async def test_allow_closes_a_cached_client_that_fails_during_use():
    class BrokenRedis:
        def __init__(self) -> None:
            self.closed = False

        def pipeline(self, *, transaction: bool):
            assert transaction is True
            raise ConnectionError("pipeline failed")

        async def aclose(self):
            self.closed = True

    rl = RateLimiter(
        max_events=2,
        window_seconds=60,
        redis_url="redis://fake",
        fail_closed=True,
    )
    broken = BrokenRedis()
    rl._redis = broken

    with pytest.raises(RateLimitUnavailable):
        await rl.allow("k")
    assert broken.closed is True
    assert rl._redis is None
