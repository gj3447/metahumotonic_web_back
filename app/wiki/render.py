"""Deterministic, untrusted Markdown rendering for public wiki pages.

The renderer deliberately has two independent safety boundaries:

* markdown-it never interprets author supplied HTML; and
* Bleach applies a small output allowlist before HTML leaves the API.

Keeping this module free of request or database state makes the same revision
render identically from the web, CLI, and MCP entry points.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import bleach
from markdown_it import MarkdownIt

ALLOWED_TAGS: frozenset[str] = frozenset(
    {
        "a",
        "blockquote",
        "br",
        "code",
        "em",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "hr",
        "li",
        "ol",
        "p",
        "pre",
        "strong",
        "ul",
    }
)

ALLOWED_ATTRIBUTES: Mapping[str, Sequence[str]] = {
    "a": ("href", "rel", "title"),
}

ALLOWED_PROTOCOLS: frozenset[str] = frozenset({"http", "https", "mailto"})

# ``noopener`` protects future consumers that choose to open links in a new
# browsing context. ``noreferrer`` avoids leaking private page names, while
# ``nofollow`` and ``ugc`` mark links as untrusted community content.
SAFE_LINK_REL = "nofollow noopener noreferrer ugc"


_MARKDOWN = MarkdownIt(
    "commonmark",
    {
        "html": False,
        "linkify": False,
        "typographer": False,
    },
)


def _render_link_open(
    tokens: list[Any], index: int, options: Mapping[str, Any], env: Any
) -> str:
    """Stamp a non-author-controlled rel value on every Markdown link."""

    tokens[index].attrSet("rel", SAFE_LINK_REL)
    return _MARKDOWN.renderer.renderToken(tokens, index, options, env)


_MARKDOWN.renderer.rules["link_open"] = _render_link_open

_CLEANER = bleach.Cleaner(
    tags=ALLOWED_TAGS,
    attributes=ALLOWED_ATTRIBUTES,
    protocols=ALLOWED_PROTOCOLS,
    strip=True,
    strip_comments=True,
)


def render_markdown(markdown: str) -> str:
    """Render untrusted wiki Markdown to allowlisted HTML.

    Line endings are canonicalized so equivalent revisions uploaded from Unix,
    Windows, or classic-Mac clients yield the same HTML. Author supplied raw
    HTML remains visible as escaped text instead of becoming active markup.
    """

    if not isinstance(markdown, str):
        raise TypeError("markdown must be a string")

    canonical = markdown.replace("\r\n", "\n").replace("\r", "\n")
    return _CLEANER.clean(_MARKDOWN.render(canonical))


__all__ = [
    "ALLOWED_ATTRIBUTES",
    "ALLOWED_PROTOCOLS",
    "ALLOWED_TAGS",
    "SAFE_LINK_REL",
    "render_markdown",
]
