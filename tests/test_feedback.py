from app.routers.feedback import _limiter
from app.store import store


async def test_feedback_success_persists(client):
    r = await client.post(
        "/api/feedback",
        json={"type": "bug", "subject": "broken link", "body": "the /333 page 404s"},
    )
    assert r.status_code == 200
    payload = r.json()
    assert payload["ok"] is True and payload["id"]
    assert len(store.memory) == 1
    assert store.memory[0]["subject"] == "broken link"


async def test_honeypot_is_silently_dropped(client):
    r = await client.post(
        "/api/feedback",
        json={"subject": "spam", "body": "buy now", "honeypot": "i-am-a-bot"},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert store.memory == []  # nothing stored


async def test_missing_required_fields_422(client):
    r = await client.post("/api/feedback", json={"subject": "only subject"})
    assert r.status_code == 422


async def test_rate_limit_returns_429(client):
    _limiter.max_events = 3
    for _ in range(3):
        ok = await client.post(
            "/api/feedback", json={"subject": "s", "body": "b"}
        )
        assert ok.status_code == 200
    blocked = await client.post("/api/feedback", json={"subject": "s", "body": "b"})
    assert blocked.status_code == 429
    assert blocked.json()["reason"] == "rate_limited"
