"""Tests for GET /api/mcp/* — live registry with fail-soft snapshot fallback.

The Mongo store is faked at the service boundary (app.mcp_store.registry);
no Mongo is required, matching the repo's zero-infra test contract.
"""

from __future__ import annotations

import json

import pytest

from app import mcp_store, mcp_vault
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
        "connection": {
            "recipe": "local-tunnel",
            "command": "npx",
            "args": ["-y", "mongodb-mcp-server", "--connectionString",
                     "mongodb://mongo:<MONGO_PASSWORD>@127.0.0.1:37017/"],
        },
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

VAULT_PASSWORD = "312447"
VAULT_PAYLOAD = {"redis": {"urls": ["redis://default:redispassword@127.0.0.1:16379/0"]}}


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

    async def vault():
        return None  # store live, vault not initialized (tests override)

    monkeypatch.setattr(mcp_store.registry, "list_servers", list_servers)
    monkeypatch.setattr(mcp_store.registry, "get_server", get_server)
    monkeypatch.setattr(mcp_store.registry, "manifest_meta", manifest_meta)
    monkeypatch.setattr(mcp_store.registry, "health", health)
    monkeypatch.setattr(mcp_store.registry, "vault", vault)
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
    monkeypatch.setattr(mcp_store.registry, "vault", boom)


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


# --------------------------------------------------------------------------- #
# H-01 — discovery, JSON-LD, capabilities/auth, well-known alias              #
# --------------------------------------------------------------------------- #


