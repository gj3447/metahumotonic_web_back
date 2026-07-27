"""mhb-mcp — operator CLI for the Mongo-backed MCP registry.

Solid, scriptable, AI-friendly: every command prints plain lines or JSON and
uses exit codes (0 ok, 1 error/probe failure). All writes to the registry
happen here; the HTTP API is read-only.

Mongo target resolution (first match wins):

1. ``--mongo-uri`` flag
2. ``MHB_MONGO_URI`` env
3. macbook local tunnel default, when ``MONGO_PASSWORD`` is set:
   ``mongodb://mongo:$MONGO_PASSWORD@127.0.0.1:37017/?authSource=admin&directConnection=true``

No secret is ever stored in the repo — URIs and passwords come from the
environment only. Database: ``MHB_MONGO_DB`` (default ``metahumotonic``),
collection ``mcp_servers``.

Usage:

    mhb-mcp seed manifest.json         # upsert all servers + manifest meta
    mhb-mcp list [--json]              # name/status table or full JSON
    mhb-mcp show NAME                  # one entry as JSON
    mhb-mcp upsert NAME [--set k=v ...] | [--file server.json]
    mhb-mcp remove NAME
    mhb-mcp import .mcp.json           # bulk-import a standard MCP client config
    mhb-mcp verify [NAME]              # probe reachability, record results
    mhb-mcp export [--out manifest.json]
    mhb-mcp vault init --password PW [--file seed.json] [--from-mcp-json .mcp.json]
                       [--mc-config config.json --mc-alias bhgman] [--set svc.key=v]
    mhb-mcp vault unlock --password PW # decrypt + print credentials (local check)
    mhb-mcp vault show                 # vault metadata (ciphertext summary only)

Vault password: ``--password`` flag or ``MHB_VAULT_PASSWORD`` env. The vault
stores PBKDF2-SHA256→Fernet ciphertext only; see app/mcp_vault.py.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any

from . import mcp_vault
from .mcp_manifest import SCHEMA, SITE, build_manifest

DB_DEFAULT = "metahumotonic"
COLLECTION = "mcp_servers"
META_ID = "manifest_meta"

# Fields kept in Mongo for operations but stripped from exported manifests
# (they are registry bookkeeping, not manifest schema).
_EXPORT_DROP = {"kind", "_id", "updated_at", "last_probe_at"}


class CliError(RuntimeError):
    """User-facing failure — printed to stderr, exit code 1."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today() -> str:
    return _now().date().isoformat()


def resolve_mongo_uri(flag: str = "") -> str:
    """Resolve the target Mongo URI without ever persisting secrets."""
    if flag:
        return flag
    env_uri = os.environ.get("MHB_MONGO_URI", "").strip()
    if env_uri:
        return env_uri
    password = os.environ.get("MONGO_PASSWORD", "")
    if password:
        return (
            f"mongodb://mongo:{password}@127.0.0.1:37017/"
            "?authSource=admin&directConnection=true"
        )
    raise CliError(
        "no Mongo target: set MHB_MONGO_URI, pass --mongo-uri, or set "
        "MONGO_PASSWORD for the macbook tunnel default (127.0.0.1:37017)"
    )


def get_collection(uri: str):
    """Build a motor collection handle (lazy — connects on first operation)."""
    from motor.motor_asyncio import AsyncIOMotorClient

    db = os.environ.get("MHB_MONGO_DB", DB_DEFAULT)
    return AsyncIOMotorClient(uri)[db][COLLECTION]


async def _all_servers(col) -> list[dict[str, Any]]:
    docs = [doc async for doc in col.find({"kind": "server"})]
    return sorted(docs, key=lambda d: d.get("name", ""))


async def _touch_meta_updated(col) -> None:
    await col.update_one(
        {"_id": META_ID}, {"$set": {"updated": _today()}}, upsert=True
    )


def _public(doc: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in doc.items() if k not in ("_id", "kind")}


def _parse_value(raw: str) -> Any:
    """--set values: JSON when it parses (numbers, lists, objects, quoted
    strings), otherwise the plain string."""
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


