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
    # find a free port by binding a listener
    listener = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = listener.sockets[0].getsockname()[1]
    server["connection"]["tunnel"]["port"] = port
    result, _ = await cli.probe_server(server, timeout=2.0)
    assert result == "ok"
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
