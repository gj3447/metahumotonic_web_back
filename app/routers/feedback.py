"""Public feedback intake plus a private operator inbox.

Matches feedback-form.js expectations:
  - 200 {ok, id}          on success
  - 200 {ok}              silently for honeypot-tripped bots
  - 429 {reason}          when the per-IP window is exceeded
  - 422 (FastAPI default) on validation error
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from ..config import settings
from ..contracts import (
    FeedbackInboxResponse,
    FeedbackRecord,
    FeedbackRequest,
    FeedbackResponse,
    FeedbackTriageRequest,
    FeedbackTriageResponse,
)
from ..netutil import client_key as _client_key
from ..ratelimit import RateLimiter
from ..store import FeedbackStoreUnavailable, store
from ..turnstile import verify as verify_turnstile

router = APIRouter(prefix="/api")
internal_router = APIRouter(prefix="/internal")

_limiter = RateLimiter(
    max_events=settings.feedback_max_per_window,
    window_seconds=settings.feedback_window_seconds,
    redis_url=settings.redis_url,
)


@router.post("/feedback", response_model=FeedbackResponse)
async def post_feedback(payload: FeedbackRequest, request: Request):
    # Bot trap: pretend success, store nothing.
    if payload.honeypot:
        return FeedbackResponse(ok=True)

    if not await _limiter.allow(_client_key(request)):
        return JSONResponse(
            status_code=429,
            content={"reason": "rate_limited"},
        )

    # Turnstile (no-op unless a secret is configured). Rate-limit first so
    # invalid-token floods cannot force unbounded verifier calls.
    if not await verify_turnstile(payload.turnstile_token, _client_key(request)):
        return JSONResponse(status_code=403, content={"reason": "challenge_failed"})

    result = await store.save_result(
        {
            "type": payload.type,
            "subject": payload.subject,
            "body": payload.body,
            "email": payload.email,
            "source_path": payload.source_path,
            "contact_consent": payload.contact_consent,
            "status": "new",
        }
    )
    if settings.feedback_require_durable and not result.durable:
        return JSONResponse(
            status_code=503,
            content={"reason": "storage_unavailable", "retryable": True},
        )
    return FeedbackResponse(
        ok=True,
        id=result.id,
        status="stored" if result.durable else "accepted",
    )


def _require_admin(request: Request) -> None:
    configured = settings.feedback_admin_key
    if not configured:
        raise HTTPException(status_code=503, detail="feedback inbox disabled")
    supplied = request.headers.get("x-api-key", "")
    if not supplied or not secrets.compare_digest(supplied, configured):
        raise HTTPException(status_code=401, detail="invalid feedback inbox key")


def _record_from_doc(record: dict) -> FeedbackRecord:
    return FeedbackRecord(
        id=str(record.get("_id", "")),
        created_at=record["created_at"],
        type=record.get("type", "general"),
        subject=record.get("subject", ""),
        body=record.get("body", ""),
        email=record.get("email", ""),
        source_path=record.get("source_path", "/"),
        contact_consent=bool(record.get("contact_consent", False)),
        status=record.get("status", "new"),
        operator_note=record.get("operator_note", ""),
        reviewed_at=record.get("reviewed_at"),
    )


@internal_router.get("/feedback", response_model=FeedbackInboxResponse)
async def get_feedback_inbox(
    request: Request,
    response: Response,
    limit: int = Query(default=50, ge=1, le=100),
):
    """Newest-first inbox. Disabled by default and never publicly enumerable."""
    _require_admin(request)
    response.headers["Cache-Control"] = "private, no-store"
    try:
        records = await store.recent(limit=limit)
    except FeedbackStoreUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    items = [_record_from_doc(record) for record in records]
    return FeedbackInboxResponse(items=items, count=len(items))


@internal_router.patch("/feedback/{record_id}", response_model=FeedbackTriageResponse)
async def triage_feedback(
    record_id: str,
    payload: FeedbackTriageRequest,
    request: Request,
    response: Response,
):
    """Move one inbox item through the bounded operator review lifecycle."""
    _require_admin(request)
    response.headers["Cache-Control"] = "private, no-store"
    if len(record_id) != 32 or any(ch not in "0123456789abcdef" for ch in record_id):
        raise HTTPException(status_code=404, detail="feedback not found")
    try:
        record = await store.triage(
            record_id,
            status=payload.status,
            operator_note=payload.operator_note,
        )
    except FeedbackStoreUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if record is None:
        raise HTTPException(
            status_code=409,
            detail="feedback not found or transition is not allowed",
        )
    return FeedbackTriageResponse(item=_record_from_doc(record))


@internal_router.delete("/feedback/{record_id}", status_code=204)
async def delete_feedback(record_id: str, request: Request, response: Response):
    """Permanently erase an inbox item, including an optional contact address."""
    _require_admin(request)
    response.headers["Cache-Control"] = "private, no-store"
    if len(record_id) != 32 or any(ch not in "0123456789abcdef" for ch in record_id):
        raise HTTPException(status_code=404, detail="feedback not found")
    try:
        deleted = await store.delete(record_id)
    except FeedbackStoreUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="feedback not found")
    response.status_code = 204
