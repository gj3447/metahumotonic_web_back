"""Health + root."""

from __future__ import annotations

from fastapi import APIRouter

from .. import __version__

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "metahumotonic-web-back", "version": __version__}


@router.get("/")
async def root() -> dict:
    return {
        "service": "metahumotonic-web-back",
        "version": __version__,
        "endpoints": ["/health", "/api/stats", "/api/domains", "/api/skills", "/api/feedback"],
    }
