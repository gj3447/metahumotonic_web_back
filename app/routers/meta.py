"""Health + root."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import __version__
from ..config import settings
from ..kg import kg
from ..ontology import ontology_runtime

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    # Liveness — process is up. MUST NOT touch external deps (else a DB blip
    # restart-loops the pod). PROM16 C5.
    return {"status": "ok", "service": "metahumotonic-web-back", "version": __version__}


@router.get("/ready", response_model=None)
async def ready(request: Request) -> Any:
    # Readiness — dependency-aware but degraded-tolerant: the service serves
    # snapshot/in-memory fallbacks when KG/Mongo are down, so it stays READY
    # and just reports the degraded signal. PROM16 C5.
    kg_live = False
    if settings.neo4j_live:
        try:
            rows = await kg._run("RETURN 1 AS ok")
            kg_live = bool(rows)
        except Exception:  # noqa: BLE001 - readiness must degrade on any driver failure
            kg_live = False
    wiki_required = settings.wiki_public_writes
    wiki_store_live = False
    wiki_rate_limit_live = not (wiki_required and settings.wiki_require_redis)
    runtime = getattr(request.app.state, "wiki_runtime", None)
    if runtime is not None and runtime.ready:
        try:
            wiki_store_live = bool(await runtime.store.ping())
        except Exception:  # noqa: BLE001 - readiness must fail closed on any store failure
            wiki_store_live = False
        if wiki_required and settings.wiki_require_redis:
            try:
                wiki_rate_limit_live = all(
                    [
                        await limiter.ready()
                        for limiter in (
                            runtime.session_limiter,
                            runtime.mutation_limiter,
                            runtime.read_limiter,
                        )
                        if limiter is not None
                    ]
                )
            except Exception:  # noqa: BLE001 - readiness must fail closed on limiter failure
                wiki_rate_limit_live = False
    wiki_live = wiki_store_live and wiki_rate_limit_live
    ontology_required = settings.ontology_enabled
    ontology_live = bool(
        ontology_runtime.enabled
        and ontology_runtime.ready
        and ontology_runtime.projection is not None
    )
    degraded = (settings.neo4j_live and not kg_live) or (
        wiki_required and not wiki_live
    ) or (ontology_required and not ontology_live)
    not_ready = (wiki_required and not wiki_live) or (
        ontology_required and not ontology_live
    )
    payload = {
        "status": "ready" if not not_ready else "not_ready",
        "kg_live": kg_live,
        "wiki_required": wiki_required,
        "wiki_live": wiki_live,
        "wiki_store_live": wiki_store_live,
        "wiki_rate_limit_live": wiki_rate_limit_live,
        "ontology_required": ontology_required,
        "ontology_live": ontology_live,
        "degraded": degraded,
    }
    if not_ready:
        return JSONResponse(status_code=503, content=payload)
    return payload


@router.get("/")
async def root() -> dict:
    return {
        "service": "metahumotonic-web-back",
        "version": __version__,
        "endpoints": [
            "/health",
            "/ready",
            "/api/stats",
            "/api/domains",
            "/api/skills",
            "/api/feedback",
            "/api/wiki/v1",
            "/api/v1/ontology/schema",
            "/api/v1/ontology/conflicts",
        ],
    }
