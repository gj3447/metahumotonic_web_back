from __future__ import annotations

from html.parser import HTMLParser

import pytest

from app.wiki.render import SAFE_LINK_REL, render_markdown


class _AnchorCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.anchors: list[dict[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self.anchors.append(dict(attrs))


class _MarkupCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[str] = []
        self.attributes: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        self.attributes.extend(name for name, _value in attrs)


def _anchors(html: str) -> list[dict[str, str | None]]:
    parser = _AnchorCollector()
    parser.feed(html)
    return parser.anchors


def test_renders_normal_commonmark() -> None:
    html = render_markdown(
        "# 제목\n\n**굵게**와 *강조*\n\n- 하나\n- 둘\n\n```python\nprint('ok')\n```\n"
    )

    assert "<h1>제목</h1>" in html
    assert "<strong>굵게</strong>" in html
    assert "<em>강조</em>" in html
    assert "<ul>" in html and html.count("<li>") == 2
    assert "<pre><code>print('ok')\n</code></pre>" in html
    assert "language-python" not in html


@pytest.mark.parametrize(
    "target",
    [
        "https://example.com/wiki?q=1",
        "http://example.com/page",
        "mailto:editor@example.com",
        "/wiki/community/?page=hello",
        "#history",
    ],
)
def test_safe_links_keep_destination_and_receive_fixed_rel(target: str) -> None:
    html = render_markdown(f"[문서]({target})")
    anchors = _anchors(html)

    assert anchors == [{"href": target, "rel": SAFE_LINK_REL}]
    assert "target=" not in html


@pytest.mark.parametrize(
    "payload",
    [
        "<script>alert(1)</script>",
        '<img src=x onerror="alert(1)">',
        "<svg><script>alert(1)</script></svg>",
        "<math><mtext><img src=x onerror=alert(1)></mtext></math>",
        '<a href="javascript:alert(1)" onclick="alert(2)">raw</a>',
        '<iframe srcdoc="<script>alert(1)</script>"></iframe>',
    ],
)
def test_raw_html_is_never_activated(payload: str) -> None:
    html = render_markdown(payload)

    # Raw HTML is author text, so Markdown escapes it before the sanitizer.
    assert payload not in html
    parser = _MarkupCollector()
    parser.feed(html)
    assert set(parser.tags) <= {"p"}
    assert parser.attributes == []


@pytest.mark.parametrize(
    "target",
    [
        "javascript:alert(1)",
        "JaVaScRiPt:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "vbscript:msgbox(1)",
        "file:///etc/passwd",
    ],
)
def test_unsafe_markdown_link_protocol_never_becomes_href(target: str) -> None:
    html = render_markdown(f"[위험]({target})")

    assert _anchors(html) == []


def test_line_endings_are_canonical_and_rendering_is_repeatable() -> None:
    unix = "## 제목\n\n본문\n"
    windows = unix.replace("\n", "\r\n")

    expected = render_markdown(unix)
    assert render_markdown(windows) == expected
    assert render_markdown(unix) == expected


def test_non_string_input_is_rejected() -> None:
    with pytest.raises(TypeError, match="markdown must be a string"):
        render_markdown(None)  # type: ignore[arg-type]
