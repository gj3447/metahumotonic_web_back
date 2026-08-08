from __future__ import annotations

import json
from typing import ClassVar

import httpx
import pytest

from app import wiki_cli
from app.wiki_client import WikiAPIError, WikiClient, WikiClientConfig, WikiClientError


@pytest.fixture(autouse=True)
def clean_wiki_env(monkeypatch):
    for name in (
        "MHB_WIKI_BASE_URL",
        "MHB_WIKI_TOKEN",
        "MHB_WIKI_TIMEOUT",
        "MHB_WIKI_CONFIG",
    ):
        monkeypatch.delenv(name, raising=False)


async def test_http_client_uses_bearer_and_exact_contract_paths():
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    client = WikiClient(
        WikiClientConfig(base_url="https://wiki.example", token="secret", timeout=3),
        transport=httpx.MockTransport(handler),
    )
    async with client:
        await client.search("orca", limit=5)
        await client.create_revision(
            slug="hello world",
            title="Renamed page",
            content="body",
            edit_summary="edit",
            expected_head_revision_id="rev-1",
            idempotency_key="retry-edit-1",
        )
        await client.submit_for_review(
            "hello world",
            revision_id="rev-2",
            content_hash="a" * 64,
            note="ready",
        )
        await client.report_page(
            "hello world", reason="unsafe content", idempotency_key="report-1"
        )
        await client.recent(offset=25)

    assert [request.url.raw_path for request in seen] == [
        b"/api/wiki/v1/pages?q=orca&limit=5",
        b"/api/wiki/v1/pages/hello%20world/revisions",
        b"/api/wiki/v1/pages/hello%20world/submit-review",
        b"/api/wiki/v1/pages/hello%20world/report",
        b"/api/wiki/v1/recent-changes?offset=25",
    ]
    assert seen[0].url.params["q"] == "orca"
    assert seen[0].url.params["limit"] == "5"
    assert seen[0].headers["authorization"] == "Bearer secret"
    assert seen[1].headers["idempotency-key"] == "retry-edit-1"
    assert seen[2].headers["idempotency-key"]
    assert seen[3].headers["idempotency-key"] == "report-1"
    assert json.loads(seen[1].content) == {
        "title": "Renamed page",
        "content": "body",
        "edit_summary": "edit",
        "expected_head_revision_id": "rev-1",
    }
    assert json.loads(seen[2].content) == {
        "revision_id": "rev-2",
        "content_hash": "a" * 64,
        "note": "ready",
    }
    assert json.loads(seen[3].content) == {"reason": "unsafe content"}


async def test_index_uses_exact_no_trailing_slash_url():
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"service": "wiki"})

    async with WikiClient(
        WikiClientConfig(base_url="https://wiki.example"),
        transport=httpx.MockTransport(handler),
    ) as client:
        assert await client.index() == {"service": "wiki"}
    assert seen[0].url.path == "/api/wiki/v1"


async def test_session_and_diff_use_snake_case_contract():
    bodies: list[dict] = []
    queries: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.content:
            bodies.append(json.loads(request.content))
        queries.append(dict(request.url.params))
        return httpx.Response(200, json={"ok": True})

    async with WikiClient(
        WikiClientConfig(base_url="http://127.0.0.1:8000"),
        transport=httpx.MockTransport(handler),
    ) as client:
        await client.create_session(
            display_name="agent one",
            actor_kind="agent",
            agent_url="https://agent.example",
        )
        await client.diff("page", from_revision_id="r1", to_revision_id="r2")

    assert bodies == [
        {
            "display_name": "agent one",
            "actor_kind": "agent",
            "agent_url": "https://agent.example",
        }
    ]
    assert queries[-1] == {"from_revision_id": "r1", "to_revision_id": "r2"}


async def test_expected_head_is_required_before_network():
    called = False

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    async with WikiClient(
        WikiClientConfig(base_url="https://wiki.example"),
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(WikiClientError, match="expected_head_revision_id"):
            await client.create_revision(
                slug="page",
                content="body",
                edit_summary="edit",
                expected_head_revision_id=" ",
            )
    assert called is False


async def test_api_error_is_typed_and_does_not_echo_token():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={"detail": {"code": "REVISION_CONFLICT", "message": "stale head"}},
        )

    async with WikiClient(
        WikiClientConfig(base_url="https://wiki.example", token="do-not-echo"),
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(WikiAPIError) as raised:
            await client.get_page("page")
    assert raised.value.status_code == 409
    assert raised.value.code == "REVISION_CONFLICT"
    assert "do-not-echo" not in str(raised.value)


def test_config_precedence_explicit_env_file(tmp_path, monkeypatch):
    path = tmp_path / "wiki.json"
    path.write_text(
        json.dumps({"base_url": "https://file.example", "token": "file", "timeout": 9}),
        encoding="utf-8",
    )
    monkeypatch.setenv("MHB_WIKI_BASE_URL", "https://env.example")
    monkeypatch.setenv("MHB_WIKI_TOKEN", "env")
    config = WikiClientConfig.from_sources(
        base_url="https://flag.example", token="flag", config_path=path
    )
    assert config == WikiClientConfig(
        base_url="https://flag.example", token="flag", timeout=9
    )


def test_config_save_is_explicit_and_owner_only(tmp_path):
    target = tmp_path / "nested" / "wiki.json"
    saved = WikiClientConfig(base_url="https://wiki.example", token="secret").save(
        target
    )
    assert saved == target
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "base_url": "https://wiki.example",
        "token": "secret",
    }
    assert target.stat().st_mode & 0o777 == 0o600


