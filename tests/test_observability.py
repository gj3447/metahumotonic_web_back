"""PROM16 C6 metrics + Turnstile gate (disabled by default)."""

from app.turnstile import enabled, verify


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
