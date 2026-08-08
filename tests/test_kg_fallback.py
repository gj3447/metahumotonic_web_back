from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.kg as kg_mod
from app.config import settings
from app.kg import KGClient


class FakeResult:
    def __init__(self, rows):
        self._rows = iter(rows)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._rows)
        except StopIteration:
            raise StopAsyncIteration


class FakeSession:
    def __init__(self, uri: str):
        self.uri = uri

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def run(self, cypher: str, **params):
        if self.uri == "bolt://bad-dns:7687":
            raise RuntimeError("dns failed")
        return FakeResult([{"ok": 1}])


class FakeDriver:
    def __init__(self, uri: str):
        self.uri = uri
        self.closed = False

    def session(self):
        return FakeSession(self.uri)

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_kg_run_tries_fallback_uri_after_primary_failure(monkeypatch):
    calls: list[str] = []

    def fake_driver(uri: str, auth):
        calls.append(uri)
        return FakeDriver(uri)

    monkeypatch.setattr(settings, "neo4j_live", True)
    monkeypatch.setattr(settings, "neo4j_uri", "bolt://bad-dns:7687")
    monkeypatch.setattr(settings, "neo4j_fallback_uris", "bolt://100.64.0.3:7687")
    monkeypatch.setattr(settings, "kg_query_timeout_seconds", 1)
    monkeypatch.setattr(
        kg_mod, "AsyncGraphDatabase", SimpleNamespace(driver=fake_driver)
    )

    client = KGClient()
    try:
        rows = await client._run("RETURN 1 AS ok")
        assert rows == [{"ok": 1}]
        assert calls == ["bolt://bad-dns:7687", "bolt://100.64.0.3:7687"]
        assert "bolt://bad-dns:7687" not in client._drivers
        assert "bolt://100.64.0.3:7687" in client._drivers
    finally:
        await client.close()
