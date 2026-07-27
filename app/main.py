"""FastAPI app entrypoint.

  uvicorn app.main:app --reload
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from . import __version__
from .config import settings
from .kg import kg
from .mcp_store import registry as mcp_registry_store
from .middleware import RequestLoggingMiddleware
from .observability import configure_logging, instrument
from .routers import domains, feedback, kg_proxy, mcp_registry, meta, research, skills, stats
from .store import store

configure_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup: ensure the feedback TTL index exists (no-op without Mongo)
    await store.ensure_indexes()
    # startup: ensure the MCP registry unique-name index (no-op without Mongo)
    await mcp_registry_store.ensure_indexes()
    yield
    # graceful shutdown — isolate each close so one failure can't leak the rest
    import asyncio

    from .routers.feedback import _limiter

    await asyncio.gather(
        kg.close(),
        store.close(),
        mcp_registry_store.close(),
        _limiter.close(),
        return_exceptions=True,
    )


app = FastAPI(
    title="metahumotonic-web-back",
    version=__version__,
    description="Live KG stats + feedback intake for metahumotonic-web.",
    lifespan=lifespan,
)

# logging added first → inner; CORS added last → outermost, so our synthesized
# 500 (except branch) flows back out through CORS and gets ACAO headers — a
# cross-origin client can read the 500 body. [gate F-cors]
app.add_middleware(RequestLoggingMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list(),
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
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


@app.get("/.well-known/mcp-servers.json", include_in_schema=False)
async def well_known_mcp_servers() -> RedirectResponse:
    """Well-known MCP discovery alias (H-01) — agents probing the standard
    path land on the canonical live manifest."""
    return RedirectResponse(url="/api/mcp/manifest", status_code=302)


instrument(app)  # Prometheus /metrics (PROM16 C6)
