from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.config import settings
from app.main import _validate_wiki_configuration
from app.routers import meta


class _Dependency:
    def __init__(self, ready: bool) -> None:
        self._ready = ready

    async def ready(self) -> bool:
        return self._ready

    async def ping(self) -> bool:
        return self._ready


def _valid_public_settings(monkeypatch) -> None:
    monkeypatch.setattr(settings, "wiki_public_writes", True)
    monkeypatch.setattr(settings, "wiki_database_url", "postgresql://wiki/test")
    monkeypatch.setattr(settings, "wiki_session_secret", "s" * 32)
    monkeypatch.setattr(settings, "wiki_moderation_admin_key", "m" * 32)
    monkeypatch.setattr(settings, "wiki_require_redis", True)
    monkeypatch.setattr(settings, "redis_url", "redis://redis.test/0")


def test_public_wiki_requires_distinct_moderation_authority(monkeypatch):
    _valid_public_settings(monkeypatch)
    _validate_wiki_configuration()

    monkeypatch.setattr(settings, "wiki_moderation_admin_key", "short")
    with pytest.raises(RuntimeError, match="MODERATION_ADMIN_KEY"):
        _validate_wiki_configuration()

    monkeypatch.setattr(settings, "wiki_moderation_admin_key", "s" * 32)
    with pytest.raises(RuntimeError, match="must be distinct"):
        _validate_wiki_configuration()


async def test_ready_fails_closed_when_wiki_redis_is_unavailable(monkeypatch):
    monkeypatch.setattr(settings, "wiki_public_writes", True)
    monkeypatch.setattr(settings, "wiki_require_redis", True)
    monkeypatch.setattr(settings, "neo4j_live", False)
    app = FastAPI()
    app.include_router(meta.router)
    app.state.wiki_runtime = SimpleNamespace(
        ready=True,
        store=_Dependency(True),
        session_limiter=_Dependency(True),
        mutation_limiter=_Dependency(False),
        read_limiter=_Dependency(True),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://test",
    ) as client:
        response = await client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "kg_live": False,
        "wiki_required": True,
        "wiki_live": False,
        "wiki_store_live": True,
        "wiki_rate_limit_live": False,
        "degraded": True,
    }


async def test_ready_requires_both_wiki_store_and_distributed_limits(monkeypatch):
    monkeypatch.setattr(settings, "wiki_public_writes", True)
    monkeypatch.setattr(settings, "wiki_require_redis", True)
    monkeypatch.setattr(settings, "neo4j_live", False)
    available = _Dependency(True)
    app = FastAPI()
    app.include_router(meta.router)
    app.state.wiki_runtime = SimpleNamespace(
        ready=True,
        store=available,
        session_limiter=available,
        mutation_limiter=available,
        read_limiter=available,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://test",
    ) as client:
        response = await client.get("/ready")

    assert response.status_code == 200
    assert response.json()["wiki_live"] is True
    assert response.json()["wiki_rate_limit_live"] is True
