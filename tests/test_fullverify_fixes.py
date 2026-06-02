"""Fixes from the full-verify adversarial pass."""

import time

import pytest

from app.breaker import Breaker


def test_breaker_is_not_a_permanent_latch():
    b = Breaker(cooldown=0.05)
    assert b.is_open() is False
    b.trip()
    assert b.is_open() is True           # open right after a failure
    time.sleep(0.06)
    assert b.is_open() is False          # ...but recovers after cooldown (NOT permanent)


def test_breaker_reset_clears_immediately():
    b = Breaker(cooldown=100)
    b.trip()
    assert b.is_open() is True
    b.reset()
    assert b.is_open() is False


async def test_stats_counts_consistent_with_list_endpoints(client):
    stats = (await client.get("/api/stats")).json()
    domains = (await client.get("/api/domains")).json()
    skills = (await client.get("/api/skills")).json()
    # full-verify: /api/stats domains/skills must match the list endpoints
    assert stats["domains"] == len(domains)
    assert stats["skills"] == len(skills)


async def test_control_chars_rejected_in_single_line_fields(client):
    r = await client.post(
        "/api/feedback", json={"subject": "ok\nBcc: evil@x", "body": "b"}
    )
    assert r.status_code == 422  # header/log injection blocked


async def test_multiline_body_is_allowed(client):
    r = await client.post(
        "/api/feedback", json={"subject": "fine", "body": "line1\nline2\nline3"}
    )
    assert r.status_code == 200  # textarea body keeps its newlines
