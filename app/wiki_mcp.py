"""Official MCP Python SDK stdio adapter for the community wiki API.

Every tool delegates to :mod:`app.wiki_client`; this process has no direct
database, filesystem-write, or knowledge-graph capability.  Stdio stdout is
reserved exclusively for the MCP protocol.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from .wiki_client import WikiClient, WikiClientConfig

READ_ONLY = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}
SAFE_WRITE = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": False,
}


def create_server(*, client: WikiClient | None = None):
    """Create the stdio server, optionally with an injected test client."""

    try:
        from mcp.server import MCPServer
        from mcp.types import ToolAnnotations
    except ImportError as exc:  # dependency is declared by the packaging owner
        raise RuntimeError("the official 'mcp' Python SDK is required") from exc

    owned_client = client is None
    wiki = client or WikiClient(WikiClientConfig.from_sources())

    @asynccontextmanager
    async def lifespan(_server):
        try:
            yield {"wiki_client": wiki}
        finally:
            if owned_client:
                await wiki.close()

    server = MCPServer(
        "Metahumotonic Community Wiki",
        version="1.0.0",
        instructions=(
            "Read and edit the community wiki only through its versioned HTTP API. "
            "This server does not issue sessions or return bearer tokens; provision "
            "MHB_WIKI_TOKEN or the local wiki config before starting stdio. "
            "Edits require the current expected_head_revision_id. Community content "
            "is not KG canon; submit_for_review only starts the separate review flow."
        ),
        lifespan=lifespan,
    )

    read_annotations = ToolAnnotations.model_validate(READ_ONLY)
    write_annotations = ToolAnnotations.model_validate(SAFE_WRITE)

    @server.tool(name="wiki_get", annotations=read_annotations, structured_output=True)
    async def wiki_get(slug: str) -> dict[str, Any]:
        """Get one community page by exact slug."""

        return await wiki.get_page(slug)

    @server.tool(
        name="wiki_search", annotations=read_annotations, structured_output=True
    )
    async def wiki_search(
        query: str, limit: int | None = None, offset: int | None = None
    ) -> dict[str, Any]:
        """Search community pages; use an empty query to list all pages."""

        return await wiki.search(query, limit=limit, offset=offset)

    @server.tool(
        name="wiki_create_page", annotations=write_annotations, structured_output=True
    )
    async def wiki_create_page(
        slug: str,
        title: str,
        content: str,
        edit_summary: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Create a community page and its first immutable revision."""

        return await wiki.create_page(
            slug=slug,
            title=title,
            content=content,
            edit_summary=edit_summary,
            idempotency_key=idempotency_key,
        )

    @server.tool(
        name="wiki_create_revision",
        annotations=write_annotations,
        structured_output=True,
    )
    async def wiki_create_revision(
        slug: str,
        content: str,
        edit_summary: str,
        expected_head_revision_id: str,
        title: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Append a revision if the exact expected head still matches."""

        return await wiki.create_revision(
            slug=slug,
            title=title,
            content=content,
            edit_summary=edit_summary,
            expected_head_revision_id=expected_head_revision_id,
            idempotency_key=idempotency_key,
        )

    @server.tool(
        name="wiki_history", annotations=read_annotations, structured_output=True
    )
    async def wiki_history(
        slug: str, limit: int | None = None, offset: int | None = None
    ) -> dict[str, Any]:
        """List immutable revision history for one page."""

        return await wiki.history(slug, limit=limit, offset=offset)

    @server.tool(name="wiki_diff", annotations=read_annotations, structured_output=True)
    async def wiki_diff(
        slug: str, from_revision_id: str, to_revision_id: str
    ) -> dict[str, Any]:
        """Diff two exact revisions of one page."""

        return await wiki.diff(
            slug,
            from_revision_id=from_revision_id,
            to_revision_id=to_revision_id,
        )

    @server.tool(
        name="wiki_recent", annotations=read_annotations, structured_output=True
    )
    async def wiki_recent(
        limit: int | None = None, offset: int | None = None
    ) -> dict[str, Any]:
        """List recent community-wiki changes."""

        return await wiki.recent(limit=limit, offset=offset)

    @server.tool(
        name="wiki_submit_for_review",
        annotations=write_annotations,
        structured_output=True,
    )
    async def wiki_submit_for_review(
        slug: str,
        revision_id: str | None = None,
        content_hash: str | None = None,
        note: str = "",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Submit an exact revision, or the current head, for separate KG review."""

        return await wiki.submit_for_review(
            slug,
            revision_id=revision_id,
            content_hash=content_hash,
            note=note,
            idempotency_key=idempotency_key,
        )

    @server.tool(
        name="wiki_report", annotations=write_annotations, structured_output=True
    )
    async def wiki_report(
        slug: str,
        reason: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Report a community page for moderation without changing KG canon."""

        return await wiki.report_page(
            slug,
            reason=reason,
            idempotency_key=idempotency_key,
        )

    return server


def main() -> None:
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
