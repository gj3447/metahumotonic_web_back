"""Shared manifest builder for the MCP registry (H-01 semantic/AI-friendliness).

Used by BOTH the HTTP router (live ``/api/mcp/manifest``) and the CLI
(``mhb-mcp export`` → the static-site fallback ``/mcp/manifest.json``) so the
two never drift: JSON-LD context, per-server ``capabilities`` / ``auth``
enrichment, and the credential-vault unlock spec all come from here.

Everything is rule-derived from the stored registry documents — no server name
is hardcoded. A server with explicit ``capabilities`` / ``auth`` fields in
Mongo keeps them; everyone else gets the category-derived defaults below.
"""

from __future__ import annotations

import re
from typing import Any

SCHEMA = "metahumotonic/mcp-registry@1"
SITE = "https://metahumotonic.com"

# JSON-LD: schema.org + a small custom vocabulary under the site's own /mcp
# namespace (H-01: "@context schema.org + 커스텀 vocab").
JSONLD_CONTEXT = {
    "schema": "https://schema.org/",
    "mhb": "https://metahumotonic.com/mcp/ontology#",
}
MANIFEST_TYPES = ["schema:ItemList", "mhb:McpRegistry"]
SERVER_TYPES = ["schema:SoftwareApplication", "mhb:McpServer"]

# Default capability hints per registry category — the kinds of tools an MCP
# server of that category typically exposes. Explicit per-server
# ``capabilities`` in Mongo always win over these.
CATEGORY_CAPABILITIES: dict[str, list[str]] = {
    "graph": ["cypher.read", "cypher.write", "schema.inspect"],
    "vector": ["kv.crud", "vector.search", "json.document", "streams", "pubsub"],
    "document": ["document.crud", "query", "aggregation", "index.admin"],
    "storage": ["bucket.admin", "object.read", "object.write", "presigned-url"],
    "utility": ["tools"],
}
DEFAULT_CAPABILITIES = ["tools"]

# The public, static description of the credential vault (H-04). Ciphertext
# lives at VAULT_SPEC["url"]; the password never leaves the user.
VAULT_SPEC: dict[str, Any] = {
    "url": f"{SITE}/api/mcp/vault",
    "kdf": "PBKDF2-SHA256",
    "cipher": "fernet",
    "hint": "Ask the user for the registry password (6 digits).",
    "unlock": [
        "GET /api/mcp/vault → {kdf: {iterations, salt (base64)}, blob}",
        "key = base64url(PBKDF2-HMAC-SHA256(password, base64decode(kdf.salt), kdf.iterations, dklen=32))",
        "credentials = JSON.parse(Fernet(key).decrypt(blob)) → {service: {...}}",
    ],
    "cli": "mhb-mcp vault unlock --password <registry-password>",
    "rotate": "mhb-mcp vault init --password <new-password> --file seed.json — re-encrypts with a fresh salt and atomically replaces the blob; the old password stops working immediately.",
}

_PLACEHOLDER_RE = re.compile(r"<([A-Z][A-Z0-9_]*)>")


def _placeholder_names(connection: dict[str, Any]) -> list[str]:
    """Secret placeholders (``<REDIS_PASSWORD>`` etc.) referenced by a
    connection recipe — env values and arg strings."""
    names: set[str] = set()
    env = connection.get("env") or {}
    for value in env.values():
        names.update(_PLACEHOLDER_RE.findall(str(value)))
    for arg in connection.get("args") or []:
        names.update(_PLACEHOLDER_RE.findall(str(arg)))
    url = connection.get("url")
    if url:
        names.update(_PLACEHOLDER_RE.findall(str(url)))
    return sorted(names)


def capabilities_for(server: dict[str, Any]) -> list[str]:
    """Tool kinds this MCP provides — explicit field wins, category default
    otherwise (rule-derived, never name-hardcoded)."""
    explicit = server.get("capabilities")
    if isinstance(explicit, list) and explicit:
        return explicit
    return list(CATEGORY_CAPABILITIES.get(server.get("category") or "", DEFAULT_CAPABILITIES))


def auth_for(server: dict[str, Any]) -> dict[str, Any]:
    """Credential contract for one server.

    - explicit ``auth`` in Mongo wins;
    - a connection recipe with ``<PLACEHOLDER>`` secrets → registry vault:
      the agent asks the user for the registry password, fetches
      ``/api/mcp/vault`` and decrypts locally (PBKDF2 → Fernet);
    - otherwise the server needs no credentials."""
    explicit = server.get("auth")
    if isinstance(explicit, dict) and explicit:
        return explicit
    requires = _placeholder_names(server.get("connection") or {})
    if requires:
        return {
            "type": "vault",
            "vault": VAULT_SPEC["url"],
            "kdf": VAULT_SPEC["kdf"],
            "cipher": VAULT_SPEC["cipher"],
            "hint": VAULT_SPEC["hint"],
            "requires": requires,
        }
    return {"type": "none"}


def enrich_server(server: dict[str, Any]) -> dict[str, Any]:
    """One manifest entry + JSON-LD typing + capabilities/auth."""
    name = server.get("name", "")
    return {
        "@type": SERVER_TYPES,
        "@id": f"{SITE}/api/mcp/servers/{name}",
        **server,
        "capabilities": capabilities_for(server),
        "auth": auth_for(server),
    }


def build_manifest(
    servers: list[dict[str, Any]], meta: dict[str, Any], *, updated: str = ""
) -> dict[str, Any]:
    """The canonical ``metahumotonic/mcp-registry@1`` manifest payload
    (without the router-only ``source`` field — the caller adds it)."""
    resolved_updated = updated or meta.get("updated") or max(
        (s.get("verified_at") or "" for s in servers), default=""
    )
    return {
        "@context": JSONLD_CONTEXT,
        "@type": MANIFEST_TYPES,
        "schema": SCHEMA,
        "updated": resolved_updated,
        "site": meta.get("site", SITE),
        "notes": meta.get("notes", []),
        "credential_vault": VAULT_SPEC,
        "servers": [enrich_server(s) for s in servers],
    }
