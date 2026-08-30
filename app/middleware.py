"""Request logging + correlation ID (APT SCW — completes PROM16 C6).

Pure ASGI (NOT BaseHTTPMiddleware) so the X-Request-ID header + the access log
hold across normal, 4xx, handled-5xx, streaming, AND unhandled-exception paths
— BaseHTTPMiddleware cannot set headers on framework-generated 500s
(Naesengmoon gate F1/F2, encode/starlette#1729).

Contract (revised at gate, F3/F4):
  request_id      — ALWAYS server-generated 16-hex; the X-Request-ID response
                    header and the primary trace key. Never client-settable
                    (no log/trace spoofing).
  correlation_id  — a valid inbound X-Request-ID, logged separately (echo of
                    the caller's id for cross-system correlation, untrusted).
Postconditions:
  P1 every non-excluded response carries X-Request-ID = server 16-hex
  P2 inbound X-Request-ID surfaces as `correlation_id` in the log (not echoed)
  P3 exactly one "request" log per non-excluded request, incl. unhandled→500
  P4 excluded paths (/metrics,/health,/ready) emit no request log
"""

from __future__ import annotations

import re
import time
import uuid

import structlog
from starlette.datastructures import MutableHeaders
from starlette.requests import Request

from .netutil import client_key

log = structlog.get_logger("mhb.access")

_EXCLUDED = {"/metrics", "/health", "/ready"}
_CID_RE = re.compile(r"[0-9A-Za-z._-]{1,64}")
_500_BODY = b'{"detail":"Internal Server Error"}'


def _inbound_correlation_id(request: Request) -> str | None:
    cid = request.headers.get("x-request-id")
    return cid if cid and _CID_RE.fullmatch(cid) else None


class RequestLoggingMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        rid = uuid.uuid4().hex[:16]            # server-generated, authoritative
        scope.setdefault("state", {})["request_id"] = rid
        correlation_id = _inbound_correlation_id(request)
        path = scope.get("path", "")
        method = scope.get("method", "")
        start = time.perf_counter()
        state = {"started": False, "logged": False, "status": 500}

        ctx = {"request_id": rid}
        if correlation_id:
            ctx["correlation_id"] = correlation_id
        structlog.contextvars.bind_contextvars(**ctx)

        def _emit(status_code: int) -> None:
            if state["logged"]:
                return
            # Excluded paths: skip only SUCCESSFUL logs; always record errors
            # (a failing /health/metrics is exactly what we want logged). [gate F5]
            if path in _EXCLUDED and status_code < 400:
                return
            state["logged"] = True
            log.info(
                "request",
                request_id=rid,
                method=method,
                path=path,
                status_code=status_code,
                duration_ms=round((time.perf_counter() - start) * 1000, 2),
                client=client_key(request),
                **({"correlation_id": correlation_id} if correlation_id else {}),
            )

        async def send_wrapper(message):
            t = message["type"]
            if t == "http.response.start":
                state["started"] = True
                state["status"] = message["status"]
                MutableHeaders(raw=message.setdefault("headers", []))["X-Request-ID"] = rid
            elif t == "http.response.body" and not message.get("more_body", False):
                # final body frame → duration covers the full response [gate F-dur]
                _emit(state["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            # Unhandled: ExceptionMiddleware (inside us) already handles
            # HTTPException/registered handlers, so only true bugs reach here.
            log.exception("request_failed", request_id=rid, method=method, path=path)
            _emit(500)  # record the failure as 500 even if a start frame said 2xx
            if not state["started"]:
                # P1+P3 for 500s: emit our own response carrying the header.
                await send(
                    {
                        "type": "http.response.start",
                        "status": 500,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"x-request-id", rid.encode()),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": _500_BODY})
            else:
                raise  # response already streaming; can't inject a clean 500
        finally:
            structlog.contextvars.unbind_contextvars("request_id", "correlation_id")
