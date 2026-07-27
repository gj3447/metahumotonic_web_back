"""Tests for GET /api/mcp/* — live registry with fail-soft snapshot fallback.

The Mongo store is faked at the service boundary (app.mcp_store.registry);
no Mongo is required, matching the repo's zero-infra test contract.
"""

from __future__ import annotations

import pytest

from app import mcp_store
from app.mcp_store import McpRegistryUnavailable
from app.routers import mcp_registry

SERVERS = [
    {
        "name": "mongodb",
        "description": "MongoDB — document store.",
        "category": "document",
        "transport": "stdio",
        "status": "verified",
        "verified_at": "2026-07-27",
        "connection": {"recipe": "local-tunnel", "command": "npx", "args": []},
    },
    {
        "name": "memory",
        "description": "Knowledge-graph memory.",
        "category": "utility",
        "transport": "stdio",
        "status": "verified",
        "verified_at": "2026-07-27",
        "connection": {"recipe": "local-npx", "command": "npx", "args": []},
    },
]

META = {
    "schema": "metahumotonic/mcp-registry@1",
    "site": "https://metahumotonic.com",
    "notes": ["note a"],
    "updated": "2026-07-27",
}


@pytest.fixture(autouse=True)
def _fake_registry(monkeypatch):
    """Fresh fake store + cleared router cache per test."""
    mcp_registry._cache.clear()

    async def list_servers():
        return sorted(SERVERS, key=lambda s: s["name"])  # store sorts by name

    async def get_server(name):
        return next((dict(s) for s in SERVERS if s["name"] == name), None)

    async def manifest_meta():
        return dict(META)

    async def health():
        return [
            {
                "name": s["name"],
                "status": s["status"],
                "verified_at": s["verified_at"],
            }
            for s in SERVERS
        ]

    monkeypatch.setattr(mcp_store.registry, "list_servers", list_servers)
    monkeypatch.setattr(mcp_store.registry, "get_server", get_server)
    monkeypatch.setattr(mcp_store.registry, "manifest_meta", manifest_meta)
    monkeypatch.setattr(mcp_store.registry, "health", health)
    yield
    mcp_registry._cache.clear()


@pytest.fixture
def _broken_registry(monkeypatch):
    async def boom(*_a, **_kw):
        raise McpRegistryUnavailable("mongo registry unavailable")

    monkeypatch.setattr(mcp_store.registry, "list_servers", boom)
    monkeypatch.setattr(mcp_store.registry, "get_server", boom)
    monkeypatch.setattr(mcp_store.registry, "manifest_meta", boom)
    monkeypatch.setattr(mcp_store.registry, "health", boom)


async def test_servers_live(client):
    resp = await client.get("/api/mcp/servers")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "live"
    assert body["count"] == 2
    assert [s["name"] for s in body["servers"]] == ["memory", "mongodb"]
    assert body["schema"] == "metahumotonic/mcp-registry@1"


async def test_servers_fail_soft_when_mongo_down(client, _broken_registry):
    resp = await client.get("/api/mcp/servers")
    assert resp.status_code == 200  # never 500
    body = resp.json()
    assert body["source"] == "snapshot"
    assert body["servers"] == []


async def test_get_server_found(client):
    resp = await client.get("/api/mcp/servers/mongodb")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "live"
    assert body["server"]["name"] == "mongodb"
    assert body["server"]["connection"]["recipe"] == "local-tunnel"


async def test_get_server_not_found(client):
    resp = await client.get("/api/mcp/servers/nope")
    assert resp.status_code == 404


async def test_get_server_fail_soft(client, _broken_registry):
    resp = await client.get("/api/mcp/servers/mongodb")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "snapshot"
    assert body["server"] is None


async def test_manifest_live(client):
    resp = await client.get("/api/mcp/manifest")
    assert resp.status_code == 200
    body = resp.json()
    assert body["schema"] == "metahumotonic/mcp-registry@1"
    assert body["source"] == "live"
    assert body["updated"] == "2026-07-27"
    assert body["site"] == "https://metahumotonic.com"
    assert body["notes"] == ["note a"]
    assert len(body["servers"]) == 2


async def test_manifest_fail_soft(client, _broken_registry):
    resp = await client.get("/api/mcp/manifest")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "snapshot"
    assert body["servers"] == []
    assert body["schema"] == "metahumotonic/mcp-registry@1"


async def test_health_live(client):
    resp = await client.get("/api/mcp/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "live"
    assert body["count"] == 2
    by_name = {s["name"]: s for s in body["servers"]}
    assert by_name["mongodb"]["status"] == "verified"
    assert by_name["mongodb"]["verified_at"] == "2026-07-27"


async def test_health_fail_soft(client, _broken_registry):
    resp = await client.get("/api/mcp/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "snapshot"
    assert body["servers"] == []


async def test_servers_are_cached(client, monkeypatch):
    calls = 0

    async def counting():
        nonlocal calls
        calls += 1
        return list(SERVERS)

    mcp_registry._cache.clear()
    monkeypatch.setattr(mcp_store.registry, "list_servers", counting)
    await client.get("/api/mcp/servers")
    await client.get("/api/mcp/servers")
    assert calls == 1  # second hit served from the TTL cache
