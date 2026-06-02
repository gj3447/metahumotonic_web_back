"""PROM16 P0 hardening: stats cache, XFF trust, feedback created_at type."""

import asyncio
import datetime

import pytest

from app.cache import TTLCache
from app.routers.feedback import _client_key


async def test_ttl_cache_single_flight_and_reuse():
    calls = {"n": 0}

    async def producer():
        calls["n"] += 1
        await asyncio.sleep(0.01)
        return "v"

    cache = TTLCache(ttl_seconds=100)
    # concurrent first access → producer runs exactly once (single-flight)
    results = await asyncio.gather(*[cache.get_or_set("k", producer) for _ in range(5)])
    assert results == ["v"] * 5
    assert calls["n"] == 1
    # within TTL → cached, no new call
    await cache.get_or_set("k", producer)
    assert calls["n"] == 1


async def test_ttl_zero_disables_cache():
    calls = {"n": 0}

    async def producer():
        calls["n"] += 1
        return calls["n"]

    cache = TTLCache(ttl_seconds=0)
    assert await cache.get_or_set("k", producer) == 1
    assert await cache.get_or_set("k", producer) == 2  # always recomputes


class _Req:
    def __init__(self, headers, peer="9.9.9.9"):
        self.headers = headers

        class _C:
            host = peer

        self.client = _C()


def _trust(monkeypatch, on):
    from app.config import settings

    monkeypatch.setattr(settings, "trust_proxy", on)


def test_xff_uses_rightmost_not_spoofable_leftmost(monkeypatch):
    _trust(monkeypatch, True)
    # attacker spoofs leftmost; proxy appends real IP on the right
    req = _Req({"x-forwarded-for": "1.1.1.1, 2.2.2.2, 10.0.0.5"})
    assert _client_key(req) == "10.0.0.5"


def test_cf_connecting_ip_wins(monkeypatch):
    _trust(monkeypatch, True)
    req = _Req({"cf-connecting-ip": "203.0.113.7", "x-forwarded-for": "1.1.1.1, 10.0.0.5"})
    assert _client_key(req) == "203.0.113.7"


def test_default_no_trust_ignores_forwarded_headers(monkeypatch):
    # full-verify: trust_proxy defaults False → spoofed headers ignored
    _trust(monkeypatch, False)
    req = _Req({"cf-connecting-ip": "1.2.3.4", "x-forwarded-for": "5.6.7.8"})
    assert _client_key(req) == "9.9.9.9"  # socket peer only


def test_falls_back_to_peer_without_headers(monkeypatch):
    _trust(monkeypatch, True)
    req = _Req({})
    assert _client_key(req) == "9.9.9.9"


async def test_feedback_created_at_is_datetime(client):
    from app.store import store

    await client.post("/api/feedback", json={"subject": "s", "body": "b"})
    assert isinstance(store.memory[0]["created_at"], datetime.datetime)