def _read_json(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as e:
        raise CliError(f"cannot read {path}: {e}") from e


# --------------------------------------------------------------------------- #
# Commands (each takes the parsed args + a collection handle)                  #
# --------------------------------------------------------------------------- #


async def cmd_seed(args, col) -> int:
    """Upsert every server from a manifest file + store manifest-level meta."""
    manifest = _read_json(args.manifest)
    servers = manifest.get("servers") if isinstance(manifest, dict) else None
    if not isinstance(servers, list) or not servers:
        raise CliError("manifest has no non-empty 'servers' list")

    await col.create_index(
        "name",
        name="mcp_server_name",
        unique=True,
        partialFilterExpression={"kind": "server"},
    )
    now = _now()
    for entry in servers:
        name = entry.get("name")
        if not name:
            raise CliError(f"server entry without a name: {entry!r}")
        doc = {**entry, "kind": "server", "updated_at": now}
        await col.update_one({"kind": "server", "name": name}, {"$set": doc}, upsert=True)
    await col.update_one(
        {"_id": META_ID},
        {
            "$set": {
                "kind": "meta",
                "schema": manifest.get("schema", SCHEMA),
                "site": manifest.get("site", SITE),
                "notes": manifest.get("notes", []),
                "updated": manifest.get("updated", _today()),
            }
        },
        upsert=True,
    )
    print(f"seeded {len(servers)} servers into {col.database.name}.{col.name}")
    return 0


async def cmd_list(args, col) -> int:
    servers = [ _public(d) for d in await _all_servers(col) ]
    if args.json:
        print(json.dumps(servers, indent=2, default=str, ensure_ascii=False))
        return 0
    if not servers:
        print("(registry empty — run: mhb-mcp seed manifest.json)")
        return 0
    print(f"{'NAME':<22}{'STATUS':<14}{'VERIFIED_AT':<14}{'CATEGORY':<14}DESCRIPTION")
    for s in servers:
        desc = (s.get("description") or "")[:60]
        print(
            f"{s.get('name',''):<22}{(s.get('status') or '-'):<14}"
            f"{(s.get('verified_at') or '-'):<14}{(s.get('category') or '-'):<14}{desc}"
        )
    return 0


async def cmd_show(args, col) -> int:
    doc = await col.find_one({"kind": "server", "name": args.name})
    if doc is None:
        raise CliError(f"no such server: {args.name}")
    print(json.dumps(_public(doc), indent=2, default=str, ensure_ascii=False))
    return 0


async def cmd_upsert(args, col) -> int:
    """Create or update one entry — from --set pairs or a JSON file (merged)."""
    existing = await col.find_one({"kind": "server", "name": args.name}) or {}
    doc = _public(existing)
    if args.file:
        payload = _read_json(args.file)
        if not isinstance(payload, dict):
            raise CliError("upsert file must contain a JSON object")
        doc.update(payload)
    for pair in args.set or []:
        if "=" not in pair:
            raise CliError(f"--set expects key=value, got: {pair!r}")
        key, raw = pair.split("=", 1)
        if not key:
            raise CliError(f"--set expects key=value, got: {pair!r}")
        doc[key] = _parse_value(raw)
    doc["name"] = args.name
    doc.setdefault("status", "available")
    doc.update({"kind": "server", "updated_at": _now()})
    await col.update_one(
        {"kind": "server", "name": args.name}, {"$set": doc}, upsert=True
    )
    await _touch_meta_updated(col)
    verb = "updated" if existing else "created"
    print(f"{verb} {args.name} (status={doc.get('status')})")
    return 0


async def cmd_remove(args, col) -> int:
    result = await col.delete_one({"kind": "server", "name": args.name})
    if not result.deleted_count:
        raise CliError(f"no such server: {args.name}")
    await _touch_meta_updated(col)
    print(f"removed {args.name}")
    return 0


# --------------------------------------------------------------------------- #
# import — standard MCP client config (.mcp.json) → registry entries          #
# --------------------------------------------------------------------------- #

# env/arg keys whose VALUES are secrets → placeholderized in the registry and
# captured into the vault seed. Companion keys (URIs, usernames) are captured
# into the vault too, so an unlocked vault yields complete working credentials.
_SECRET_KEY_RE = re.compile(r"PASS|TOKEN|SECRET|KEY|CREDENTIAL", re.IGNORECASE)
_COMPANION_KEY_RE = re.compile(r"USER|URI|URL|DATABASE|ENDPOINT|HOST", re.IGNORECASE)
# scheme://user:password@ → the password group is replaced with a placeholder.
_URI_CRED_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*)://([^/\s:@]+):([^@/\s]+)@")
_KEYVAL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)=(\S+)")
_TUNNEL_RE = re.compile(r"(?:127\.0\.0\.1|localhost):(\d{2,5})")

