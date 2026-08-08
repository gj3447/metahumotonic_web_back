from __future__ import annotations

import httpx
from fastapi import FastAPI, Request

from app.wiki.http import WikiBodyLimitMiddleware, WikiRobotsMiddleware


async def test_wiki_body_limit_rejects_declared_oversize_before_route():
    called = False
    app = FastAPI()

    @app.post("/api/wiki/v1/pages")
    async def endpoint():
        nonlocal called
        called = True
        return {"ok": True}

    wrapped = WikiBodyLimitMiddleware(app, max_bytes=16)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=wrapped), base_url="https://test"
    ) as client:
        response = await client.post("/api/wiki/v1/pages", content=b"x" * 17)
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "request_body_too_large"
    assert called is False


async def test_wiki_body_limit_counts_streamed_chunks():
    called = False
    app = FastAPI()

    @app.post("/api/wiki/v1/pages")
    async def endpoint(request: Request):
        nonlocal called
        await request.body()
        called = True
        return {"ok": True}

    async def chunks():
        yield b"12345678"
        yield b"901234567"

    wrapped = WikiBodyLimitMiddleware(app, max_bytes=16)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=wrapped), base_url="https://test"
    ) as client:
        response = await client.post("/api/wiki/v1/pages", content=chunks())
    assert response.status_code == 413
    assert called is False


async def test_body_limit_does_not_affect_non_wiki_routes():
    app = FastAPI()

    @app.post("/api/feedback")
    async def endpoint(request: Request):
        return {"size": len(await request.body())}

    wrapped = WikiBodyLimitMiddleware(app, max_bytes=4)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=wrapped), base_url="https://test"
    ) as client:
        response = await client.post("/api/feedback", content=b"x" * 20)
    assert response.status_code == 200
    assert response.json() == {"size": 20}


async def test_wiki_robots_header_covers_public_and_internal_surfaces_only():
    app = FastAPI()

    @app.get("/{path:path}")
    async def endpoint(path: str):
        return {"path": path}

    wrapped = WikiRobotsMiddleware(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=wrapped), base_url="https://test"
    ) as client:
        for path in (
            "/api/wiki/v1",
            "/api/wiki/v1/pages",
            "/internal/wiki/moderation/reports",
        ):
            response = await client.get(path)
            assert response.headers["x-robots-tag"] == "noindex, nofollow, noarchive"
            assert response.headers["cache-control"] == "no-store"

        response = await client.get("/api/research/summary")
        assert "x-robots-tag" not in response.headers