async def test_discovery_document(client):
    resp = await client.get("/api/mcp/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "metahumotonic MCP registry"
    paths = {e["path"] for e in body["endpoints"]}
    assert "/api/mcp/manifest" in paths
    assert "/api/mcp/status" in paths
    assert "/api/mcp/vault" in paths
    assert "/.well-known/mcp-servers.json" in paths
    assert body["agent_docs"]["llms_txt"].endswith("/llms.txt")
    assert body["agent_docs"]["mcp_llms_txt"].endswith("/mcp/llms.txt")
    assert body["credential_vault"]["kdf"] == "PBKDF2-SHA256"
    assert "312447" not in json.dumps(body)  # the password is never published


async def test_discovery_needs_no_mongo(client, _broken_registry):
    resp = await client.get("/api/mcp/")
    assert resp.status_code == 200  # static — no store involved


async def test_manifest_carries_jsonld_and_enriched_servers(client):
    resp = await client.get("/api/mcp/manifest")
    body = resp.json()
    assert body["@context"]["schema"] == "https://schema.org/"
    assert "mhb" in body["@context"]
    assert "schema:ItemList" in body["@type"]
    assert body["credential_vault"]["url"].endswith("/api/mcp/vault")
    assert body["credential_vault"]["hint"]
    by_name = {s["name"]: s for s in body["servers"]}
    mongo = by_name["mongodb"]
    assert mongo["@type"] == ["schema:SoftwareApplication", "mhb:McpServer"]
    assert mongo["@id"].endswith("/api/mcp/servers/mongodb")
    assert "query" in mongo["capabilities"]  # document-category default
    # connection args reference <MONGO_PASSWORD> → vault auth with requires
    assert mongo["auth"]["type"] == "vault"
    assert mongo["auth"]["requires"] == ["MONGO_PASSWORD"]
    assert mongo["auth"]["kdf"] == "PBKDF2-SHA256"
    memory = by_name["memory"]
    assert memory["auth"] == {"type": "none"}
    assert memory["capabilities"] == ["tools"]  # utility default


async def test_servers_endpoint_is_enriched_too(client):
    resp = await client.get("/api/mcp/servers")
    by_name = {s["name"]: s for s in resp.json()["servers"]}
    assert by_name["mongodb"]["auth"]["type"] == "vault"
    assert by_name["memory"]["capabilities"] == ["tools"]


async def test_well_known_redirects_to_manifest(client):
    resp = await client.get("/.well-known/mcp-servers.json")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/api/mcp/manifest"
    resp = await client.get("/.well-known/mcp-servers.json", follow_redirects=True)
    assert resp.status_code == 200
    assert resp.json()["schema"] == "metahumotonic/mcp-registry@1"


# --------------------------------------------------------------------------- #
# H-02 — /api/mcp/status (+?format=text)                                      #
# --------------------------------------------------------------------------- #


@pytest.fixture
def _rich_health(monkeypatch):
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    fresh = (now - timedelta(hours=1)).isoformat()
    old = (now - timedelta(days=3)).isoformat()

    async def health():
        return [
            {"name": "redis", "status": "verified", "verified_at": "2026-07-27",
             "last_probe_at": fresh, "notes": "probe ok"},
            {"name": "mongodb", "status": "verified", "verified_at": "2026-07-20",
             "last_probe_at": old, "notes": "probe ok"},
            {"name": "postgres", "status": "available", "verified_at": None,
             "last_probe_at": None, "notes": None},
            {"name": "omd", "status": "unreachable", "verified_at": "2026-07-26",
             "last_probe_at": fresh, "notes": "probe failed: timeout"},
            {"name": "airo-neo4j", "status": "unused", "verified_at": None,
             "last_probe_at": None, "notes": None},
        ]

    monkeypatch.setattr(mcp_store.registry, "health", health)


async def test_status_aggregate(client, _rich_health):
    resp = await client.get("/api/mcp/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "live"
    summary = body["summary"]
    assert summary["total"] == 5
    assert summary["verified"] == 2
    assert summary["available"] == 1
    assert summary["down"] == 1
    assert summary["unused"] == 1
    assert summary["stale"] == 1  # mongodb: verified but probed 3 days ago
    assert body["last_verify_at"]
    by_name = {s["name"]: s for s in body["servers"]}
    assert by_name["redis"]["badge"] == "verified"
    assert by_name["mongodb"]["badge"] == "stale"
    assert by_name["mongodb"]["stale"] is True
    assert by_name["postgres"]["badge"] == "available"
    assert by_name["omd"]["badge"] == "down"
    assert by_name["airo-neo4j"]["badge"] == "unused"
    assert by_name["redis"]["last_check_at"]


async def test_status_text_format(client, _rich_health):
    resp = await client.get("/api/mcp/status?format=text")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    text = resp.text
    head = text.splitlines()[0]
    assert "5 servers" in head
    assert "2 verified (1 stale)" in head
    assert "1 down" in head
    assert "last verify run:" in head
    assert "redis: verified" in text
    assert "mongodb: stale" in text
    assert "omd: down" in text


async def test_status_fail_soft(client, _broken_registry):
    resp = await client.get("/api/mcp/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "snapshot"
    resp = await client.get("/api/mcp/status?format=text")
    assert resp.status_code == 200
    assert "UNAVAILABLE" in resp.text


# --------------------------------------------------------------------------- #
# H-04 — /api/mcp/vault (ciphertext only)                                     #
# --------------------------------------------------------------------------- #


@pytest.fixture
def _live_vault(monkeypatch):
    doc = mcp_vault.encrypt_payload(VAULT_PAYLOAD, VAULT_PASSWORD)

    async def vault():
        return dict(doc)

    monkeypatch.setattr(mcp_store.registry, "vault", vault)


async def test_vault_returns_ciphertext_only(client, _live_vault):
    resp = await client.get("/api/mcp/vault")
    assert resp.status_code == 200
    raw = resp.text
    assert "redispassword" not in raw
    assert VAULT_PASSWORD not in raw
    body = resp.json()
    assert body["source"] == "live"
    vault = body["vault"]
    assert vault["kdf"]["name"] == "PBKDF2-SHA256"
    assert vault["kdf"]["salt"]
    assert vault["blob"]
    assert vault["services"] == ["redis"]
    assert body["unlock"]  # the recipe rides along
    # the blob really decrypts with the registry password (end-to-end)
    assert mcp_vault.decrypt_payload(vault, VAULT_PASSWORD) == VAULT_PAYLOAD
    with pytest.raises(mcp_vault.VaultError):
        mcp_vault.decrypt_payload(vault, "000000")


async def test_vault_404_when_uninitialized(client):
    resp = await client.get("/api/mcp/vault")
    assert resp.status_code == 404


async def test_vault_fail_soft_when_store_down(client, _broken_registry):
    resp = await client.get("/api/mcp/vault")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "snapshot"
    assert body["vault"] is None