# scheme → placeholder stem, matching the curated manifest's convention
# (mongodb://…:<MONGO_PASSWORD>@, postgresql://…:<PG_PASSWORD>@, redis://…:<REDIS_PASSWORD>@).
_SCHEME_PLACEHOLDER = {
    "mongodb": "MONGO",
    "mongo": "MONGO",
    "postgresql": "PG",
    "postgres": "PG",
    "redis": "REDIS",
    "rediss": "REDIS",
}

# name-substring → category, first match wins (H-03: 이름 기반 자동 추론).
_CATEGORY_RULES = [
    ("neo4j", "graph"),
    ("redis", "vector"),
    ("valkey", "vector"),
    ("minio", "storage"),
    ("aistor", "storage"),
    ("s3", "storage"),
    ("mongo", "document"),
    ("postgres", "document"),
    ("pgsql", "document"),
    ("mysql", "document"),
    ("mariadb", "document"),
    ("sqlite", "document"),
]

_LOCAL_RUNNERS = {"npx", "uvx", "uv", "node", "deno", "bun"}
_SSH_OPT_WITH_VALUE = {"-b", "-c", "-D", "-E", "-F", "-i", "-J", "-L", "-l",
                       "-m", "-o", "-p", "-Q", "-R", "-S", "-W", "-w"}


def _pw_placeholder(scheme: str) -> str:
    stem = _SCHEME_PLACEHOLDER.get(scheme.lower(), scheme.upper().replace("-", "_"))
    return f"<{stem}_PASSWORD>"


def _redact_uri_passwords(text: str) -> str:
    return _URI_CRED_RE.sub(
        lambda m: f"{m.group(1)}://{m.group(2)}:{_pw_placeholder(m.group(1))}@",
        text,
    )


def _sanitize_arg(arg: Any) -> str:
    """One args element: URI passwords → scheme placeholders; KEY=value with a
    secret-looking KEY → KEY=<KEY>. Non-secret values pass through."""
    text = _redact_uri_passwords(str(arg))
    return _KEYVAL_RE.sub(
        lambda m: f"{m.group(1)}=<{m.group(1)}>" if _SECRET_KEY_RE.search(m.group(1)) else m.group(0),
        text,
    )


