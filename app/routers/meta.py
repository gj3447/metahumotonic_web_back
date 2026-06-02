"""Health + root."""

from __future__ import annotations

from fastapi import APIRouter

from .. import __version__
from ..config import settings
from ..kg import kg

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    # Liveness — process is up. MUST NOT touch external deps (else a DB blip
    # restart-loops the pod). PROM16 C5.
    return {"status": "ok", "service": "metahumotonic-web-back", "version": __version__}


@router.get("/ready")
async def ready() -> dict:
    # Readiness — dependency-aware but degraded-tolerant: the service serves
    # snapshot/in-memory fallbacks when KG/Mongo are down, so it stays READY
    # and just reports the degraded signal. PROM16 C5.
    kg_live = False
    if settings.neo4j_live:
        try:
            rows = await kg._run("RETURN 1 AS ok")  # noqa: SLF001
            kg_live = bool(rows)
        except Exception:
            kg_live = False
    return {"status": "ready", "kg_live": kg_live, "degraded": settings.neo4j_live and not kg_live}


@router.get("/")
async def root() -> dict:
    return {
        "service": "metahumotonic-web-back",
        "version": __version__,
        "endpoints": ["/health", "/ready", "/api/stats", "/api/domains", "/api/skills", "/api/feedback"],
    }
