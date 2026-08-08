"""FastAPI app entrypoint.

uvicorn app.main:app --reload
"""

from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from datetime import timedelta
from hashlib import sha256

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from . import __version__
from .config import settings
from .kg import kg
from .mcp_store import registry as mcp_registry_store
from .middleware import RequestLoggingMiddleware
from .observability import configure_logging, instrument
from .ratelimit import RateLimiter
from .routers import (
    domains,
    feedback,
    kg_proxy,
    mcp_registry,
    meta,
    research,
    skills,
    stats,
)
from .routers.wiki import WikiRuntime, create_moderation_router, create_router
from .store import store
from .wiki.http import WikiBodyLimitMiddleware, WikiRobotsMiddleware
from .wiki.memory import InMemoryWikiStore
from .wiki.postgres import PostgresWikiStore
from .wiki.security import SessionSigner

configure_logging()
log = logging.getLogger("mhb.wiki")


def _wiki_signer() -> SessionSigner:
    configured = settings.wiki_session_secret.encode("utf-8")
    secret = configured if len(configured) >= 32 else secrets.token_bytes(32)
    return SessionSigner(
        secret, ttl=timedelta(seconds=settings.wiki_session_ttl_seconds)
    )


def _wiki_moderator_actor(_request: object) -> str:
    """Derive a stable principal without retaining or exposing the operator key."""

    fingerprint = sha256(
        settings.wiki_moderation_admin_key.encode("utf-8")
    ).hexdigest()
    return f"moderator:{fingerprint[:32]}"


_wiki_fail_closed = settings.wiki_public_writes and settings.wiki_require_redis
_wiki_session_limiter = RateLimiter(
    settings.wiki_session_max_per_window,
    settings.wiki_session_window_seconds,
    settings.redis_url,
    fail_closed=_wiki_fail_closed,
)
_wiki_mutation_limiter = RateLimiter(
    settings.wiki_mutation_max_per_window,
    settings.wiki_mutation_window_seconds,
    settings.redis_url,
    fail_closed=_wiki_fail_closed,
)
_wiki_read_limiter = RateLimiter(
    settings.wiki_read_max_per_window,
    settings.wiki_read_window_seconds,
    settings.redis_url,
    fail_closed=_wiki_fail_closed,
)
wiki_runtime = WikiRuntime(
    InMemoryWikiStore(),
    _wiki_signer(),
    writes_enabled=settings.wiki_public_writes,
    secure_cookie=settings.wiki_session_cookie_secure,
    allowed_origins=frozenset(settings.cors_origin_list()),
    session_limiter=_wiki_session_limiter,
    mutation_limiter=_wiki_mutation_limiter,
    read_limiter=_wiki_read_limiter,
    moderation_key=settings.wiki_moderation_admin_key,
    moderator_actor=_wiki_moderator_actor,
)


def _validate_wiki_configuration() -> None:
    if settings.wiki_session_ttl_seconds < 300:
        raise RuntimeError("MHB_WIKI_SESSION_TTL_SECONDS must be at least 300")
    if not settings.wiki_public_writes:
        return
    if not settings.wiki_database_url:
        raise RuntimeError(
            "MHB_WIKI_DATABASE_URL is required when wiki writes are enabled"
        )
    if len(settings.wiki_session_secret.encode("utf-8")) < 32:
        raise RuntimeError("MHB_WIKI_SESSION_SECRET must be at least 32 bytes")
    if len(settings.wiki_moderation_admin_key.encode("utf-8")) < 32:
        raise RuntimeError("MHB_WIKI_MODERATION_ADMIN_KEY must be at least 32 bytes")
    if settings.wiki_moderation_admin_key == settings.wiki_session_secret:
        raise RuntimeError("wiki moderation and session-signing secrets must be distinct")
    if settings.wiki_require_redis and not settings.redis_url:
        raise RuntimeError(
            "MHB_REDIS_URL is required when public wiki writes are enabled"
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup: ensure the feedback TTL index exists (no-op without Mongo)
    await store.ensure_indexes()
    # startup: ensure the MCP registry unique-name index (no-op without Mongo)
    await mcp_registry_store.ensure_indexes()

    _validate_wiki_configuration()
    if settings.wiki_public_writes:
        limiter_ready = [
            await limiter.ready()
            for limiter in (
                _wiki_session_limiter,
                _wiki_mutation_limiter,
                _wiki_read_limiter,
            )
        ]
        if not all(limiter_ready):
            raise RuntimeError("Redis rate limiter is required but unavailable")

    if settings.wiki_database_url:
        wiki_store = await PostgresWikiStore.connect(
            settings.wiki_database_url,
            min_size=1,
            max_size=10,
            command_timeout=10,
        )
        try:
            await wiki_store.ensure_schema()
            if not await wiki_store.ping():
                raise RuntimeError("wiki PostgreSQL schema readiness check failed")
        except Exception:
            await wiki_store.close()
            raise
        wiki_runtime.store = wiki_store
        wiki_runtime.ready = True
    else:
        wiki_runtime.ready = not settings.wiki_public_writes

    app.state.wiki_runtime = wiki_runtime
    try:
        yield
    finally:
        # graceful shutdown — isolate each close so one failure can't leak the rest
        import asyncio

        from .routers.feedback import _limiter

        await asyncio.gather(
            kg.close(),
            store.close(),
            mcp_registry_store.close(),
            _limiter.close(),
            wiki_runtime.store.close(),
            _wiki_session_limiter.close(),
            _wiki_mutation_limiter.close(),
            _wiki_read_limiter.close(),
            return_exceptions=True,
        )


app = FastAPI(
    title="metahumotonic-web-back",
    version=__version__,
    description="Live KG surfaces, feedback, and the revisioned community wiki.",
    lifespan=lifespan,
)

# logging added first → inner; CORS added last → outermost, so our synthesized
# 500 (except branch) flows back out through CORS and gets ACAO headers — a
# cross-origin client can read the 500 body. [gate F-cors]
app.add_middleware(RequestLoggingMiddleware)
app.add_middleware(WikiBodyLimitMiddleware, max_bytes=settings.wiki_max_body_bytes)
app.add_middleware(WikiRobotsMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list(),
    allow_methods=["GET", "POST", "PUT", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-CSRF-Token"],
    allow_credentials=True,
)

app.include_router(meta.router)
app.include_router(stats.router)
app.include_router(domains.router)
app.include_router(skills.router)
app.include_router(research.router)
app.include_router(feedback.router)
app.include_router(feedback.internal_router)
app.include_router(kg_proxy.router)
app.include_router(mcp_registry.router)
app.include_router(create_router(wiki_runtime))
app.include_router(create_moderation_router(wiki_runtime))


@app.get("/.well-known/mcp-servers.json", include_in_schema=False)
async def well_known_mcp_servers() -> RedirectResponse:
    """Well-known MCP discovery alias (H-01) — agents probing the standard
    path land on the canonical live manifest."""
    return RedirectResponse(url="/api/mcp/manifest", status_code=302)


instrument(app)  # Prometheus /metrics (PROM16 C6)
