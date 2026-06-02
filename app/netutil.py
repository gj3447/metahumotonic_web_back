"""Shared client-IP resolution (used by rate limiting + request logging).

PROM16 A3S2: prefer the proxy-appended client IP (CF-Connecting-IP / rightmost
X-Forwarded-For) over the spoofable leftmost entry, when trust_proxy is set.
"""

from __future__ import annotations

from starlette.requests import Request

from .config import settings


def client_key(request: Request) -> str:
    if settings.trust_proxy:
        cf = request.headers.get("cf-connecting-ip")
        if cf:
            return cf.strip()
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"