def _sanitize_env(env: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in env.items():
        if not isinstance(value, str):
            out[key] = value
        elif _SECRET_KEY_RE.search(key):
            out[key] = f"<{key}>"
        else:
            out[key] = _redact_uri_passwords(value)
    return out


def infer_category(name: str) -> str:
    lowered = name.lower()
    for needle, category in _CATEGORY_RULES:
        if needle in lowered:
            return category
    return "utility"


def _tunnel_port(text: str) -> int | None:
    match = _TUNNEL_RE.search(text or "")
    return int(match.group(1)) if match else None


def _ssh_alias_and_command(args: list[str]) -> tuple[str, str]:
    """First non-option token is the alias; the rest is the remote command."""
    i = 0
    while i < len(args):
        token = args[i]
        if token in _SSH_OPT_WITH_VALUE:
            i += 2
            continue
        if token.startswith("-"):
            i += 1
            continue
        break
    alias = args[i] if i < len(args) else ""
    remote = " ".join(args[i + 1:]) if i + 1 <= len(args) else ""
    return alias, remote


def _is_http_config(cfg: dict[str, Any]) -> bool:
    kind = str(cfg.get("type") or cfg.get("transport") or "").lower()
    return bool(cfg.get("url")) or kind in {"http", "sse", "streamable-http"}


def _infer_connection(name: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """A standard .mcp.json server entry → a sanitized registry connection
    recipe. Secrets never survive this function — only placeholders do."""
    if _is_http_config(cfg):
        url = _redact_uri_passwords(str(cfg.get("url") or ""))
        conn: dict[str, Any] = {"recipe": "http", "url": url}
        port = _tunnel_port(url)
        if port:
            conn["tunnel"] = {"port": port, "target": name, "opened_by": "local SSH tunnel"}
        return conn

    command = str(cfg.get("command") or "")
    args = [str(a) for a in (cfg.get("args") or [])]
    env = cfg.get("env") or {}
    base = os.path.basename(command)

    if base == "ssh":
        alias, remote = _ssh_alias_and_command(args)
        conn = {"recipe": "ssh-stdio", "command": "ssh", "args": []}
        if alias:
            conn["args"].append(alias)
        if remote:
            conn["args"].append(_sanitize_arg(remote))
        sanitized = _sanitize_env(env)
        if sanitized:
            conn["env"] = sanitized
        return conn

    haystack = " ".join([command, *args, *(str(v) for v in env.values())])
    port = _tunnel_port(haystack)
    if port:
        recipe = "local-tunnel"
    elif base in _LOCAL_RUNNERS:
        recipe = "local-npx"
    else:
        recipe = "local-command"
    conn = {"recipe": recipe, "command": command, "args": [_sanitize_arg(a) for a in args]}
    if port:
        conn["tunnel"] = {"port": port, "target": name, "opened_by": "local SSH tunnel"}
    sanitized = _sanitize_env(env)
    if sanitized:
        conn["env"] = sanitized
    return conn


def _mcp_servers_map(data: Any, path: str) -> dict[str, Any]:
    """Accept the standard {"mcpServers": {...}} shape, tolerating a bare
    {name: config} map."""
    if isinstance(data, dict):
        servers = data.get("mcpServers")
        if isinstance(servers, dict):
            return servers
        if data and all(isinstance(v, dict) for v in data.values()):
            return data
    raise CliError(f"{path}: no 'mcpServers' object found")


async def cmd_import(args, col) -> int:
    """Bulk-import a standard MCP client config (.mcp.json): each server
    becomes a registry entry with sanitized connection, inferred category and
    recipe. Curated fields of existing entries (description, status, verified
    timestamps, backend, category) are preserved — the file is the connection
    truth, Mongo stays the curation truth."""
    servers = _mcp_servers_map(_read_json(args.mcp_json), args.mcp_json)
    created = updated = 0
    for name, cfg in sorted(servers.items()):
        if not isinstance(cfg, dict):
            raise CliError(f"{args.mcp_json}: server {name!r} is not a JSON object")
        existing = await col.find_one({"kind": "server", "name": name}) or {}
        doc = _public(existing)
        doc["name"] = name
        doc["transport"] = "http" if _is_http_config(cfg) else "stdio"
        doc["category"] = existing.get("category") or infer_category(name)
        doc["connection"] = _infer_connection(name, cfg)
        if not existing:
            doc["status"] = "available"
            doc["description"] = f"Imported from {os.path.basename(args.mcp_json)}"
        doc.update({"kind": "server", "updated_at": _now()})
        await col.update_one({"kind": "server", "name": name}, {"$set": doc}, upsert=True)
        created += 0 if existing else 1
        updated += 1 if existing else 0
        print(
            f"{'updated' if existing else 'created'} {name} "
            f"({doc['category']}, {doc['connection'].get('recipe')})"
        )
    await _touch_meta_updated(col)
    print(f"imported {created + updated} servers from {args.mcp_json} ({created} new, {updated} updated)")
    return 0


# --------------------------------------------------------------------------- #
# vault — one-password credential vault (PBKDF2-SHA256 → Fernet)              #
# --------------------------------------------------------------------------- #


def _vault_password(args) -> str:
    password = getattr(args, "password", "") or os.environ.get("MHB_VAULT_PASSWORD", "")
    if not password:
        raise CliError("vault password required: --password or MHB_VAULT_PASSWORD env")
    return password


def extract_credentials(data: Any) -> dict[str, Any]:
    """Pull the real credentials out of a standard .mcp.json — secret-looking
    env values, URIs with embedded passwords, and KEY=value secrets hiding in
    args (e.g. ssh-wrapped `env TOKEN=… cmd` launchers). Companion values
    (URIs/usernames) ride along so an unlocked vault is self-sufficient."""
    out: dict[str, Any] = {}
    for name, cfg in _mcp_servers_map(data, "<mcp-json>").items():
        creds: dict[str, Any] = {}
        for key, value in (cfg.get("env") or {}).items():
            if not isinstance(value, str):
                continue
            if (
                _SECRET_KEY_RE.search(key)
                or _COMPANION_KEY_RE.search(key)
                or _URI_CRED_RE.search(value)
            ):
                creds.setdefault("env", {})[key] = value
        texts = [str(a) for a in (cfg.get("args") or [])]
        if cfg.get("url"):
            texts.append(str(cfg["url"]))
        for text in texts:
            for match in _URI_CRED_RE.finditer(text):
                creds.setdefault("urls", [])
                if match.group(0) not in creds["urls"]:
                    creds["urls"].append(match.group(0))
            for match in _KEYVAL_RE.finditer(text):
                if _SECRET_KEY_RE.search(match.group(1)):
                    creds.setdefault("env", {})[match.group(1)] = match.group(2)
        if creds:
            out[name] = creds
    return out


def extract_mc_alias(mc_config: Any, alias: str) -> dict[str, Any]:
    """One `mc` (MinIO client) alias → {endpoint, access_key, secret_key}."""
    aliases = (mc_config or {}).get("aliases") or {}
    entry = aliases.get(alias)
    if not entry:
        raise CliError(f"mc config has no alias {alias!r} (have: {', '.join(sorted(aliases))})")
    return {
        "endpoint": entry.get("url"),
        "access_key": entry.get("accessKey"),
        "secret_key": entry.get("secretKey"),
    }


def _deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _load_seed_payload(args) -> dict[str, Any]:
    """Merge the requested credential sources into one {service: creds} map."""
    payload: dict[str, Any] = {}
    if args.file:
        data = _read_json(args.file)
        if not isinstance(data, dict):
            raise CliError("vault seed file must contain a JSON object {service: creds}")
        _deep_merge(payload, data)
    if args.from_mcp_json:
        _deep_merge(payload, extract_credentials(_read_json(args.from_mcp_json)))
    if args.mc_config:
        cred = extract_mc_alias(_read_json(args.mc_config), args.mc_alias)
        _deep_merge(payload, {args.mc_service or args.mc_alias: cred})
    for pair in args.set or []:
        lhs, sep, raw = pair.partition("=")
        service, dot, field = lhs.partition(".")
        if not sep or not dot or not service or not field:
            raise CliError(f"--set expects service.field=value, got: {pair!r}")
        payload.setdefault(service, {})[field] = _parse_value(raw)
    if not payload:
        raise CliError(
            "empty vault payload — pass --file, --from-mcp-json, --mc-config or --set"
        )
    return payload


async def cmd_vault_init(args, col) -> int:
    """Collect real credentials → encrypt with the registry password → store
    the ciphertext blob in Mongo. Rotation = run this again (fresh salt, blob
    atomically replaced; the old password stops working immediately)."""
    payload = _load_seed_payload(args)
    doc = mcp_vault.encrypt_payload(payload, _vault_password(args))
    await col.update_one({"_id": mcp_vault.VAULT_ID}, {"$set": doc}, upsert=True)
    await _touch_meta_updated(col)
    print(
        f"vault initialized: {len(payload)} services ({', '.join(sorted(payload))}) "
        "— ciphertext stored in Mongo, plaintext nowhere"
    )
    return 0


async def _vault_doc(col) -> dict[str, Any]:
    doc = await col.find_one({"_id": mcp_vault.VAULT_ID})
    if not doc:
        raise CliError("vault not initialized — run: mhb-mcp vault init")
    return doc


async def cmd_vault_unlock(args, col) -> int:
    """Decrypt the vault locally and print the credentials as JSON — the
    verification path for 'one password unlocks every MCP'."""
    try:
        payload = mcp_vault.decrypt_payload(await _vault_doc(col), _vault_password(args))
    except mcp_vault.VaultError as e:
        raise CliError(str(e)) from e
    print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
    return 0


async def cmd_vault_show(args, col) -> int:
    """Vault metadata — KDF/cipher/services/updated_at. Never plaintext."""
    doc = await _vault_doc(col)
    view = mcp_vault.public_view(doc)
    view["blob"] = f"<{len(view['blob'])} chars of fernet ciphertext>"
    print(json.dumps(view, indent=2, ensure_ascii=False, sort_keys=True))
    return 0


# --------------------------------------------------------------------------- #
# verify — recipe-aware reachability probes                                    #
# --------------------------------------------------------------------------- #


async def _probe_tcp(host: str, port: int, timeout: float) -> None:
    """Raise unless a TCP connect succeeds within `timeout`."""
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port), timeout=timeout
    )
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass


