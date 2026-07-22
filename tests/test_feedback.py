from app.routers.feedback import _limiter
from app.store import store


async def test_feedback_success_persists(client):
    r = await client.post(
        "/api/feedback",
        json={
            "type": "bug",
            "subject": "broken link",
            "body": "the /333 page 404s",
            "source_path": "/compute/",
            "contact_consent": False,
        },
    )
    assert r.status_code == 200
    payload = r.json()
    assert payload["ok"] is True and payload["id"]
    assert payload["status"] == "accepted"
    assert len(store.memory) == 1
    assert store.memory[0]["subject"] == "broken link"
    assert store.memory[0]["source_path"] == "/compute/"
    assert "source_ip" not in store.memory[0]
    assert "user_agent" not in store.memory[0]


async def test_honeypot_is_silently_dropped(client):
    r = await client.post(
        "/api/feedback",
        json={"subject": "spam", "body": "buy now", "honeypot": "i-am-a-bot"},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert len(store.memory) == 0  # nothing stored


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


async def test_feedback_accepts_product_categories(client):
    for kind in ("thesis", "compute", "collaboration"):
        r = await client.post(
            "/api/feedback",
            json={"type": kind, "subject": kind, "body": "one clear thought"},
        )
        assert r.status_code == 200


async def test_feedback_rejects_external_source_and_malformed_email(client):
    external = await client.post(
        "/api/feedback",
        json={
            "subject": "s",
            "body": "b",
            "source_path": "https://evil.example/track",
        },
    )
    assert external.status_code == 422

    bad_email = await client.post(
        "/api/feedback",
        json={"subject": "s", "body": "b", "email": "not-an-email"},
    )
    assert bad_email.status_code == 422


async def test_durable_mode_refuses_false_success(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "feedback_require_durable", True)
    r = await client.post(
        "/api/feedback", json={"subject": "s", "body": "b"}
    )
    assert r.status_code == 503
    assert r.json() == {"reason": "storage_unavailable", "retryable": True}


async def test_operator_inbox_is_disabled_without_key(client):
    assert (await client.get("/api/feedback")).status_code == 405
    r = await client.get("/internal/feedback")
    assert r.status_code == 503


async def test_operator_inbox_requires_key_and_returns_newest_first(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "feedback_admin_key", "test-inbox-key")
    first = await client.post(
        "/api/feedback",
        json={"type": "thesis", "subject": "first", "body": "one"},
    )
    second = await client.post(
        "/api/feedback",
        json={
            "type": "collaboration",
            "subject": "second",
            "body": "two",
            "email": "agent@example.com",
            "contact_consent": True,
        },
    )
    assert first.status_code == second.status_code == 200

    denied = await client.get("/internal/feedback", headers={"X-API-Key": "wrong"})
    assert denied.status_code == 401

    inbox = await client.get(
        "/internal/feedback?limit=1", headers={"X-API-Key": "test-inbox-key"}
    )
    assert inbox.status_code == 200
    assert inbox.headers["cache-control"] == "private, no-store"
    payload = inbox.json()
    assert payload["count"] == 1
    assert payload["items"][0]["subject"] == "second"
    assert payload["items"][0]["contact_consent"] is True
    assert payload["items"][0]["status"] == "new"
    assert "source_ip" not in payload["items"][0]
    assert "user_agent" not in payload["items"][0]

    record_id = payload["items"][0]["id"]
    reviewed = await client.patch(
        f"/internal/feedback/{record_id}",
        headers={"X-API-Key": "test-inbox-key"},
        json={"status": "reviewed", "operator_note": "actionable"},
    )
    assert reviewed.status_code == 200
    assert reviewed.headers["cache-control"] == "private, no-store"
    assert reviewed.json()["item"]["status"] == "reviewed"
    assert reviewed.json()["item"]["operator_note"] == "actionable"

    archived = await client.patch(
        f"/internal/feedback/{record_id}",
        headers={"X-API-Key": "test-inbox-key"},
        json={"status": "archived"},
    )
    assert archived.status_code == 200
    invalid = await client.patch(
        f"/internal/feedback/{record_id}",
        headers={"X-API-Key": "test-inbox-key"},
        json={"status": "reviewed"},
    )
    assert invalid.status_code == 409

    deleted = await client.delete(
        f"/internal/feedback/{record_id}",
        headers={"X-API-Key": "test-inbox-key"},
    )
    assert deleted.status_code == 204
    assert deleted.headers["cache-control"] == "private, no-store"
    missing = await client.delete(
        f"/internal/feedback/{record_id}",
        headers={"X-API-Key": "test-inbox-key"},
    )
    assert missing.status_code == 404


async def test_operator_inbox_fails_closed_in_durable_mode(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "feedback_admin_key", "test-inbox-key")
    monkeypatch.setattr(settings, "feedback_require_durable", True)
    inbox = await client.get(
        "/internal/feedback", headers={"X-API-Key": "test-inbox-key"}
    )
    assert inbox.status_code == 503


async def test_email_requires_explicit_contact_consent(client):
    r = await client.post(
        "/api/feedback",
        json={
            "subject": "contact me",
            "body": "hello",
            "email": "agent@example.com",
        },
    )
    assert r.status_code == 422


async def test_blank_visible_content_is_rejected(client):
    subject = await client.post(
        "/api/feedback", json={"subject": "   ", "body": "body"}
    )
    body = await client.post(
        "/api/feedback", json={"subject": "subject", "body": " \n\t "}
    )
    assert subject.status_code == 422
    assert body.status_code == 422