class FakeCLIClient:
    calls: ClassVar[list[tuple[str, dict]]] = []

    def __init__(self, config: WikiClientConfig):
        self.config = config

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def create_revision(self, **kwargs):
        self.calls.append(("create_revision", kwargs))
        return {"accepted": True, "revision_id": "r2"}

    async def create_session(self, **kwargs):
        self.calls.append(("create_session", kwargs))
        return {
            "access_token": "issued-secret",
            "bearer_token": "issued-secret",
            "csrf_token": "csrf-secret",
            "actor_id": "agent:1",
        }

    async def report_page(self, slug, **kwargs):
        self.calls.append(("report_page", {"slug": slug, **kwargs}))
        return {"accepted": True, "reported": slug}

    async def submit_for_review(self, slug, **kwargs):
        self.calls.append(("submit_for_review", {"slug": slug, **kwargs}))
        return {"accepted": True, "submitted": slug}


async def test_cli_edit_calls_only_shared_http_client(tmp_path, capsys):
    content = tmp_path / "page.md"
    content.write_text("new body", encoding="utf-8")
    FakeCLIClient.calls.clear()
    rc = await wiki_cli.async_main(
        [
            "--base-url",
            "https://wiki.example",
            "edit",
            "page",
            "--content-file",
            str(content),
            "--summary",
            "fix",
            "--expected-head-revision-id",
            "r1",
        ],
        client_factory=FakeCLIClient,
    )
    assert rc == 0
    assert FakeCLIClient.calls == [
        (
            "create_revision",
            {
                "slug": "page",
                "title": None,
                "content": "new body",
                "edit_summary": "fix",
                "expected_head_revision_id": "r1",
                "idempotency_key": None,
            },
        )
    ]
    assert json.loads(capsys.readouterr().out)["revision_id"] == "r2"


async def test_cli_init_saves_only_with_explicit_flag(tmp_path, capsys):
    config = tmp_path / "wiki.json"
    FakeCLIClient.calls.clear()
    rc = await wiki_cli.async_main(
        [
            "--base-url",
            "https://wiki.example",
            "--config",
            str(config),
            "init",
            "build-agent",
            "--actor-kind",
            "agent",
            "--agent-url",
            "https://agent.example",
            "--save",
        ],
        client_factory=FakeCLIClient,
    )
    assert rc == 0
    assert json.loads(config.read_text(encoding="utf-8")) == {
        "base_url": "https://wiki.example",
        "token": "issued-secret",
    }
    assert config.stat().st_mode & 0o777 == 0o600
    output = capsys.readouterr().out
    assert "issued-secret" not in output
    assert "csrf-secret" not in output
    parsed = json.loads(output)
    assert parsed["access_token"] == "[REDACTED]"
    assert parsed["bearer_token"] == "[REDACTED]"
    assert parsed["csrf_token"] == "[REDACTED]"
    assert parsed["config_saved"] == str(config)


async def test_cli_init_does_not_save_or_show_token_by_default(tmp_path, capsys):
    config = tmp_path / "wiki.json"
    rc = await wiki_cli.async_main(
        ["--config", str(config), "init", "human", "--actor-kind", "human"],
        client_factory=FakeCLIClient,
    )
    assert rc == 0
    assert not config.exists()
    output = capsys.readouterr().out
    assert "issued-secret" not in output
    assert json.loads(output)["access_token"] == "[REDACTED]"


async def test_cli_init_show_token_requires_explicit_flag(tmp_path, capsys):
    config = tmp_path / "wiki.json"
    rc = await wiki_cli.async_main(
        ["--config", str(config), "init", "agent", "--show-token"],
        client_factory=FakeCLIClient,
    )
    assert rc == 0
    assert not config.exists()
    assert json.loads(capsys.readouterr().out)["access_token"] == "issued-secret"


async def test_cli_report_delegates_to_shared_http_client(capsys):
    FakeCLIClient.calls.clear()
    rc = await wiki_cli.async_main(
        ["report", "page", "--reason", "spam", "--idempotency-key", "report-2"],
        client_factory=FakeCLIClient,
    )
    assert rc == 0
    assert FakeCLIClient.calls == [
        (
            "report_page",
            {"slug": "page", "reason": "spam", "idempotency_key": "report-2"},
        )
    ]
    assert json.loads(capsys.readouterr().out)["reported"] == "page"


async def test_cli_submit_forwards_exact_hash_and_note(capsys):
    FakeCLIClient.calls.clear()
    digest = "b" * 64
    rc = await wiki_cli.async_main(
        [
            "submit",
            "page",
            "--revision-id",
            "r2",
            "--content-hash",
            digest,
            "--note",
            "review this",
            "--idempotency-key",
            "submit-2",
        ],
        client_factory=FakeCLIClient,
    )
    assert rc == 0
    assert FakeCLIClient.calls == [
        (
            "submit_for_review",
            {
                "slug": "page",
                "revision_id": "r2",
                "content_hash": digest,
                "note": "review this",
                "idempotency_key": "submit-2",
            },
        )
    ]
    assert json.loads(capsys.readouterr().out)["submitted"] == "page"


def test_cli_parser_has_exact_public_commands():
    parser = wiki_cli.build_parser()
    subparsers = next(
        action
        for action in parser._actions
        if isinstance(action, __import__("argparse")._SubParsersAction)
    )
    assert set(subparsers.choices) == {
        "init",
        "get",
        "search",
        "create",
        "edit",
        "history",
        "diff",
        "recent",
        "submit",
        "report",
    }
