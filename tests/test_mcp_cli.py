"""Unit tests for the mhb-mcp CLI (app/cli.py) — Mongo faked in-memory.

Covers seed/list/show/upsert/remove/export round-trips, probe result
recording, and the honest skip path for non-probeable recipes. No Mongo,
no network (probes are exercised against a local TCP listener or faked).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app import cli


class FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    def __aiter__(self):
        async def gen():
            for doc in self._docs:
                yield dict(doc)

        return gen()


class FakeResult:
    def __init__(self, deleted_count=0):
        self.deleted_count = deleted_count


class FakeDatabase:
    name = "metahumotonic"


class FakeCollection:
    """Minimal in-memory stand-in for a motor collection."""

    def __init__(self):
        self.docs: dict[object, dict] = {}
        self.database = FakeDatabase()
        self.name = cli.COLLECTION
        self.indexes = []

    async def create_index(self, key, **kwargs):
        self.indexes.append((key, kwargs))

    def find(self, filter_, projection=None):
        return FakeCursor(
            [d for d in self.docs.values() if d.get("kind") == filter_.get("kind")]
        )

    async def find_one(self, filter_, projection=None):
        if filter_.get("_id") is not None:
            doc = self.docs.get(filter_["_id"])
            return dict(doc) if doc else None
        for doc in self.docs.values():
            if doc.get("kind") == filter_.get("kind") and doc.get("name") == filter_.get("name"):
                return dict(doc)
        return None

    async def update_one(self, filter_, update, upsert=False):
        existing = await self.find_one(filter_)
        doc = existing or {}
        if "_id" in filter_:
            doc["_id"] = filter_["_id"]
        doc.update(update.get("$set", {}))
        key = doc.get("_id") or ("server", doc.get("name"))
        self.docs[key] = doc

    async def delete_one(self, filter_):
        existing = await self.find_one(filter_)
        if existing is None:
            return FakeResult(0)
        self.docs.pop(("server", existing["name"]), None)
        return FakeResult(1)

    def servers(self):
        return [d for d in self.docs.values() if d.get("kind") == "server"]


MANIFEST = {
    "schema": "metahumotonic/mcp-registry@1",
    "updated": "2026-07-27",
    "site": "https://metahumotonic.com",
    "notes": ["secrets are placeholders"],
    "servers": [
        {
            "name": "memory",
            "description": "KG memory.",
            "category": "utility",
            "transport": "stdio",
            "status": "verified",
            "verified_at": "2026-07-27",
            "connection": {"recipe": "local-npx", "command": "npx", "args": ["-y", "x"]},
        },
        {
            "name": "redis",
            "description": "Redis 8.",
            "category": "vector",
            "transport": "stdio",
            "status": "verified",
            "verified_at": "2026-07-27",
            "connection": {
                "recipe": "local-tunnel",
                "command": "uvx",
                "args": ["redis-mcp-server"],
                "tunnel": {"port": 16379, "target": "redis"},
            },
        },
    ],
}


class Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


@pytest.fixture
def col():
    return FakeCollection()


@pytest.fixture
def manifest_file(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(MANIFEST), encoding="utf-8")
    return str(path)


async def test_seed_loads_servers_and_meta(col, manifest_file, capsys):
    rc = await cli.cmd_seed(Args(manifest=manifest_file), col)
    assert rc == 0
    assert {s["name"] for s in col.servers()} == {"memory", "redis"}
    meta = await col.find_one({"_id": cli.META_ID})
    assert meta["schema"] == "metahumotonic/mcp-registry@1"
    assert meta["notes"] == ["secrets are placeholders"]
    # unique name index requested
    assert any(k == "name" for k, _ in col.indexes)
    assert "seeded 2 servers" in capsys.readouterr().out


async def test_seed_is_idempotent_upsert(col, manifest_file):
    await cli.cmd_seed(Args(manifest=manifest_file), col)
    await cli.cmd_seed(Args(manifest=manifest_file), col)
    assert len(col.servers()) == 2


async def test_seed_rejects_empty_manifest(col, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"servers": []}', encoding="utf-8")
    with pytest.raises(cli.CliError):
        await cli.cmd_seed(Args(manifest=str(bad)), col)


async def test_list_table_and_json(col, manifest_file, capsys):
    await cli.cmd_seed(Args(manifest=manifest_file), col)
    capsys.readouterr()
    await cli.cmd_list(Args(json=False), col)
    out = capsys.readouterr().out
    assert "memory" in out and "redis" in out and "STATUS" in out
    await cli.cmd_list(Args(json=True), col)
    parsed = json.loads(capsys.readouterr().out)
    assert {s["name"] for s in parsed} == {"memory", "redis"}


async def test_show_and_missing(col, manifest_file, capsys):
    await cli.cmd_seed(Args(manifest=manifest_file), col)
    capsys.readouterr()
    await cli.cmd_show(Args(name="redis"), col)
    doc = json.loads(capsys.readouterr().out)
    assert doc["connection"]["recipe"] == "local-tunnel"
    with pytest.raises(cli.CliError):
        await cli.cmd_show(Args(name="nope"), col)


async def test_upsert_set_and_remove(col, manifest_file):
    await cli.cmd_seed(Args(manifest=manifest_file), col)
    await cli.cmd_upsert(
        Args(name="redis", set=["status=unused", 'extra={"a": 1}'], file=None), col
    )
    doc = await col.find_one({"kind": "server", "name": "redis"})
    assert doc["status"] == "unused"
    assert doc["extra"] == {"a": 1}  # JSON-parsed --set value
    assert doc["description"] == "Redis 8."  # merge keeps existing fields
    await cli.cmd_upsert(Args(name="newone", set=["description=New"], file=None), col)
    assert (await col.find_one({"kind": "server", "name": "newone"}))["status"] == "available"
    await cli.cmd_remove(Args(name="newone"), col)
    assert await col.find_one({"kind": "server", "name": "newone"}) is None
    with pytest.raises(cli.CliError):
        await cli.cmd_remove(Args(name="newone"), col)


async def test_upsert_rejects_bad_set(col):
    with pytest.raises(cli.CliError):
        await cli.cmd_upsert(Args(name="x", set=["noequals"], file=None), col)


async def test_export_roundtrip(col, manifest_file, tmp_path):
    await cli.cmd_seed(Args(manifest=manifest_file), col)
    out = tmp_path / "out.json"
    rc = await cli.cmd_export(Args(out=str(out)), col)
    assert rc == 0
    exported = json.loads(out.read_text(encoding="utf-8"))
    assert exported["schema"] == "metahumotonic/mcp-registry@1"
    assert exported["notes"] == ["secrets are placeholders"]
    assert {s["name"] for s in exported["servers"]} == {"memory", "redis"}
    # bookkeeping fields must not leak into the manifest
    for s in exported["servers"]:
        assert "updated_at" not in s and "kind" not in s and "_id" not in s


async def test_probe_tcp_ok_and_fail():
    server = {
        "name": "t",
        "connection": {"recipe": "local-tunnel", "tunnel": {"port": 0}},
    }
    # Find a free port by binding a listener.  Close the accepted stream
    # explicitly: Python 3.12's Server.wait_closed() correctly waits for all
    # active connections, so a callback that drops the StreamWriter can hang
    # forever instead of being collected promptly.
    connection_closed = asyncio.Event()

    async def close_connection(_reader, writer):
        writer.close()
        try:
            await writer.wait_closed()
        finally:
            connection_closed.set()

    listener = await asyncio.start_server(close_connection, "127.0.0.1", 0)
    port = listener.sockets[0].getsockname()[1]
    server["connection"]["tunnel"]["port"] = port
    try:
        result, _ = await cli.probe_server(server, timeout=2.0)
        assert result == "ok"
        await asyncio.wait_for(connection_closed.wait(), timeout=2.0)
    finally:
        listener.close()
        await listener.wait_closed()
    # nothing listening now → fail
    result, detail = await cli.probe_server(server, timeout=1.0)
    assert result == "fail"
    assert detail


async def test_probe_skip_for_local_recipe():
    server = {"name": "m", "connection": {"recipe": "local-npx", "command": "npx"}}
    result, detail = await cli.probe_server(server)
    assert result == "skip"
    assert "no probe" in detail


async def test_verify_records_status(col, manifest_file):
    await cli.cmd_seed(Args(manifest=manifest_file), col)

    async def fake_probe(server, timeout=5.0):
        return ("ok", "tcp 127.0.0.1:16379 connect") if server["name"] == "redis" else (
            "skip",
            "recipe 'local-npx' has no probe (local command)",
        )

    import app.cli as cli_mod

    orig = cli_mod.probe_server
    cli_mod.probe_server = fake_probe
    try:
        rc = await cli.cmd_verify(Args(name=None, timeout=1.0), col)
    finally:
        cli_mod.probe_server = orig
    assert rc == 0  # skip is not a failure
    redis = await col.find_one({"kind": "server", "name": "redis"})
    assert redis["status"] == "verified"
    assert redis["verified_at"]
    assert redis["last_probe_at"]
    assert redis["notes"].startswith("probe ok")
    memory = await col.find_one({"kind": "server", "name": "memory"})
    assert memory["status"] == "verified"  # untouched by skip
    assert "probe skipped" in memory["notes"]


async def test_verify_failure_marks_unreachable_and_rc1(col, manifest_file):
    await cli.cmd_seed(Args(manifest=manifest_file), col)

    async def fake_probe(server, timeout=5.0):
        return "fail", "ConnectionRefusedError: refused"

    import app.cli as cli_mod

    orig = cli_mod.probe_server
    cli_mod.probe_server = fake_probe
    try:
        rc = await cli.cmd_verify(Args(name="redis", timeout=1.0), col)
    finally:
        cli_mod.probe_server = orig
    assert rc == 1
    redis = await col.find_one({"kind": "server", "name": "redis"})
    assert redis["status"] == "unreachable"
    assert redis["verified_at"] == "2026-07-27"  # last success preserved
    assert redis["last_probe_at"]
    assert "probe failed" in redis["notes"]


def test_resolve_mongo_uri_precedence(monkeypatch):
    monkeypatch.delenv("MHB_MONGO_URI", raising=False)
    monkeypatch.delenv("MONGO_PASSWORD", raising=False)
    with pytest.raises(cli.CliError):
        cli.resolve_mongo_uri("")
    monkeypatch.setenv("MONGO_PASSWORD", "pw")
    uri = cli.resolve_mongo_uri("")
    assert uri.startswith("mongodb://mongo:pw@127.0.0.1:37017/")
    monkeypatch.setenv("MHB_MONGO_URI", "mongodb://example:27017/")
    assert cli.resolve_mongo_uri("") == "mongodb://example:27017/"
    assert cli.resolve_mongo_uri("mongodb://flag:1/") == "mongodb://flag:1/"


# --------------------------------------------------------------------------- #
# H-03 — mhb-mcp import (.mcp.json → sanitized registry entries)              #
# --------------------------------------------------------------------------- #

MCP_JSON = {
    "mcpServers": {
        "neo4j-test": {
            "type": "stdio",
            "command": "/opt/homebrew/bin/uvx",
            "args": ["mcp-neo4j-cypher@0.5.3"],
            "env": {
                "NEO4J_URI": "bolt://127.0.0.1:17687",
                "NEO4J_USERNAME": "neo4j",
                "NEO4J_PASSWORD": "secretpw",
            },
        },
        "miniostore": {
            "type": "stdio",
            "command": "docker",
            "args": ["run", "-i", "mcp-server-aistor"],
            "env": {"MINIO_SECRET_KEY": "miniosecret"},
        },
        "webby": {"type": "http", "url": "http://127.0.0.1:18009/mcp"},
        "sshthing": {
            "type": "stdio",
            "command": "ssh",
            "args": ["-o", "BatchMode=yes", "myhost", "env API_TOKEN=tok123 MCP_TRANSPORT=stdio /usr/bin/run-mcp"],
        },
        "plain": {
            "type": "stdio",
            "command": "/opt/homebrew/bin/npx",
            "args": ["-y", "@modelcontextprotocol/server-memory"],
        },
        "postgres-test": {
            "type": "stdio",
            "command": "/opt/homebrew/bin/uvx",
            "args": ["postgres-mcp"],
            "env": {"DATABASE_URI": "postgresql://postgres:pgpw@127.0.0.1:15432/maindb"},
        },
    }
}


@pytest.fixture
def mcp_json_file(tmp_path):
    path = tmp_path / ".mcp.json"
    path.write_text(json.dumps(MCP_JSON), encoding="utf-8")
    return str(path)


async def test_import_creates_sanitized_entries(col, mcp_json_file, capsys):
    rc = await cli.cmd_import(Args(mcp_json=mcp_json_file), col)
    assert rc == 0
    by_name = {s["name"]: s for s in col.servers()}
    assert set(by_name) == {"neo4j-test", "miniostore", "webby", "sshthing", "plain", "postgres-test"}

    # name-based category inference (H-03)
    assert by_name["neo4j-test"]["category"] == "graph"
    assert by_name["miniostore"]["category"] == "storage"
    assert by_name["postgres-test"]["category"] == "document"
    assert by_name["webby"]["category"] == "utility"
    assert by_name["plain"]["category"] == "utility"

    # recipe inference
    neo = by_name["neo4j-test"]["connection"]
    assert neo["recipe"] == "local-tunnel"  # bolt://127.0.0.1:17687 in env
    assert neo["tunnel"]["port"] == 17687
    assert by_name["webby"]["connection"] == {
        "recipe": "http",
        "url": "http://127.0.0.1:18009/mcp",
        "tunnel": {"port": 18009, "target": "webby", "opened_by": "local SSH tunnel"},
    }
    assert by_name["plain"]["connection"]["recipe"] == "local-npx"
    ssh = by_name["sshthing"]["connection"]
    assert ssh["recipe"] == "ssh-stdio"
    assert ssh["args"][0] == "myhost"
    assert "API_TOKEN=<API_TOKEN>" in ssh["args"][1]
    assert "tok123" not in ssh["args"][1]

    # secret placeholdering: env keys stay, secret values become <KEY>
    assert neo["env"]["NEO4J_PASSWORD"] == "<NEO4J_PASSWORD>"
    assert neo["env"]["NEO4J_URI"] == "bolt://127.0.0.1:17687"  # non-secret kept
    pg = by_name["postgres-test"]["connection"]
    assert pg["env"]["DATABASE_URI"] == "postgresql://postgres:<PG_PASSWORD>@127.0.0.1:15432/maindb"
    assert pg["tunnel"]["port"] == 15432
    assert by_name["miniostore"]["connection"]["env"]["MINIO_SECRET_KEY"] == "<MINIO_SECRET_KEY>"

    # new entries default to available + transport recorded
    assert by_name["plain"]["status"] == "available"
    assert by_name["webby"]["transport"] == "http"
    assert by_name["plain"]["transport"] == "stdio"
    out = capsys.readouterr().out
    assert "6 new" in out

    # nothing secret survives anywhere in Mongo
    dump = json.dumps(list(col.docs.values()), default=str)
    for secret in ("secretpw", "miniosecret", "tok123", "pgpw"):
        assert secret not in dump


async def test_import_preserves_curated_fields(col, mcp_json_file):
    await cli.cmd_upsert(
        Args(name="plain", set=["description=Curated desc", "status=verified",
                                    "category=utility"], file=None),
        col,
    )
    await cli.cmd_import(Args(mcp_json=mcp_json_file), col)
    doc = await col.find_one({"kind": "server", "name": "plain"})
    assert doc["description"] == "Curated desc"  # curation wins
    assert doc["status"] == "verified"  # not reset to available
    assert doc["connection"]["recipe"] == "local-npx"  # file is connection truth


async def test_import_export_roundtrip_has_no_plaintext(col, mcp_json_file, tmp_path):
    await cli.cmd_import(Args(mcp_json=mcp_json_file), col)
    out = tmp_path / "out.json"
    await cli.cmd_export(Args(out=str(out)), col)
    text = out.read_text(encoding="utf-8")
    for secret in ("secretpw", "miniosecret", "tok123", "pgpw"):
        assert secret not in text
    exported = json.loads(text)
    # export is the enriched manifest (JSON-LD + capabilities + auth + vault spec)
    assert exported["@context"]["schema"] == "https://schema.org/"
    assert exported["credential_vault"]["url"].endswith("/api/mcp/vault")
    by_name = {s["name"]: s for s in exported["servers"]}
    assert by_name["neo4j-test"]["auth"]["type"] == "vault"
    assert by_name["neo4j-test"]["auth"]["requires"] == ["NEO4J_PASSWORD"]
    assert by_name["postgres-test"]["auth"]["requires"] == ["PG_PASSWORD"]
    assert by_name["plain"]["auth"] == {"type": "none"}
    assert by_name["neo4j-test"]["capabilities"]


def test_extract_credentials_from_mcp_json():
    creds = cli.extract_credentials(MCP_JSON)
    neo = creds["neo4j-test"]["env"]
    assert neo["NEO4J_PASSWORD"] == "secretpw"
    assert neo["NEO4J_URI"] == "bolt://127.0.0.1:17687"  # companion rides along
    assert creds["postgres-test"]["env"]["DATABASE_URI"].startswith("postgresql://postgres:pgpw@")
    assert creds["sshthing"]["env"]["API_TOKEN"] == "tok123"
    assert "plain" not in creds  # nothing secret → not in the vault seed


def test_category_rules_documented_examples():
    assert cli.infer_category("neo4j") == "graph"
    assert cli.infer_category("redis") == "vector"
    assert cli.infer_category("minio") == "storage"
    assert cli.infer_category("something-else") == "utility"


# --------------------------------------------------------------------------- #
# H-04 — mhb-mcp vault init / unlock / show                                   #
# --------------------------------------------------------------------------- #

VAULT_PW = "312447"


def _mc_config(tmp_path):
    path = tmp_path / "mc-config.json"
    path.write_text(json.dumps({
        "aliases": {"bhgman": {"url": "http://localhost:9000",
                               "accessKey": "minioadmin",
                               "secretKey": "miniosecret"}}
    }), encoding="utf-8")
    return str(path)


async def test_vault_init_unlock_roundtrip(col, mcp_json_file, tmp_path, capsys):
    mc = _mc_config(tmp_path)
    rc = await cli.cmd_vault_init(
        Args(password=VAULT_PW, file=None, from_mcp_json=mcp_json_file,
                 mc_config=mc, mc_alias="bhgman", mc_service="bhgman-minio",
                 set=["lakatotree.token=lakatos-tok-123"]),
        col,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "vault initialized" in out

    # stored document carries no plaintext
    stored = await col.find_one({"_id": cli.mcp_vault.VAULT_ID})
    dump = json.dumps(stored)
    for secret in ("secretpw", "miniosecret", "tok123", "lakatos-tok-123", VAULT_PW):
        assert secret not in dump
    assert stored["kdf"]["name"] == "PBKDF2-SHA256"
    assert "bhgman-minio" in stored["services"]

    # unlock with the registry password recovers everything
    capsys.readouterr()
    rc = await cli.cmd_vault_unlock(Args(password=VAULT_PW), col)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["neo4j-test"]["env"]["NEO4J_PASSWORD"] == "secretpw"
    assert payload["bhgman-minio"]["secret_key"] == "miniosecret"
    assert payload["bhgman-minio"]["access_key"] == "minioadmin"
    assert payload["lakatotree"]["token"] == "lakatos-tok-123"
    assert payload["postgres-test"]["env"]["DATABASE_URI"].startswith("postgresql://postgres:pgpw@")


async def test_vault_unlock_wrong_password_fails(col, mcp_json_file):
    await cli.cmd_vault_init(
        Args(password=VAULT_PW, file=None, from_mcp_json=mcp_json_file,
                 mc_config=None, set=None),
        col,
    )
    with pytest.raises(cli.CliError, match="wrong password"):
        await cli.cmd_vault_unlock(Args(password="000000"), col)


async def test_vault_init_requires_password_and_payload(col, mcp_json_file, monkeypatch):
    monkeypatch.delenv("MHB_VAULT_PASSWORD", raising=False)
    with pytest.raises(cli.CliError, match="password required"):
        await cli.cmd_vault_init(
            Args(password="", file=None, from_mcp_json=mcp_json_file,
                     mc_config=None, set=None),
            col,
        )
    with pytest.raises(cli.CliError, match="empty vault payload"):
        await cli.cmd_vault_init(
            Args(password=VAULT_PW, file=None, from_mcp_json=None,
                     mc_config=None, set=None),
            col,
        )


async def test_vault_show_masks_blob(col, mcp_json_file, capsys):
    await cli.cmd_vault_init(
        Args(password=VAULT_PW, file=None, from_mcp_json=mcp_json_file,
                 mc_config=None, set=None),
        col,
    )
    capsys.readouterr()
    rc = await cli.cmd_vault_show(Args(), col)
    assert rc == 0
    out = capsys.readouterr().out
    assert "fernet ciphertext" in out
    assert "secretpw" not in out


async def test_vault_rotation_replaces_blob(col, mcp_json_file):
    args = Args(password=VAULT_PW, file=None, from_mcp_json=mcp_json_file,
                    mc_config=None, set=None)
    await cli.cmd_vault_init(args, col)
    first = await col.find_one({"_id": cli.mcp_vault.VAULT_ID})
    # rotate the registry password: re-init with the new one
    await cli.cmd_vault_init(
        Args(password="654321", file=None, from_mcp_json=mcp_json_file,
                 mc_config=None, set=None),
        col,
    )
    second = await col.find_one({"_id": cli.mcp_vault.VAULT_ID})
    assert first["blob"] != second["blob"]
    assert first["kdf"]["salt"] != second["kdf"]["salt"]
    with pytest.raises(cli.CliError):  # old password is dead
        await cli.cmd_vault_unlock(Args(password=VAULT_PW), col)
    cap = await cli.cmd_vault_unlock(Args(password="654321"), col)
    assert cap == 0


async def test_vault_unlock_before_init_errors(col):
    with pytest.raises(cli.CliError, match="not initialized"):
        await cli.cmd_vault_unlock(Args(password=VAULT_PW), col)


async def test_vault_password_from_env(col, mcp_json_file, monkeypatch, capsys):
    monkeypatch.setenv("MHB_VAULT_PASSWORD", VAULT_PW)
    await cli.cmd_vault_init(
        Args(password="", file=None, from_mcp_json=mcp_json_file,
                 mc_config=None, set=None),
        col,
    )
    capsys.readouterr()
    rc = await cli.cmd_vault_unlock(Args(password=""), col)
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["neo4j-test"]
