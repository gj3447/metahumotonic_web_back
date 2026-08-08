from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI

from app.routers.wiki import WikiRuntime, create_moderation_router, create_router
from app.wiki.memory import InMemoryWikiStore
from app.wiki.security import SessionSigner

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


@pytest.fixture
def wiki_app():
    store = InMemoryWikiStore()
    runtime = WikiRuntime(
        store,
        SessionSigner(b"test-secret-that-is-at-least-32-bytes-long"),
        now=lambda: NOW,
        writes_enabled=True,
        allowed_origins=frozenset({"https://test"}),
        moderation_key="operator-secret",
        moderator_actor=lambda _request: "moderator:operator",
    )
    app = FastAPI()
    app.include_router(create_router(runtime))
    app.include_router(create_moderation_router(runtime))
    app.state.wiki_store = store
    app.state.wiki_runtime = runtime
    return app


async def session(client: httpx.AsyncClient):
    response = await client.post("/api/wiki/v1/sessions")
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.asyncio
async def test_bearer_create_edit_read_history_diff_search_and_recent(wiki_app):
    transport = httpx.ASGITransport(app=wiki_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://test"
    ) as client:
        auth = await session(client)
        headers = {
            "Authorization": f"Bearer {auth['bearer_token']}",
            "Idempotency-Key": "create-1",
        }
        created = await client.post(
            "/api/wiki/v1/pages",
            headers=headers,
            json={
                "slug": " Hello__World ",
                "title": "Hello",
                "content": "first\n",
                "edit_summary": "start",
            },
        )
        assert created.status_code == 201, created.text
        first = created.json()

        replay = await client.post(
            "/api/wiki/v1/pages",
            headers=headers,
            json={
                "slug": " Hello__World ",
                "title": "Hello",
                "content": "first\n",
                "edit_summary": "start",
            },
        )
        assert replay.status_code == 201
        assert replay.json()["replayed"] is True
        assert replay.json()["page_id"] == first["page_id"]

        page = (await client.get("/api/wiki/v1/pages/hello-world")).json()
        assert page["authority"] == "community"
        assert page["canonical"] is False
        assert page["review_status"] == "unreviewed"
        assert page["head_revision_id"] == first["event_ids"][0]

        edited = await client.post(
            "/api/wiki/v1/pages/hello-world/revisions",
            headers={
                "Authorization": f"Bearer {auth['bearer_token']}",
                "Idempotency-Key": "edit-1",
            },
            json={
                "expected_head_revision_id": page["head_revision_id"],
                "content": "second\n",
                "edit_summary": "edit",
            },
        )
        assert edited.status_code == 200, edited.text
        second_revision = edited.json()["event_ids"][0]
        assert (await client.get("/api/wiki/v1/pages/hello-world")).json()[
            "title"
        ] == "Hello"

        history = await client.get("/api/wiki/v1/pages/hello-world/history")
        assert [item["event_type"] for item in history.json()["items"]] == [
            "revision_committed",
            "page_created",
        ]
        diff = await client.get(
            "/api/wiki/v1/pages/hello-world/diff",
            params={
                "from_revision_id": first["event_ids"][0],
                "to_revision_id": second_revision,
            },
        )
        assert diff.status_code == 200
        assert "-first" in diff.json()["unified_diff"]
        assert "+second" in diff.json()["unified_diff"]

        listing = await client.get("/api/wiki/v1/pages", params={"q": "HELLO"})
        assert [item["slug"] for item in listing.json()["items"]] == ["hello-world"]
        recent = await client.get("/api/wiki/v1/recent-changes")
        assert len(recent.json()["items"]) == 2


