"""PROM16 C6 metrics + Turnstile gate (disabled by default)."""

import httpx

from app import observability
from app.config import settings
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


class _TurnstileResponse:
    def __init__(self, body):
        self._body = body
        self.status_code = 200

    def json(self):
        return self._body


class _TurnstileClient:
    body = {}
    error = None

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, *_args, **_kwargs):
        if self.error:
            raise self.error
        return _TurnstileResponse(self.body)


async def test_turnstile_checks_hostname_and_action(monkeypatch):
    monkeypatch.setattr(settings, "turnstile_secret", "secret")
    monkeypatch.setattr(settings, "turnstile_hostname", "metahumotonic.com")
    monkeypatch.setattr(settings, "turnstile_action", "feedback_submit")
    monkeypatch.setattr(httpx, "AsyncClient", _TurnstileClient)

    _TurnstileClient.error = None
    _TurnstileClient.body = {
        "success": True,
        "hostname": "metahumotonic.com",
        "action": "feedback_submit",
    }
    assert await verify("token") is True

    _TurnstileClient.body = {
        "success": True,
        "hostname": "other.example",
        "action": "feedback_submit",
    }
    assert await verify("token") is False

    _TurnstileClient.body = {
        "success": True,
        "hostname": "metahumotonic.com",
        "action": "other_action",
    }
    assert await verify("token") is False


async def test_turnstile_transport_failure_is_closed_by_default(monkeypatch):
    monkeypatch.setattr(settings, "turnstile_secret", "secret")
    monkeypatch.setattr(settings, "turnstile_fail_open", False)
    monkeypatch.setattr(httpx, "AsyncClient", _TurnstileClient)
    _TurnstileClient.error = httpx.TransportError("offline")
    try:
        assert await verify("token") is False
    finally:
        _TurnstileClient.error = None