async def _probe_http(url: str, timeout: float) -> str:
    """Any HTTP response (even 4xx) proves reachability."""
    import httpx

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(url)
        return f"HTTP {resp.status_code}"


async def _probe_ssh(alias: str, timeout: float) -> None:
    """BatchMode reachability check — never prompts, fails fast without keys."""
    proc = await asyncio.create_subprocess_exec(
        "ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={int(timeout)}",
        alias, "true",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout + 5)
    if proc.returncode != 0:
        raise CliError(stderr.decode(errors="replace").strip()[:200] or "ssh failed")


async def probe_server(server: dict[str, Any], timeout: float = 5.0) -> tuple[str, str]:
    """Probe one entry. Returns (result, detail) with result in
    {"ok", "fail", "skip"} — skip is honest: the recipe has no safe probe."""
    connection = server.get("connection") or {}
    recipe = connection.get("recipe", "")
    try:
        if recipe == "local-tunnel":
            port = int((connection.get("tunnel") or {}).get("port", 0))
            if not port:
                return "skip", "local-tunnel without tunnel.port"
            await _probe_tcp("127.0.0.1", port, timeout)
            return "ok", f"tcp 127.0.0.1:{port} connect"
        if recipe == "http":
            url = connection.get("url", "")
            if not url:
                return "skip", "http recipe without url"
            detail = await _probe_http(url, timeout)
            return "ok", f"GET {url} -> {detail}"
        if recipe == "ssh-stdio":
            args = connection.get("args") or []
            if not args:
                return "skip", "ssh-stdio without ssh alias in args[0]"
            await _probe_ssh(str(args[0]), timeout)
            return "ok", f"ssh {args[0]} true"
        return "skip", f"recipe {recipe!r} has no probe (local command)"
    except Exception as e:
        return "fail", f"{type(e).__name__}: {e}"[:300]