@pytest.mark.asyncio
async def test_cookie_mutations_require_csrf_and_submit_only_enqueues_review(wiki_app):
    transport = httpx.ASGITransport(app=wiki_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://test"
    ) as client:
        auth = await session(client)
        rejected = await client.post(
            "/api/wiki/v1/pages",
            headers={"Origin": "https://test"},
            json={"slug": "cookie-page", "title": "Cookie", "content": "content"},
        )
        assert rejected.status_code == 403
        assert rejected.json()["detail"]["code"] == "csrf_failed"

        created = await client.post(
            "/api/wiki/v1/pages",
            headers={"Origin": "https://test", "X-CSRF-Token": auth["csrf_token"]},
            json={"slug": "cookie-page", "title": "Cookie", "content": "content"},
        )
        assert created.status_code == 201
        page = (await client.get("/api/wiki/v1/pages/cookie-page")).json()

        submitted = await client.post(
            "/api/wiki/v1/pages/cookie-page/submit-review",
            headers={"Origin": "https://test", "X-CSRF-Token": auth["csrf_token"]},
            json={
                "revision_id": page["head_revision_id"],
                "content_hash": page["content_hash"],
            },
        )
        assert submitted.status_code == 202
        page = (await client.get("/api/wiki/v1/pages/cookie-page")).json()
        assert page["review_status"] == "submitted"
        outbox = await wiki_app.state.wiki_store.pending_outbox()
        assert [item.effect.effect_type for item in outbox] == ["review_requested"]


@pytest.mark.asyncio
async def test_browser_session_keeps_bearer_out_of_javascript_response(wiki_app):
    transport = httpx.ASGITransport(app=wiki_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://test"
    ) as client:
        response = await client.post(
            "/api/wiki/v1/sessions",
            headers={"Origin": "https://test"},
            json={},
        )
        assert response.status_code == 201
        assert response.json()["access_token"] is None
        assert response.json()["bearer_token"] is None
        assert response.json()["csrf_token"]
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie
        assert "Path=/api/wiki/v1" in cookie
        assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_submit_rejects_stale_revision_or_hash(wiki_app):
    transport = httpx.ASGITransport(app=wiki_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://test"
    ) as client:
        auth = await session(client)
        bearer = {"Authorization": f"Bearer {auth['bearer_token']}"}
        await client.post(
            "/api/wiki/v1/pages",
            headers=bearer,
            json={"slug": "exact", "title": "Exact", "content": "content"},
        )
        page = (await client.get("/api/wiki/v1/pages/exact")).json()
        stale = await client.post(
            "/api/wiki/v1/pages/exact/submit-review",
            headers=bearer,
            json={"revision_id": page["head_revision_id"], "content_hash": "0" * 64},
        )
        assert stale.status_code == 409
        assert stale.json()["detail"]["code"] == "content_hash_conflict"


@pytest.mark.asyncio
async def test_mutation_bodies_reject_actor_and_capabilities(wiki_app):
    transport = httpx.ASGITransport(app=wiki_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://test"
    ) as client:
        auth = await session(client)
        response = await client.post(
            "/api/wiki/v1/pages",
            headers={"Authorization": f"Bearer {auth['bearer_token']}"},
            json={
                "slug": "bad",
                "title": "Bad",
                "content": "content",
                "actor_id": "admin",
                "capabilities": ["wiki:review"],
            },
        )
        assert response.status_code == 422

        session_response = await client.post(
            "/api/wiki/v1/sessions",
            json={"display_name": "Imposter", "actor_id": "admin"},
        )
        assert session_response.status_code == 422


@pytest.mark.asyncio
async def test_server_renders_sanitized_html_and_lists_only_summaries(wiki_app):
    transport = httpx.ASGITransport(app=wiki_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://test"
    ) as client:
        auth = await session(client)
        bearer = {"Authorization": f"Bearer {auth['bearer_token']}"}
        created = await client.post(
            "/api/wiki/v1/pages",
            headers=bearer,
            json={
                "slug": "safe-render",
                "title": "Safe",
                "content": "# Heading\n\n<script>alert(1)</script> [bad](javascript:alert(1))",
            },
        )
        assert created.status_code == 201
        page = (await client.get("/api/wiki/v1/pages/safe-render")).json()
        assert "<h1>Heading</h1>" in page["sanitized_html"]
        assert "<script" not in page["sanitized_html"]
        assert 'href="javascript:' not in page["sanitized_html"]
        listing = (await client.get("/api/wiki/v1/pages")).json()
        assert "content" not in listing["items"][0]
        assert "sanitized_html" not in listing["items"][0]


