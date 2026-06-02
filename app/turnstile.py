"""Cloudflare Turnstile verification (PROM16 A3S3/A3S4).

Disabled unless `turnstile_secret` is set — so it's a no-op today and becomes
active the moment a Cloudflare Turnstile secret is configured (the matching
site key goes on the frontend widget). Fails open on a Cloudflare outage so a
verification-endpoint blip never blocks all feedback.
"""

from __future__ import annotations

import logging

from .config import settings

log = logging.getLogger("mhb.turnstile")

_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


def enabled() -> bool:
    return bool(settings.turnstile_secret)


async def verify(token: str, remote_ip: str | None = None) -> bool:
    if not enabled():
        return True  # feature off → always pass
    if not token:
        return False
    try:
        import httpx

        data = {"secret": settings.turnstile_secret, "response": token}
        if remote_ip:
            data["remoteip"] = remote_ip
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.post(_VERIFY_URL, data=data)
            return bool(r.json().get("success"))
    except Exception as e:  # pragma: no cover - network dependent
        log.warning("turnstile verify failed (fail-open): %s", e)
        return True