async def _record_probe(col, name: str, result: str, detail: str) -> None:
    now = _now()
    update: dict[str, Any] = {"updated_at": now}
    if result == "ok":
        update.update(
            status="verified",
            verified_at=now.date().isoformat(),
            last_probe_at=now.isoformat(),
            notes=f"probe ok: {detail}",
        )
    elif result == "fail":
        update.update(
            status="unreachable",
            last_probe_at=now.isoformat(),
            notes=f"probe failed: {detail}",
        )
    else:  # skip — record honestly, don't touch status/verified_at
        update["notes"] = f"probe skipped: {detail}"
    await col.update_one({"kind": "server", "name": name}, {"$set": update})


async def cmd_verify(args, col) -> int:
    if args.name:
        doc = await col.find_one({"kind": "server", "name": args.name})
        if doc is None:
            raise CliError(f"no such server: {args.name}")
        targets = [doc]
    else:
        targets = await _all_servers(col)
    if not targets:
        print("(registry empty)")
        return 0
    failures = 0
    for server in targets:
        name = server.get("name", "?")
        result, detail = await probe_server(server, timeout=args.timeout)
        await _record_probe(col, name, result, detail)
        if result == "fail":
            failures += 1
        mark = {"ok": "OK  ", "fail": "FAIL", "skip": "SKIP"}[result]
        print(f"{mark} {name:<22}{detail}")
    await _touch_meta_updated(col)
    return 1 if failures else 0


