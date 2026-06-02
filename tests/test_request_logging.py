"""APT SCW — RequestLoggingMiddleware, contract revised at the Naesengmoon gate.

P1 every non-excluded response → X-Request-ID = server 16-hex (never client-set)
P2 inbound X-Request-ID → logged as correlation_id (NOT echoed back)
P3 exactly one "request" log per non-excluded request, incl. unhandled→500
P4 excluded paths (/metrics,/health,/ready) emit no request log
"""

import re

import structlog

from app.main import app

# test-only route that raises an unhandled exception (gate F1 path)
async def _boom(request):  # pragma: no cover - exercised via client
    raise RuntimeError("boom")


app.add_route("/_boom", _boom)


async def _slow(request):  # pragma: no cover - exercised via client
    import asyncio

    from starlette.responses import JSONResponse

    await asyncio.sleep(0.05)
    return JSONResponse({"ok": True})


app.add_route("/_slow", _slow)


async def test_response_has_server_request_id(client):
    r = await client.get("/api/stats")
    rid = r.headers.get("x-request-id")
    assert rid is not None and re.fullmatch(r"[0-9a-f]{16}", rid)


async def test_inbound_id_not_echoed_but_correlated(client):
    # F3/F4: inbound is NOT trusted as the X-Request-ID; a fresh server id is used
    with structlog.testing.capture_logs() as logs:
        r = await client.get("/api/stats", headers={"X-Request-ID": "client-spoof-1"})
    assert r.headers["x-request-id"] != "client-spoof-1"
    assert re.fullmatch(r"[0-9a-f]{16}", r.headers["x-request-id"])
    ev = next(e for e in logs if e.get("event") == "request")
    assert ev["correlation_id"] == "client-spoof-1"        # surfaced, untrusted
    assert ev["request_id"] != "client-spoof-1"            # primary key is server's


async def test_one_structured_log_event_with_fields(client):
    with structlog.testing.capture_logs() as logs:
        await client.get("/api/skills")
    events = [e for e in logs if e.get("event") == "request"]
    assert len(events) == 1
    ev = events[0]
    assert ev["method"] == "GET" and ev["path"] == "/api/skills"
    assert ev["status_code"] == 200
    assert re.fullmatch(r"[0-9a-f]{16}", ev["request_id"])
    assert isinstance(ev["duration_ms"], (int, float)) and ev["duration_ms"] >= 0


async def test_unhandled_exception_still_logged_and_headered(client):
    # gate F1/F2: BaseHTTPMiddleware would drop both; pure-ASGI must keep them
    with structlog.testing.capture_logs() as logs:
        r = await client.get("/_boom")
    assert r.status_code == 500
    assert re.fullmatch(r"[0-9a-f]{16}", r.headers.get("x-request-id", ""))
    events = [e for e in logs if e.get("event") == "request"]
    assert len(events) == 1 and events[0]["status_code"] == 500


async def test_excluded_paths_not_logged(client):
    with structlog.testing.capture_logs() as logs:
        await client.get("/health")
        await client.get("/ready")
    assert [e for e in logs if e.get("event") == "request"] == []


async def test_duration_covers_full_response_not_ttfb(client):
    # gate F-dur: ~50ms handler → duration measured at the final body frame
    with structlog.testing.capture_logs() as logs:
        await client.get("/_slow")
    ev = next(e for e in logs if e.get("event") == "request" and e["path"] == "/_slow")
    assert ev["duration_ms"] >= 45  # would be ~0 if measured at response.start


async def test_inbound_correlation_id_injection_is_rejected(client):
    # gate F6: a CR/LF-bearing or oversized inbound id must not reach the log
    bad = "abc\ndef forged"
    with structlog.testing.capture_logs() as logs:
        await client.get("/api/stats", headers={"X-Request-ID": bad})
    ev = next(e for e in logs if e.get("event") == "request")
    assert "correlation_id" not in ev  # regex fullmatch drops it entirely
