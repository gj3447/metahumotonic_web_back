"""Internal, read-only API for the sanitized Metahumotonic ontology."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from ..ontology import (
    COLLECTION_KINDS,
    PROJECTION_ID,
    SCHEMA_VERSION,
    OntologyConflictPending,
    OntologyCursorError,
    OntologyProjection,
    ontology_runtime,
)

router = APIRouter(prefix="/api/v1/ontology", tags=["ontology"])

_PREDICATE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,99}$")
_PRIVATE_CACHE = "private, max-age=60, must-revalidate"
_IMMUTABLE_PRIVATE_CACHE = "private, max-age=31536000, immutable"


def _request_id(request: Request) -> str:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) and value else "unavailable"


def _meta(projection: OntologyProjection | None = None) -> dict[str, Any]:
    if projection is not None:
        return projection.meta_payload()
    return {"schema_version": SCHEMA_VERSION, "projection_id": PROJECTION_ID}


def _headers(
    projection: OntologyProjection | None,
    *,
    etag: str | None = None,
    cache_control: str = _PRIVATE_CACHE,
) -> dict[str, str]:
    headers = {
        "Cache-Control": cache_control,
        "Vary": "X-Ontology-Key",
    }
    if projection is not None:
        headers["X-Content-SHA256"] = projection.content_sha256
    if etag is not None:
        headers["ETag"] = etag
    return headers


def _error(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    *,
    projection: OntologyProjection | None = None,
    details: Any | None = None,
) -> JSONResponse:
    error: dict[str, Any] = {
        "code": code,
        "message": message,
        "request_id": _request_id(request),
    }
    if details is not None:
        error["details"] = details
    return JSONResponse(
        status_code=status_code,
        content={"error": error, "meta": _meta(projection)},
        headers=_headers(projection, cache_control="private, no-store"),
    )


def _authorized_projection(
    request: Request,
) -> tuple[OntologyProjection | None, JSONResponse | None]:
    projection = ontology_runtime.projection
    if not ontology_runtime.enabled or not ontology_runtime.ready or projection is None:
        return None, _error(
            request,
            503,
            "ONTOLOGY_UNAVAILABLE",
            "The internal ontology projection is unavailable.",
        )
    supplied_key = request.headers.get("X-Ontology-Key")
    if not ontology_runtime.authorized(supplied_key):
        return None, _error(
            request,
            401,
            "ONTOLOGY_UNAUTHORIZED",
            "A valid internal ontology key is required.",
            projection=projection,
        )
    return projection, None


def _single_query_value(
    request: Request,
    name: str,
    *,
    required: bool = False,
) -> str | None:
    values = request.query_params.getlist(name)
    if not values:
        if required:
            raise ValueError(f"{name} is required")
        return None
    if len(values) != 1:
        raise ValueError(f"{name} must be supplied once")
    value = values[0]
    if not value:
        raise ValueError(f"{name} must not be empty")
    return value


def _limit(request: Request, *, default: int) -> int:
    raw = _single_query_value(request, "limit")
    if raw is None:
        return default
    if not raw.isascii() or not raw.isdecimal():
        raise ValueError("limit must be an integer")
    value = int(raw)
    if value < 1 or value > 100:
        raise ValueError("limit must be between 1 and 100")
    return value


def _etag_matches(request: Request, etag: str) -> bool:
    raw = request.headers.get("If-None-Match")
    if not raw:
        return False
    for candidate in raw.split(","):
        candidate = candidate.strip()
        if candidate == "*":
            return True
        if candidate.startswith("W/"):
            candidate = candidate[2:].strip()
        if candidate == etag:
            return True
    return False


def _not_modified(
    projection: OntologyProjection,
    *,
    etag: str,
    cache_control: str = _PRIVATE_CACHE,
) -> Response:
    return Response(
        status_code=304,
        headers=_headers(projection, etag=etag, cache_control=cache_control),
    )


def _success(
    projection: OntologyProjection,
    data: Any,
    *,
    etag: str,
    page: dict[str, Any] | None = None,
    cache_control: str = _PRIVATE_CACHE,
) -> JSONResponse:
    content: dict[str, Any] = {"data": data, "meta": projection.meta_payload()}
    if page is not None:
        content["page"] = page
    return JSONResponse(
        content=content,
        headers=_headers(projection, etag=etag, cache_control=cache_control),
    )


@router.get("/search", response_model=None)
async def search(request: Request) -> Any:
    projection, auth_error = _authorized_projection(request)
    if auth_error is not None:
        return auth_error
    assert projection is not None
    try:
        query = _single_query_value(request, "q", required=True)
        assert query is not None
        normalized_query = unicodedata.normalize("NFKC", query).strip()
        if not 2 <= len(normalized_query) <= 500:
            raise ValueError("q must be between 2 and 500 characters")
        kind = _single_query_value(request, "kind")
        if kind is not None and kind not in COLLECTION_KINDS.values():
            raise ValueError("kind is not a supported ontology kind")
        limit = _limit(request, default=20)
        cursor = _single_query_value(request, "cursor")
        page = projection.search(
            normalized_query,
            kind=kind,
            limit=limit,
            cursor=cursor,
        )
    except (ValueError, OntologyCursorError) as exc:
        return _error(
            request,
            400,
            "ONTOLOGY_BAD_REQUEST",
            str(exc),
            projection=projection,
        )
    etag = projection.etag_for(
        "search",
        {
            "q": normalized_query.casefold(),
            "kind": kind,
            "limit": limit,
            "cursor": cursor,
        },
    )
    if _etag_matches(request, etag):
        return _not_modified(projection, etag=etag)
    return _success(
        projection,
        {"items": page.items},
        etag=etag,
        page={"next_cursor": page.next_cursor},
    )


@router.get("/nodes/{public_id}", response_model=None)
async def node(request: Request, public_id: str) -> Any:
    projection, auth_error = _authorized_projection(request)
    if auth_error is not None:
        return auth_error
    assert projection is not None
    try:
        item = projection.get_node(public_id)
    except OntologyConflictPending as exc:
        return _error(
            request,
            409,
            "CONFLICT_PENDING",
            "This ontology slot has no ratified default entity.",
            projection=projection,
            details=projection.conflict_payload(exc.record),
        )
    if item is None:
        return _error(
            request,
            404,
            "ONTOLOGY_NODE_NOT_FOUND",
            "Ontology node not found.",
            projection=projection,
        )
    etag = projection.etag_for("node", {"public_id": public_id})
    if _etag_matches(request, etag):
        return _not_modified(projection, etag=etag)
    return _success(projection, item, etag=etag)


@router.get("/nodes/{public_id}/neighbors", response_model=None)
async def neighbors(request: Request, public_id: str) -> Any:
    projection, auth_error = _authorized_projection(request)
    if auth_error is not None:
        return auth_error
    assert projection is not None
    try:
        direction = _single_query_value(request, "direction") or "both"
        if direction not in {"in", "out", "both"}:
            raise ValueError("direction must be in, out, or both")
        predicate = _single_query_value(request, "predicate")
        if predicate is not None and not _PREDICATE_RE.fullmatch(predicate):
            raise ValueError("predicate must be an uppercase ontology predicate")
        limit = _limit(request, default=50)
        cursor = _single_query_value(request, "cursor")
        page = projection.neighbors(
            public_id,
            direction=direction,
            predicate=predicate,
            limit=limit,
            cursor=cursor,
        )
    except (ValueError, OntologyCursorError) as exc:
        return _error(
            request,
            400,
            "ONTOLOGY_BAD_REQUEST",
            str(exc),
            projection=projection,
        )
    if page is None:
        return _error(
            request,
            404,
            "ONTOLOGY_NODE_NOT_FOUND",
            "Ontology node not found.",
            projection=projection,
        )
    etag = projection.etag_for(
        "neighbors",
        {
            "public_id": public_id,
            "direction": direction,
            "predicate": predicate,
            "limit": limit,
            "cursor": cursor,
        },
    )
    if _etag_matches(request, etag):
        return _not_modified(projection, etag=etag)
    return _success(
        projection,
        {"node_public_id": public_id, "items": page.items},
        etag=etag,
        page={"next_cursor": page.next_cursor},
    )


@router.get("/schema", response_model=None)
async def schema(request: Request) -> Any:
    projection, auth_error = _authorized_projection(request)
    if auth_error is not None:
        return auth_error
    assert projection is not None
    etag = projection.etag_for("schema")
    if _etag_matches(request, etag):
        return _not_modified(projection, etag=etag)
    return _success(projection, projection.schema_document(), etag=etag)


@router.get("/releases/{projection_id}", response_model=None)
async def release(request: Request, projection_id: str) -> Any:
    projection, auth_error = _authorized_projection(request)
    if auth_error is not None:
        return auth_error
    assert projection is not None
    if projection_id != projection.meta["projection_id"]:
        return _error(
            request,
            404,
            "ONTOLOGY_RELEASE_NOT_FOUND",
            "Ontology release not found.",
            projection=projection,
        )
    etag = projection.etag_for("release", {"projection_id": projection_id})
    if _etag_matches(request, etag):
        return _not_modified(
            projection,
            etag=etag,
            cache_control=_IMMUTABLE_PRIVATE_CACHE,
        )
    return _success(
        projection,
        projection.release_document(),
        etag=etag,
        cache_control=_IMMUTABLE_PRIVATE_CACHE,
    )
