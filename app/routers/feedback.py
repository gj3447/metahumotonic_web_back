"""POST /api/feedback — honeypot + rate-limit + persist.

Matches feedback-form.js expectations:
  - 200 {ok, id}          on success
  - 200 {ok}              silently for honeypot-tripped bots
  - 429 {reason}          when the per-IP window is exceeded
  - 422 (FastAPI default) on validation error
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..config import settings
from ..contracts import FeedbackRequest, FeedbackResponse
from ..netutil import client_key as _client_key
from ..ratelimit import RateLimiter
from ..store import store
from ..turnstile import verify as verify_turnstile

router = APIRouter(prefix="/api")

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

    # Turnstile (no-op unless a secret is configured)
    if not await verify_turnstile(payload.turnstile_token, _client_key(request)):
        return JSONResponse(status_code=403, content={"reason": "challenge_failed"})

    if not await _limiter.allow(_client_key(request)):
        return JSONResponse(
            status_code=429,
            content={"reason": "rate_limited"},
        )

    record_id = await store.save(
        {
            "type": payload.type,
            "subject": payload.subject,
            "body": payload.body,
            "email": payload.email,
            "source_ip": _client_key(request),
            "user_agent": request.headers.get("user-agent", ""),
        }
    )
    return FeedbackResponse(ok=True, id=record_id)
