"""KG Cypher proxy — key-gated read/write split.

Auth is enforced before any DB call, so these run with zero infra. The
happy-path tests stub kg.run_cypher to assert the route wiring + access mode
without a live Neo4j.
"""

from __future__ import annotations

import pytest
import structlog
from neo4j.exceptions import ClientError

from app import kg as kg_mod
from app.config import settings

READ_KEY = "test_read_key_aaa"
WRITE_KEY = "test_write_key_bbb"


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setattr(settings, "kg_read_key", READ_KEY)
    monkeypatch.setattr(settings, "kg_write_key", WRITE_KEY)


@pytest.fixture
def stub_cypher(monkeypatch):
    """Record (query, params, write) and return a canned row."""
    calls = []

    async def _fake(query, params, *, write):
        calls.append({"query": query, "params": params, "write": write})
        return [{"n": 1}]

    monkeypatch.setattr(kg_mod.kg, "run_cypher", _fake)
    return calls


# --- disabled by default (opt-in) ------------------------------------------ #

async def test_proxy_disabled_without_keys(client):
    for path in ("/api/kg/read", "/api/kg/write"):
        r = await client.post(path, json={"query": "RETURN 1"})
        assert r.status_code == 503, path


# --- auth gating ------------------------------------------------------------ #

async def test_read_requires_key(client, keys):
    r = await client.post("/api/kg/read", json={"query": "RETURN 1"})
    assert r.status_code == 401


async def test_read_rejects_wrong_key(client, keys):
    r = await client.post(
        "/api/kg/read", json={"query": "RETURN 1"},
        headers={"X-API-Key": "nope"},
    )
    assert r.status_code == 401


async def test_write_rejects_read_key(client, keys):
    r = await client.post(
        "/api/kg/write", json={"query": "CREATE (n) RETURN n"},
        headers={"X-API-Key": READ_KEY},
    )
    assert r.status_code == 401


# --- happy path (stubbed DB) ----------------------------------------------- #

async def test_read_with_read_key(client, keys, stub_cypher):
    r = await client.post(
        "/api/kg/read", json={"query": "MATCH (n) RETURN n LIMIT 1", "params": {}},
        headers={"X-API-Key": READ_KEY},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "read" and body["count"] == 1
    assert stub_cypher[0]["write"] is False


async def test_write_key_can_also_read(client, keys, stub_cypher):
    r = await client.post(
        "/api/kg/read", json={"query": "RETURN 1"},
        headers={"X-API-Key": WRITE_KEY},
    )
    assert r.status_code == 200
    assert stub_cypher[0]["write"] is False


async def test_write_with_write_key(client, keys, stub_cypher):
    r = await client.post(
        "/api/kg/write", json={"query": "CREATE (n:T) RETURN n", "params": {}},
        headers={"X-API-Key": WRITE_KEY},
    )
    assert r.status_code == 200
    assert r.json()["mode"] == "write"
    assert stub_cypher[0]["write"] is True


# --- error mapping ---------------------------------------------------------- #

async def test_unreachable_kg_maps_to_502(client, keys, monkeypatch):
    async def _down(*a, **k):
        raise kg_mod.KGUnavailable("no route")

    monkeypatch.setattr(kg_mod.kg, "run_cypher", _down)
    r = await client.post(
        "/api/kg/read", json={"query": "RETURN 1"},
        headers={"X-API-Key": READ_KEY},
    )
    assert r.status_code == 502


async def test_bad_cypher_maps_to_400(client, keys, monkeypatch):
    async def _bad(*a, **k):
        raise ClientError("Writing in read access mode not allowed")

    monkeypatch.setattr(kg_mod.kg, "run_cypher", _bad)
    r = await client.post(
        "/api/kg/read", json={"query": "CREATE (n) RETURN n"},
        headers={"X-API-Key": READ_KEY},
    )
    assert r.status_code == 400


# --- structured audit log --------------------------------------------------- #

async def test_read_emits_audit_log(client, keys, stub_cypher):
    with structlog.testing.capture_logs() as logs:
        r = await client.post(
            "/api/kg/read",
            json={"query": "MATCH (n) RETURN n LIMIT 1", "params": {}},
            headers={"X-API-Key": READ_KEY},
        )
    assert r.status_code == 200
    audit = [e for e in logs if e["event"] == "kg_proxy_query"]
    assert len(audit) == 1
    ev = audit[0]
    assert ev["mode"] == "read"
    assert ev["rows"] == 1
    assert ev["truncated"] is False
    assert ev["log_level"] == "info"


async def test_failed_query_emits_audit_log(client, keys, monkeypatch):
    async def _down(*a, **k):
        raise kg_mod.KGUnavailable("no route")

    monkeypatch.setattr(kg_mod.kg, "run_cypher", _down)
    with structlog.testing.capture_logs() as logs:
        r = await client.post(
            "/api/kg/read", json={"query": "RETURN 1"},
            headers={"X-API-Key": READ_KEY},
        )
    assert r.status_code == 502
    failed = [e for e in logs if e["event"] == "kg_proxy_query_failed"]
    assert len(failed) == 1
    assert failed[0]["mode"] == "read"
    assert failed[0]["log_level"] == "warning"
