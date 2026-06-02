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
from ..ratelimit import SlidingWindowRateLimiter
from ..store import store

router = APIRouter(prefix="/api")

_limiter = SlidingWindowRateLimiter(
    max_events=settings.feedback_max_per_window,
    window_seconds=settings.feedback_window_seconds,
)


def _client_key(request: Request) -> str:
    # Honor reverse-proxy header (Traefik/nginx) then fall back to socket peer.
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@router.post("/feedback", response_model=FeedbackResponse)
async def post_feedback(payload: FeedbackRequest, request: Request):
    # Bot trap: pretend success, store nothing.
    if payload.honeypot:
        return FeedbackResponse(ok=True)

    if not _limiter.allow(_client_key(request)):
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
