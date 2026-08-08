from __future__ import annotations

from typing import Any

import pytest

mcp_sdk = pytest.importorskip(
    "mcp", reason="official MCP SDK is an application dependency"
)

from app.wiki_mcp import create_server


class FakeWikiClient:
    def __init__(self):
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def get_page(self, slug):
        self.calls.append(("get_page", {"slug": slug}))
        return {"slug": slug}

    async def search(self, query, *, limit=None, offset=None):
        self.calls.append(
            ("search", {"query": query, "limit": limit, "offset": offset})
        )
        return {"pages": []}

    async def create_revision(self, **kwargs):
        self.calls.append(("create_revision", kwargs))
        return {"accepted": True, "revision_id": "r2"}

    async def create_page(self, **kwargs):
        self.calls.append(("create_page", kwargs))
        return {"accepted": True, "slug": kwargs["slug"]}

    async def history(self, slug, *, limit=None, offset=None):
        self.calls.append(("history", {"slug": slug, "limit": limit, "offset": offset}))
        return {"revisions": []}

    async def diff(self, slug, *, from_revision_id, to_revision_id):
        self.calls.append(
            (
                "diff",
                {
                    "slug": slug,
                    "from_revision_id": from_revision_id,
                    "to_revision_id": to_revision_id,
                },
            )
        )
        return {"diff": ""}

    async def recent(self, *, limit=None, offset=None):
        self.calls.append(("recent", {"limit": limit, "offset": offset}))
        return {"changes": []}

    async def submit_for_review(
        self,
        slug,
        *,
        revision_id=None,
        content_hash=None,
        note="",
        idempotency_key=None,
    ):
        self.calls.append(
            (
                "submit_for_review",
                {
                    "slug": slug,
                    "revision_id": revision_id,
                    "content_hash": content_hash,
                    "note": note,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        return {"accepted": True}

    async def report_page(self, slug, *, reason, idempotency_key=None):
        self.calls.append(
            (
                "report_page",
                {
                    "slug": slug,
                    "reason": reason,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        return {"accepted": True, "reported": slug}


async def test_mcp_exposes_exact_tool_surface_and_safety_annotations():
    server = create_server(client=FakeWikiClient())
    tools = {tool.name: tool for tool in await server.list_tools()}
    assert set(tools) == {
        "wiki_get",
        "wiki_search",
        "wiki_create_page",
        "wiki_create_revision",
        "wiki_history",
        "wiki_diff",
        "wiki_recent",
        "wiki_submit_for_review",
        "wiki_report",
    }
    assert tools["wiki_get"].annotations.read_only_hint is True
    assert tools["wiki_history"].annotations.read_only_hint is True
    assert tools["wiki_create_revision"].annotations.read_only_hint is False
    schema = tools["wiki_create_revision"].input_schema
    assert "expected_head_revision_id" in schema["required"]


async def test_mcp_revision_tool_delegates_to_http_client_only():
    client = FakeWikiClient()
    server = create_server(client=client)
    result = await server.call_tool(
        "wiki_create_revision",
        {
            "slug": "page",
            "content": "new body",
            "edit_summary": "fix",
            "expected_head_revision_id": "r1",
            "title": "New title",
        },
    )
    assert client.calls == [
        (
            "create_revision",
            {
                "slug": "page",
                "title": "New title",
                "content": "new body",
                "edit_summary": "fix",
                "expected_head_revision_id": "r1",
                "idempotency_key": None,
            },
        )
    ]
    assert result.is_error is False
    assert result.structured_content == {"accepted": True, "revision_id": "r2"}


async def test_mcp_report_delegates_to_http_client_only():
    client = FakeWikiClient()
    server = create_server(client=client)
    result = await server.call_tool(
        "wiki_report",
        {"slug": "page", "reason": "spam", "idempotency_key": "report-1"},
    )
    assert client.calls == [
        (
            "report_page",
            {
                "slug": "page",
                "reason": "spam",
                "idempotency_key": "report-1",
            },
        )
    ]
    assert result.structured_content == {"accepted": True, "reported": "page"}


async def test_mcp_submit_allows_head_or_exact_revision():
    client = FakeWikiClient()
    server = create_server(client=client)
    await server.call_tool("wiki_submit_for_review", {"slug": "page"})
    await server.call_tool(
        "wiki_submit_for_review",
        {
            "slug": "page",
            "revision_id": "r2",
            "content_hash": "c" * 64,
            "note": "ready",
        },
    )
    assert client.calls == [
        (
            "submit_for_review",
            {
                "slug": "page",
                "revision_id": None,
                "content_hash": None,
                "note": "",
                "idempotency_key": None,
            },
        ),
        (
            "submit_for_review",
            {
                "slug": "page",
                "revision_id": "r2",
                "content_hash": "c" * 64,
                "note": "ready",
                "idempotency_key": None,
            },
        ),
    ]