@pytest.mark.asyncio
async def test_cookie_origin_is_exact_and_review_notes_never_enter_public_history(
    wiki_app,
):
    transport = httpx.ASGITransport(app=wiki_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://test"
    ) as client:
        auth = await session(client)
        foreign = await client.post(
            "/api/wiki/v1/pages",
            headers={
                "Origin": "https://evil.example",
                "X-CSRF-Token": auth["csrf_token"],
            },
            json={"slug": "blocked", "title": "Blocked", "content": "content"},
        )
        assert foreign.status_code == 403
        assert foreign.json()["detail"]["code"] == "origin_forbidden"

        headers = {"Origin": "https://test", "X-CSRF-Token": auth["csrf_token"]}
        await client.post(
            "/api/wiki/v1/pages",
            headers=headers,
            json={"slug": "private-note", "title": "Private", "content": "content"},
        )
        page = (await client.get("/api/wiki/v1/pages/private-note")).json()
        await client.post(
            "/api/wiki/v1/pages/private-note/submit-review",
            headers=headers,
            json={
                "revision_id": page["head_revision_id"],
                "content_hash": page["content_hash"],
                "note": "private reviewer context",
            },
        )
        history = (await client.get("/api/wiki/v1/pages/private-note/history")).json()
        encoded = str(history)
        assert "review_submitted" not in encoded
        assert "private reviewer context" not in encoded
        assert "content" not in history["items"][0]["data"]


