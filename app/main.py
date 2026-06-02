"""FastAPI app entrypoint.

  uvicorn app.main:app --reload
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .config import settings
from .kg import kg
from .routers import domains, feedback, meta, skills, stats
from .store import store


@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup: ensure the feedback TTL index exists (no-op without Mongo)
    await store.ensure_indexes()
    yield
    # graceful shutdown of lazy clients
    await kg.close()
    await store.close()


app = FastAPI(
    title="metahumotonic-web-back",
    version=__version__,
    description="Live KG stats + feedback intake for metahumotonic-web.",
    lifespan=lifespan,
)

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
app.include_router(feedback.router)