async def cmd_export(args, col) -> int:
    """Dump the registry back to a metahumotonic/mcp-registry@1 manifest —
    the same enriched shape the live API serves (JSON-LD, capabilities, auth,
    vault spec). Refreshes the static-site fallback /mcp/manifest.json."""
    meta = await col.find_one({"_id": META_ID}) or {}
    servers = [
        {k: v for k, v in _public(d).items() if k not in _EXPORT_DROP}
        for d in await _all_servers(col)
    ]
    manifest = build_manifest(servers, meta, updated=_today())
    text = json.dumps(manifest, indent=2, default=str, ensure_ascii=False) + "\n"
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"wrote {args.out} ({len(servers)} servers)")
    else:
        print(text, end="")
    return 0


# --------------------------------------------------------------------------- #
# argparse wiring                                                              #
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mhb-mcp",
        description="Operator CLI for the metahumotonic MCP registry (Mongo-backed).",
    )
    parser.add_argument("--mongo-uri", default="", help="target Mongo URI (default: MHB_MONGO_URI env, then macbook tunnel via MONGO_PASSWORD)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("seed", help="upsert all servers from a manifest.json")
    p.add_argument("manifest")
    p.set_defaults(func=cmd_seed)

    p = sub.add_parser("list", help="list registered servers")
    p.add_argument("--json", action="store_true", help="full JSON instead of a table")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="show one server as JSON")
    p.add_argument("name")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("upsert", help="create/update one server (--set k=v or --file json)")
    p.add_argument("name")
    p.add_argument("--set", action="append", metavar="KEY=VALUE", help="set a field (JSON-parsed when possible); repeatable")
    p.add_argument("--file", help="JSON file with fields to merge")
    p.set_defaults(func=cmd_upsert)

    p = sub.add_parser("remove", help="remove one server")
    p.add_argument("name")
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("import", help="bulk-import a standard MCP client config (.mcp.json)")
    p.add_argument("mcp_json", help="path to a .mcp.json with an mcpServers object")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("verify", help="probe reachability and record status/verified_at")
    p.add_argument("name", nargs="?", help="one server (default: all)")
    p.add_argument("--timeout", type=float, default=5.0, help="per-probe timeout seconds")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("export", help="export Mongo back to a manifest.json (enriched)")
    p.add_argument("--out", help="write to file instead of stdout")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("vault", help="one-password credential vault (init/unlock/show)")
    vault_sub = p.add_subparsers(dest="vault_command", required=True)

    pv = vault_sub.add_parser("init", help="collect credentials → encrypt → store ciphertext in Mongo")
    pv.add_argument("--password", default="", help="registry password (default: MHB_VAULT_PASSWORD env)")
    pv.add_argument("--file", help="seed JSON {service: creds} (merged first)")
    pv.add_argument("--from-mcp-json", help="extract credentials from a standard .mcp.json")
    pv.add_argument("--mc-config", help="mc (MinIO client) config.json to extract an alias from")
    pv.add_argument("--mc-alias", default="bhgman", help="alias inside --mc-config (default: bhgman)")
    pv.add_argument("--mc-service", default="", help="registry service name for the mc alias (default: the alias itself)")
    pv.add_argument("--set", action="append", metavar="SERVICE.FIELD=VALUE", help="deep-set one credential field; repeatable")
    pv.set_defaults(func=cmd_vault_init)

    pv = vault_sub.add_parser("unlock", help="decrypt the vault locally and print credentials (JSON)")
    pv.add_argument("--password", default="", help="registry password (default: MHB_VAULT_PASSWORD env)")
    pv.set_defaults(func=cmd_vault_unlock)

    pv = vault_sub.add_parser("show", help="vault metadata (KDF/cipher/services — never plaintext)")
    pv.set_defaults(func=cmd_vault_show)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        uri = resolve_mongo_uri(args.mongo_uri)
        collection = get_collection(uri)
        return asyncio.run(args.func(args, collection))
    except CliError as e:
        print(f"mhb-mcp: error: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # infra errors (Mongo down, etc.)
        print(f"mhb-mcp: error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
