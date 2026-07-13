"""PROM16 C6 metrics + Turnstile gate (disabled by default)."""

from app import observability
from app.observability import configure_logging, get_logger, instrument
from app.turnstile import enabled, verify


def test_configure_logging_runs_without_error():
    configure_logging()  # idempotent; must not raise


def test_get_logger_returns_usable_logger():
    configure_logging()
    log = get_logger("test")
    assert log is not None
    # emitting must not raise, with or without structured kwargs
    log.info("hello", k=1)


def test_instrument_noop_when_metrics_disabled(monkeypatch):
    monkeypatch.setattr(observability.settings, "metrics_enabled", False)
    sentinel = object()  # would blow up if instrument actually touched the app
    assert instrument(sentinel) is None  # early return, no-op


async def test_metrics_endpoint_exposed(client):
    r = await client.get("/metrics")
    assert r.status_code == 200
    assert "http_request" in r.text  # prometheus exposition


async def test_turnstile_disabled_passes_by_default():
    assert enabled() is False
    assert await verify("") is True  # no token, but feature off → pass


async def test_feedback_still_works_without_turnstile(client):
    r = await client.post("/api/feedback", json={"subject": "s", "body": "b"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
