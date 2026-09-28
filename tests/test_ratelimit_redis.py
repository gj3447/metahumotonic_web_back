"""Redis-backed sliding-window limiter (PROM16 C2) — fakeredis, no infra."""

import asyncio

import pytest
import redis.asyncio as aioredis

import app.ratelimit as ratelimit
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


class _NeverPing:
    def __init__(self) -> None:
        self.closed = False
        self.ping_started = asyncio.Event()
        self._never = asyncio.Event()

    async def ping(self):
        self.ping_started.set()
        await self._never.wait()

    async def aclose(self):
        self.closed = True


class _NeverPipeline:
    def __init__(self) -> None:
        self._never = asyncio.Event()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def zremrangebyscore(self, *_args):
        pass

    def zadd(self, *_args):
        pass

    def zcard(self, *_args):
        pass

    def expire(self, *_args):
        pass

    async def execute(self):
        await self._never.wait()


class _NeverPipelineClient:
    def __init__(self) -> None:
        self.closed = False

    def pipeline(self, *, transaction: bool):
        assert transaction is True
        return _NeverPipeline()

    async def aclose(self):
        self.closed = True


class _NeverClose:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self._never = asyncio.Event()

    async def aclose(self):
        self.started.set()
        await self._never.wait()


async def test_connect_ping_timeout_is_bounded_and_uses_socket_limits(monkeypatch, caplog):
    monkeypatch.setattr(ratelimit, "REDIS_IO_TIMEOUT_SECONDS", 0.01)
    client = _NeverPing()
    captured: dict[str, object] = {}

    def from_url(_url: str, **kwargs):
        captured.update(kwargs)
        return client

    monkeypatch.setattr(aioredis, "from_url", from_url)
    limiter = RateLimiter(max_events=2, window_seconds=60, redis_url="redis://unreachable", fail_closed=True)
    async with asyncio.timeout(0.2):
        assert await limiter.ready() is False
    assert client.closed is True
    assert limiter._breaker.is_open() is True
    assert captured["socket_connect_timeout"] == 0.01
    assert captured["socket_timeout"] == 0.01
    assert "redis://unreachable" not in caplog.text


async def test_allow_timeout_falls_back_or_fails_closed_without_hanging(monkeypatch):
    monkeypatch.setattr(ratelimit, "REDIS_IO_TIMEOUT_SECONDS", 0.01)

    fallback = RateLimiter(max_events=2, window_seconds=60, redis_url="redis://unreachable")
    fallback_client = _NeverPipelineClient()
    fallback._redis = fallback_client
    async with asyncio.timeout(0.2):
        assert await fallback.allow("fallback") is True
    assert fallback_client.closed is True
    assert fallback._breaker.is_open() is True
    assert await fallback.allow("fallback") is True
    assert await fallback.allow("fallback") is False

    fail_closed = RateLimiter(max_events=2, window_seconds=60, redis_url="redis://unreachable", fail_closed=True)
    closed_client = _NeverPipelineClient()
    fail_closed._redis = closed_client
    async with asyncio.timeout(0.2):
        with pytest.raises(RateLimitUnavailable):
            await fail_closed.allow("closed")
    assert closed_client.closed is True
    assert fail_closed._breaker.is_open() is True


async def test_redis_cleanup_timeout_does_not_hang_shutdown(monkeypatch):
    monkeypatch.setattr(ratelimit, "REDIS_IO_TIMEOUT_SECONDS", 0.01)
    limiter = RateLimiter(max_events=2, window_seconds=60, redis_url="redis://unreachable")
    client = _NeverClose()
    limiter._redis = client
    async with asyncio.timeout(0.2):
        await limiter.close()
    assert client.started.is_set()
    assert limiter._redis is None


async def test_concurrent_blackholed_connect_attempts_once_then_fails_closed(monkeypatch):
    monkeypatch.setattr(ratelimit, "REDIS_IO_TIMEOUT_SECONDS", 0.05)
    clients: list[_NeverPing] = []

    def from_url(_url: str, **_kwargs):
        client = _NeverPing()
        clients.append(client)
        return client

    monkeypatch.setattr(aioredis, "from_url", from_url)
    limiter = RateLimiter(max_events=20, window_seconds=60, redis_url="redis://unreachable", fail_closed=True)
    first = asyncio.create_task(limiter.allow("first"))
    while not clients:
        await asyncio.sleep(0)
    await clients[0].ping_started.wait()
    followers = [asyncio.create_task(limiter.allow(f"queued-{index}")) for index in range(8)]
    await asyncio.sleep(0)  # let followers queue on the connection lock
    async with asyncio.timeout(0.5):
        results = await asyncio.gather(first, *followers, return_exceptions=True)

    assert len(clients) == 1
    assert all(isinstance(result, RateLimitUnavailable) for result in results)
    assert clients[0].closed is True
    assert limiter._breaker.is_open() is True
