"""GET /api/mcp/* — live MCP registry (read-only public surface).

Replaces the static ``/mcp/manifest.json`` with a Mongo-backed live registry.
Every endpoint is cached (~5 min) and fail-soft: a Mongo outage yields a
``source: "snapshot"`` empty payload, never a 500. Writes are CLI-only
(``mhb-mcp``) — this router exposes no mutation surface, and responses carry
manifest-level information only (no secrets, no private topology beyond what
the public manifest already documents).
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from fastapi import APIRouter, HTTPException

from ..cache import TTLCache
from ..config import settings
from ..mcp_store import McpRegistryUnavailable, registry

router = APIRouter(prefix="/api/mcp")

SCHEMA = "metahumotonic/mcp-registry@1"
SITE = "https://metahumotonic.com"

_cache = TTLCache(settings.mcp_registry_cache_ttl_seconds, max_entries=64)


async def _cached(key: str, producer: Callable[[], Awaitable[Any]], fallback: Any) -> Any:
    """Cache successful reads; on Mongo outage serve the snapshot fallback."""
    try:
        return await _cache.get_or_set(key, producer)
    except McpRegistryUnavailable:
        return fallback


async def _servers() -> list[dict[str, Any]]:
    return await registry.list_servers()


@router.get("/servers")
async def list_servers() -> dict[str, Any]:
    """All registered MCP servers (manifest-level fields), name-sorted."""
    async def produce() -> dict[str, Any]:
        servers = await _servers()
        return {
            "schema": SCHEMA,
            "source": "live",
            "count": len(servers),
            "servers": servers,
        }

    return await _cached(
        "servers", produce, {"schema": SCHEMA, "source": "snapshot", "count": 0, "servers": []}
    )


@router.get("/servers/{name}")
async def get_server(name: str) -> dict[str, Any]:
    """One registry entry by name. 404 when the store is live but has no such
    server; a snapshot fallback (200, ``server: null``) when the store is down."""

    async def produce() -> dict[str, Any]:
        doc = await registry.get_server(name)
        if doc is None:
            raise HTTPException(status_code=404, detail=f"mcp server not found: {name}")
        return {"schema": SCHEMA, "source": "live", "server": doc}

    return await _cached(
        f"server:{name}", produce, {"schema": SCHEMA, "source": "snapshot", "server": None}
    )


@router.get("/manifest")
async def get_manifest() -> dict[str, Any]:
    """Live ``metahumotonic/mcp-registry@1`` manifest — drop-in replacement
    for the static /mcp/manifest.json (kept as a client-side fallback)."""

    async def produce() -> dict[str, Any]:
        servers, meta = await _servers(), await registry.manifest_meta()
        updated = meta.get("updated") or max(
            (s.get("verified_at") or "" for s in servers), default=""
        )
        return {
            "schema": SCHEMA,
            "updated": updated,
            "site": meta.get("site", SITE),
            "notes": meta.get("notes", []),
            "source": "live",
            "servers": servers,
        }

    return await _cached(
        "manifest",
        produce,
        {
            "schema": SCHEMA,
            "updated": "",
            "site": SITE,
            "notes": [],
            "source": "snapshot",
            "servers": [],
        },
    )


@router.get("/health")
async def get_health() -> dict[str, Any]:
    """Latest verify result per server (probe outcome + timestamps only)."""

    async def produce() -> dict[str, Any]:
        items = await registry.health()
        return {"schema": SCHEMA, "source": "live", "count": len(items), "servers": items}

    return await _cached(
        "health", produce, {"schema": SCHEMA, "source": "snapshot", "count": 0, "servers": []}
    )
