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
    mhb-mcp verify [NAME]              # probe reachability, record results
    mhb-mcp export [--out manifest.json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

DB_DEFAULT = "metahumotonic"
COLLECTION = "mcp_servers"
META_ID = "manifest_meta"
SCHEMA = "metahumotonic/mcp-registry@1"
SITE = "https://metahumotonic.com"

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


# --------------------------------------------------------------------------- #
# Commands (each takes the parsed args + a collection handle)                  #
# --------------------------------------------------------------------------- #


async def cmd_seed(args, col) -> int:
    """Upsert every server from a manifest file + store manifest-level meta."""
    try:
        with open(args.manifest, encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, ValueError) as e:
        raise CliError(f"cannot read manifest {args.manifest}: {e}") from e
    servers = manifest.get("servers")
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
        try:
            with open(args.file, encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError) as e:
            raise CliError(f"cannot read {args.file}: {e}") from e
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
    """Dump the registry back to a metahumotonic/mcp-registry@1 manifest
    (refreshes the static-site fallback file)."""
    meta = await col.find_one({"_id": META_ID}) or {}
    servers = [
        {k: v for k, v in _public(d).items() if k not in _EXPORT_DROP}
        for d in await _all_servers(col)
    ]
    manifest = {
        "schema": meta.get("schema", SCHEMA),
        "updated": _today(),
        "site": meta.get("site", SITE),
        "notes": meta.get("notes", []),
        "servers": servers,
    }
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

    p = sub.add_parser("verify", help="probe reachability and record status/verified_at")
    p.add_argument("name", nargs="?", help="one server (default: all)")
    p.add_argument("--timeout", type=float, default=5.0, help="per-probe timeout seconds")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("export", help="export Mongo back to a manifest.json")
    p.add_argument("--out", help="write to file instead of stdout")
    p.set_defaults(func=cmd_export)

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
