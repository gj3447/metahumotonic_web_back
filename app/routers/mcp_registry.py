"""GET /api/mcp/* — live MCP registry (read-only public surface).

Replaces the static ``/mcp/manifest.json`` with a Mongo-backed live registry.
Every endpoint is cached (~5 min) and fail-soft: a Mongo outage yields a
``source: "snapshot"`` empty payload, never a 500. Writes are CLI-only
(``mhb-mcp``) — this router exposes no mutation surface, and responses carry
manifest-level information only (no secrets, no private topology beyond what
the public manifest already documents).

Endpoints:

- ``GET /api/mcp/``                — discovery document (endpoints + schemas
  + usage examples + agent docs); static, needs no Mongo.
- ``GET /api/mcp/servers``         — all entries (name-sorted).
- ``GET /api/mcp/servers/{name}``  — one entry.
- ``GET /api/mcp/manifest``        — canonical manifest with JSON-LD,
  per-server capabilities/auth, and the credential-vault spec.
- ``GET /api/mcp/health``          — latest verify result per server.
- ``GET /api/mcp/status``          — health + aggregate counts + stale flags;
  ``?format=text`` returns a one-line-per-server plain-text summary for agents.
- ``GET /api/mcp/vault``           — the credential vault (ciphertext only).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from ..cache import TTLCache
from ..config import settings
from ..mcp_manifest import SCHEMA, SITE, VAULT_SPEC, build_manifest, enrich_server
from ..mcp_store import McpRegistryUnavailable, registry
from ..mcp_vault import public_view

router = APIRouter(prefix="/api/mcp")

# A verified server whose last probe is older than this is reported "stale".
STALE_AFTER = timedelta(hours=24)

_cache = TTLCache(settings.mcp_registry_cache_ttl_seconds, max_entries=64)


async def _cached(key: str, producer: Callable[[], Awaitable[Any]], fallback: Any) -> Any:
    """Cache successful reads; on Mongo outage serve the snapshot fallback."""
    try:
        return await _cache.get_or_set(key, producer)
    except McpRegistryUnavailable:
        return fallback


async def _servers() -> list[dict[str, Any]]:
    return await registry.list_servers()


# --------------------------------------------------------------------------- #
# Discovery — static, always available (no Mongo needed)                      #
# --------------------------------------------------------------------------- #


def _discovery() -> dict[str, Any]:
    """The agent-facing index of everything this registry offers."""
    return {
        "schema": SCHEMA,
        "source": "live",
        "name": "metahumotonic MCP registry",
        "description": (
            "Live registry of the Model Context Protocol servers used across the "
            "metahumotonic infrastructure. Read-only HTTP; writes happen via the "
            "mhb-mcp operator CLI (mhb-mcp import / upsert)."
        ),
        "dashboard": f"{SITE}/mcp/",
        "endpoints": [
            {
                "path": "/api/mcp/manifest",
                "method": "GET",
                "description": "Canonical metahumotonic/mcp-registry@1 manifest — JSON-LD typed, per-server capabilities + auth.",
                "example": f"curl -s {SITE}/api/mcp/manifest",
            },
            {
                "path": "/api/mcp/servers",
                "method": "GET",
                "description": "All registered servers (manifest-level fields, name-sorted).",
                "example": f"curl -s {SITE}/api/mcp/servers",
            },
            {
                "path": "/api/mcp/servers/{name}",
                "method": "GET",
                "description": "One registry entry by name (404 when unknown).",
                "example": f"curl -s {SITE}/api/mcp/servers/redis",
            },
            {
                "path": "/api/mcp/health",
                "method": "GET",
                "description": "Latest verify result per server (status/verified_at/last_probe_at).",
                "example": f"curl -s {SITE}/api/mcp/health",
            },
            {
                "path": "/api/mcp/status",
                "method": "GET",
                "description": "Health + aggregate counts (verified/available/down/unused/stale) + last verify run time. ?format=text returns a plain-text summary.",
                "example": f"curl -s '{SITE}/api/mcp/status?format=text'",
            },
            {
                "path": "/api/mcp/vault",
                "method": "GET",
                "description": "Credential vault — PBKDF2→Fernet ciphertext only, never plaintext. See credential_vault for the unlock recipe.",
                "example": f"curl -s {SITE}/api/mcp/vault",
            },
            {
                "path": "/.well-known/mcp-servers.json",
                "method": "GET",
                "description": "Well-known discovery alias — 302 redirect to /api/mcp/manifest.",
                "example": f"curl -sL {SITE}/.well-known/mcp-servers.json",
            },
        ],
        "schemas": {
            "manifest": {
                "id": SCHEMA,
                "fields": [
                    "@context", "@type", "schema", "updated", "site", "notes",
                    "credential_vault", "servers",
                ],
            },
            "server": {
                "fields": [
                    "name", "description", "category", "transport", "status",
                    "backend", "connection", "capabilities", "auth",
                    "verified_at", "last_probe_at",
                ],
                "status": [
                    "verified (live-checked at verified_at)",
                    "available (configured, not currently loaded)",
                    "unused (defined, not in active use)",
                    "unreachable (last probe failed)",
                ],
                "connection.recipe": ["local-npx", "local-command", "local-tunnel", "ssh-stdio", "http"],
            },
        },
        "agent_docs": {
            "llms_txt": f"{SITE}/llms.txt",
            "mcp_llms_txt": f"{SITE}/mcp/llms.txt",
            "well_known": f"{SITE}/.well-known/mcp-servers.json",
        },
        "credential_vault": VAULT_SPEC,
        "write_access": (
            "This HTTP API is read-only. New servers are added with the operator "
            "CLI: `mhb-mcp import <path-to-.mcp.json>` (bulk, from a standard MCP "
            "client config) or `mhb-mcp upsert <name> --set key=value`."
        ),
    }


@router.get("")
@router.get("/", include_in_schema=False)
async def get_discovery() -> dict[str, Any]:
    """Discovery document — endpoint list + schemas + usage examples."""
    return _discovery()


# --------------------------------------------------------------------------- #
# Servers / manifest                                                          #
# --------------------------------------------------------------------------- #


@router.get("/servers")
async def list_servers() -> dict[str, Any]:
    """All registered MCP servers (manifest-level fields), name-sorted."""
    async def produce() -> dict[str, Any]:
        servers = await _servers()
        return {
            "schema": SCHEMA,
            "source": "live",
            "count": len(servers),
            "servers": [enrich_server(s) for s in servers],
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
        return {"schema": SCHEMA, "source": "live", "server": enrich_server(doc)}

    return await _cached(
        f"server:{name}", produce, {"schema": SCHEMA, "source": "snapshot", "server": None}
    )


@router.get("/manifest")
async def get_manifest() -> dict[str, Any]:
    """Live ``metahumotonic/mcp-registry@1`` manifest — drop-in replacement
    for the static /mcp/manifest.json (kept as a client-side fallback).
    JSON-LD typed; each server carries ``capabilities`` and ``auth``; the
    top-level ``credential_vault`` documents the unlock recipe."""

    async def produce() -> dict[str, Any]:
        servers, meta = await _servers(), await registry.manifest_meta()
        return {**build_manifest(servers, meta), "source": "live"}

    return await _cached(
        "manifest",
        produce,
        {
            **build_manifest([], {}),
            "source": "snapshot",
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


# --------------------------------------------------------------------------- #
# Status — aggregate health for dashboards (JSON) and agents (text)           #
# --------------------------------------------------------------------------- #


def _parse_ts(value: Any) -> datetime | None:
    """Accept ISO datetimes ('...Z' ok) and plain dates ('YYYY-MM-DD')."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:
        from datetime import date, time

        return datetime.combine(date.fromisoformat(text), time.min, tzinfo=timezone.utc)
    except ValueError:
        return None