@pytest.mark.asyncio
async def test_report_does_not_censor_then_quarantine_hides_every_public_surface(
    wiki_app,
):
    transport = httpx.ASGITransport(app=wiki_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://test"
    ) as client:
        auth = await session(client)
        assert "wiki:moderate" not in auth["scopes"]
        bearer = {"Authorization": f"Bearer {auth['bearer_token']}"}
        created = await client.post(
            "/api/wiki/v1/pages",
            headers=bearer,
            json={"slug": "reported", "title": "Reported", "content": "secret"},
        )
        assert created.status_code == 201
        page = (await client.get("/api/wiki/v1/pages/reported")).json()

        reported = await client.post(
            "/api/wiki/v1/pages/reported/report",
            headers=bearer,
            json={"reason": "operator should inspect"},
        )
        assert reported.status_code == 202
        assert (await client.get("/api/wiki/v1/pages/reported")).status_code == 200

        unauthenticated_queue = await client.get(
            "/internal/wiki/moderation/reports"
        )
        assert unauthenticated_queue.status_code == 403
        moderator = {"X-Wiki-Moderator-Key": "operator-secret"}
        queue = await client.get(
            "/internal/wiki/moderation/reports", headers=moderator
        )
        assert queue.status_code == 200
        assert queue.headers["cache-control"] == "no-store"
        assert len(queue.json()["items"]) == 1
        report = queue.json()["items"][0]
        assert report["payload"]["reason"] == "operator should inspect"
        assert report["payload"]["content_hash"] == page["content_hash"]
        openapi = (await client.get("/openapi.json")).json()
        assert not any(
            path.startswith("/internal/wiki/moderation")
            for path in openapi["paths"]
        )

        quarantined = await client.post(
            "/internal/wiki/moderation/pages/reported/quarantine",
            headers={**moderator, "Idempotency-Key": "quarantine-1"},
            json={
                "expected_head_revision_id": page["head_revision_id"],
                "content_hash": page["content_hash"],
                "reason": "confirmed abuse",
            },
        )
        assert quarantined.status_code == 200, quarantined.text

        assert (await client.get("/api/wiki/v1/pages/reported")).status_code == 404
        listing = (await client.get("/api/wiki/v1/pages")).json()["items"]
        search = (await client.get("/api/wiki/v1/pages", params={"q": "reported"})).json()["items"]
        recent = (await client.get("/api/wiki/v1/recent-changes")).json()["items"]
        assert listing == []
        assert search == []
        assert recent == []
        assert (
            await client.get("/api/wiki/v1/pages/reported/history")
        ).status_code == 404
        assert (
            await client.get(
                "/api/wiki/v1/pages/reported/diff",
                params={
                    "from_revision_id": page["head_revision_id"],
                    "to_revision_id": page["head_revision_id"],
                },
            )
        ).status_code == 404
        blocked_edit = await client.post(
            "/api/wiki/v1/pages/reported/revisions",
            headers=bearer,
            json={
                "expected_head_revision_id": page["head_revision_id"],
                "content": "new content",
            },
        )
        assert blocked_edit.status_code == 404

        operator_page = await client.get(
            "/internal/wiki/moderation/pages/reported", headers=moderator
        )
        assert operator_page.status_code == 200
        assert operator_page.json()["moderation_status"] == "quarantined"
        assert operator_page.json()["content"] == "secret"

        released = await client.post(
            "/internal/wiki/moderation/pages/reported/release",
            headers={**moderator, "Idempotency-Key": "release-1"},
            json={
                "expected_head_revision_id": page["head_revision_id"],
                "content_hash": page["content_hash"],
                "note": "false positive",
            },
        )
        assert released.status_code == 200, released.text
        assert (await client.get("/api/wiki/v1/pages/reported")).status_code == 200

        resolved = await client.post(
            f"/internal/wiki/moderation/reports/{report['effect_id']}/resolve",
            headers=moderator,
        )
        replayed_resolve = await client.post(
            f"/internal/wiki/moderation/reports/{report['effect_id']}/resolve",
            headers=moderator,
        )
        assert resolved.status_code == replayed_resolve.status_code == 200
        assert (
            await client.get("/internal/wiki/moderation/reports", headers=moderator)
        ).json()["items"] == []


@pytest.mark.asyncio
async def test_moderation_rejects_bad_key_and_stale_exact_head(wiki_app):
    transport = httpx.ASGITransport(app=wiki_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://test"
    ) as client:
        auth = await session(client)
        bearer = {"Authorization": f"Bearer {auth['bearer_token']}"}
        await client.post(
            "/api/wiki/v1/pages",
            headers=bearer,
            json={"slug": "guarded", "title": "Guarded", "content": "content"},
        )
        page = (await client.get("/api/wiki/v1/pages/guarded")).json()
        body = {
            "expected_head_revision_id": page["head_revision_id"],
            "content_hash": page["content_hash"],
            "reason": "abuse",
        }
        bad_key = await client.post(
            "/internal/wiki/moderation/pages/guarded/quarantine",
            headers={"X-Wiki-Moderator-Key": "wrong"},
            json=body,
        )
        assert bad_key.status_code == 403
        stale = await client.post(
            "/internal/wiki/moderation/pages/guarded/quarantine",
            headers={"X-Wiki-Moderator-Key": "operator-secret"},
            json={**body, "content_hash": "0" * 64},
        )
        assert stale.status_code == 409
        assert stale.json()["detail"]["code"] == "content_hash_conflict"


@pytest.mark.asyncio
async def test_internal_moderation_plane_is_absent_from_public_openapi(wiki_app):
    transport = httpx.ASGITransport(app=wiki_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://test"
    ) as client:
        paths = (await client.get("/openapi.json")).json()["paths"]

    assert any(path.startswith("/api/wiki/v1") for path in paths)
    assert not any(path.startswith("/internal/wiki/moderation") for path in paths)