def _status_payload(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Health rows → status payload with per-server badge + aggregate counts."""
    now = datetime.now(timezone.utc)
    servers: list[dict[str, Any]] = []
    counts = {"verified": 0, "available": 0, "down": 0, "unused": 0}
    stale_count = 0
    last_verify: datetime | None = None
    for item in sorted(items, key=lambda s: s.get("name", "")):
        status = item.get("status") or "unknown"
        probe_at = _parse_ts(item.get("last_probe_at"))
        verified_at = _parse_ts(item.get("verified_at"))
        last_check = probe_at or verified_at
        # stale = not (successfully or unsuccessfully) re-checked within 24h
        stale = last_check is None or (now - last_check) > STALE_AFTER
        if probe_at and (last_verify is None or probe_at > last_verify):
            last_verify = probe_at
        if status == "unreachable":
            badge = "down"
            counts["down"] += 1
        elif status == "verified":
            counts["verified"] += 1
            if stale:
                badge = "stale"
                stale_count += 1
            else:
                badge = "verified"
        elif status == "unused":
            badge = "unused"
            counts["unused"] += 1
        elif status == "available":
            badge = "available"
            counts["available"] += 1
        else:
            badge = status
        servers.append(
            {
                "name": item.get("name"),
                "status": status,
                "badge": badge,
                "stale": stale,
                "verified_at": item.get("verified_at"),
                "last_probe_at": item.get("last_probe_at"),
                "last_check_at": last_check.isoformat() if last_check else None,
                "notes": item.get("notes"),
            }
        )
    return {
        "schema": SCHEMA,
        "source": "live",
        "generated_at": now.isoformat(),
        "last_verify_at": last_verify.isoformat() if last_verify else None,
        "stale_after_hours": int(STALE_AFTER.total_seconds() // 3600),
        "summary": {
            "total": len(servers),
            "verified": counts["verified"],
            "available": counts["available"],
            "down": counts["down"],
            "unused": counts["unused"],
            "stale": stale_count,
        },
        "servers": servers,
    }


def _status_text(payload: dict[str, Any]) -> str:
    """One header line + one line per server — agent-readable plain text."""
    if payload.get("source") != "live":
        return (
            "metahumotonic MCP registry status UNAVAILABLE "
            "(snapshot fallback — registry store unreachable)\n"
        )
    s = payload["summary"]
    lines = [
        "metahumotonic MCP registry status @ {ts} — {total} servers: "
        "{verified} verified ({stale} stale), {available} available, "
        "{down} down, {unused} unused; last verify run: {last}".format(
            ts=payload["generated_at"],
            total=s["total"],
            verified=s["verified"],
            stale=s["stale"],
            available=s["available"],
            down=s["down"],
            unused=s["unused"],
            last=payload.get("last_verify_at") or "never",
        )
    ]
    for srv in payload["servers"]:
        badge = srv["badge"]
        when = srv.get("last_check_at") or "never"
        extra = ""
        if badge == "stale":
            extra = f" (verified {srv.get('verified_at') or '?'}, not re-checked within 24h)"
        elif badge == "down" and srv.get("notes"):
            extra = f" ({srv['notes'][:120]})"
        lines.append(f"{srv['name']}: {badge}, last check {when}{extra}")
    return "\n".join(lines) + "\n"


@router.get("/status", response_model=None)
async def get_status(request: Request) -> Any:
    """Live status: per-server badge (verified/stale/available/down/unused) +
    aggregate counts + last verify run time. ``?format=text`` → plain text."""

    async def produce() -> dict[str, Any]:
        return _status_payload(await registry.health())

    payload = await _cached(
        "status",
        produce,
        {"schema": SCHEMA, "source": "snapshot", "summary": None, "servers": []},
    )
    if request.query_params.get("format") == "text":
        return PlainTextResponse(_status_text(payload))
    return payload


# --------------------------------------------------------------------------- #
# Credential vault — ciphertext only (H-04)                                   #
# --------------------------------------------------------------------------- #


@router.get("/vault")
async def get_vault() -> dict[str, Any]:
    """The credential vault: PBKDF2-SHA256 KDF params + Fernet blob. Never
    plaintext — decrypt locally with the registry password (see manifest
    ``credential_vault`` or ``mhb-mcp vault unlock``). 404 when the store is
    live but the vault was never initialized."""

    async def produce() -> dict[str, Any]:
        doc = await registry.vault()
        if doc is None:
            raise HTTPException(
                status_code=404,
                detail="credential vault not initialized (run: mhb-mcp vault init)",
            )
        return {
            "schema": SCHEMA,
            "source": "live",
            "hint": VAULT_SPEC["hint"],
            "unlock": VAULT_SPEC["unlock"],
            "vault": public_view(doc),
        }

    return await _cached(
        "vault",
        produce,
        {"schema": SCHEMA, "source": "snapshot", "vault": None},
    )
