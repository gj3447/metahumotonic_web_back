"""``mhb-wiki`` command-line adapter for the community wiki HTTP API."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from .wiki_client import WikiClient, WikiClientConfig, WikiClientError


def _content(value: str) -> str:
    if value == "-":
        return sys.stdin.read()
    try:
        return Path(value).read_text(encoding="utf-8")
    except OSError as exc:
        raise WikiClientError(f"cannot read content file {value}: {exc}") from exc


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _session_token(payload: dict[str, Any]) -> str:
    return str(
        payload.get("bearer_token")
        or payload.get("access_token")
        or payload.get("token")
        or ""
    ).strip()


def _redact_session_tokens(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: "[REDACTED]"
        if key in {"access_token", "bearer_token", "csrf_token", "token"}
        else value
        for key, value in payload.items()
    }


async def cmd_init(args: argparse.Namespace, client: WikiClient) -> int:
    payload = await client.create_session(
        display_name=args.display_name,
        actor_kind=args.actor_kind,
        agent_url=args.agent_url,
    )
    token = _session_token(payload)
    if args.save:
        if not token:
            raise WikiClientError("session response has no access_token/token to save")
        saved = WikiClientConfig(
            base_url=client.config.base_url,
            token=token,
            timeout=client.config.timeout,
        ).save(args.config)
        payload = {**payload, "config_saved": str(saved)}
    if not args.show_token:
        payload = _redact_session_tokens(payload)
    _emit(payload)
    return 0


async def cmd_get(args: argparse.Namespace, client: WikiClient) -> int:
    _emit(await client.get_page(args.slug))
    return 0


async def cmd_search(args: argparse.Namespace, client: WikiClient) -> int:
    _emit(await client.search(args.query, limit=args.limit, offset=args.offset))
    return 0


async def cmd_create(args: argparse.Namespace, client: WikiClient) -> int:
    _emit(
        await client.create_page(
            slug=args.slug,
            title=args.title,
            content=_content(args.content_file),
            edit_summary=args.summary,
            idempotency_key=args.idempotency_key,
        )
    )
    return 0


async def cmd_edit(args: argparse.Namespace, client: WikiClient) -> int:
    _emit(
        await client.create_revision(
            slug=args.slug,
            title=args.title,
            content=_content(args.content_file),
            edit_summary=args.summary,
            expected_head_revision_id=args.expected_head_revision_id,
            idempotency_key=args.idempotency_key,
        )
    )
    return 0


async def cmd_history(args: argparse.Namespace, client: WikiClient) -> int:
    _emit(await client.history(args.slug, limit=args.limit, offset=args.offset))
    return 0


async def cmd_diff(args: argparse.Namespace, client: WikiClient) -> int:
    _emit(
        await client.diff(
            args.slug,
            from_revision_id=args.from_revision_id,
            to_revision_id=args.to_revision_id,
        )
    )
    return 0


async def cmd_recent(args: argparse.Namespace, client: WikiClient) -> int:
    _emit(await client.recent(limit=args.limit, offset=args.offset))
    return 0


async def cmd_submit(args: argparse.Namespace, client: WikiClient) -> int:
    _emit(
        await client.submit_for_review(
            args.slug,
            revision_id=args.revision_id,
            content_hash=args.content_hash,
            note=args.note,
            idempotency_key=args.idempotency_key,
        )
    )
    return 0


async def cmd_report(args: argparse.Namespace, client: WikiClient) -> int:
    _emit(
        await client.report_page(
            args.slug,
            reason=args.reason,
            idempotency_key=args.idempotency_key,
        )
    )
    return 0


Handler = Callable[[argparse.Namespace, WikiClient], Awaitable[int]]
HANDLERS: dict[str, Handler] = {
    "init": cmd_init,
    "get": cmd_get,
    "search": cmd_search,
    "create": cmd_create,
    "edit": cmd_edit,
    "history": cmd_history,
    "diff": cmd_diff,
    "recent": cmd_recent,
    "submit": cmd_submit,
    "report": cmd_report,
}


def _paging(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--limit", type=int)
    parser.add_argument("--offset", type=int)


def _idempotency(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--idempotency-key",
        help="stable retry key; a UUID is generated when omitted",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mhb-wiki", description=__doc__)
    parser.add_argument("--base-url")
    parser.add_argument("--token", help="Bearer token; prefer MHB_WIKI_TOKEN")
    parser.add_argument("--config", help="JSON config path")
    parser.add_argument("--timeout", type=float)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="create an authenticated wiki session")
    init.add_argument("display_name")
    init.add_argument("--actor-kind", choices=("agent", "human"), default="agent")
    init.add_argument("--agent-url")
    init.add_argument(
        "--save",
        action="store_true",
        help="save returned token and base URL to an owner-only config file",
    )
    init.add_argument(
        "--show-token",
        action="store_true",
        help="show session tokens in stdout; hidden by default",
    )

    get = sub.add_parser("get", help="get one page")
    get.add_argument("slug")

    search = sub.add_parser(
        "search", help="search pages; pass an empty query to list all"
    )
    search.add_argument("query", help="search text, or '' to list all pages")
    _paging(search)

    create = sub.add_parser("create", help="create a page and first revision")
    create.add_argument("slug")
    create.add_argument("--title", required=True)
    create.add_argument(
        "--content-file", required=True, help="UTF-8 file, or - for stdin"
    )
    create.add_argument("--summary", required=True)
    _idempotency(create)

    edit = sub.add_parser("edit", help="append a revision")
    edit.add_argument("slug")
    edit.add_argument(
        "--content-file", required=True, help="UTF-8 file, or - for stdin"
    )
    edit.add_argument("--summary", required=True)
    edit.add_argument("--title", help="optionally change the page title")
    edit.add_argument("--expected-head-revision-id", required=True)
    _idempotency(edit)

    history = sub.add_parser("history", help="show immutable revision history")
    history.add_argument("slug")
    _paging(history)

    diff = sub.add_parser("diff", help="compare two exact revisions")
    diff.add_argument("slug")
    diff.add_argument("--from-revision-id", required=True)
    diff.add_argument("--to-revision-id", required=True)

    recent = sub.add_parser("recent", help="list recent changes")
    _paging(recent)

    submit = sub.add_parser("submit", help="submit an exact revision, or the head")
    submit.add_argument("slug")
    submit.add_argument("--revision-id")
    submit.add_argument("--content-hash", help="optional exact lowercase SHA-256")
    submit.add_argument("--note", default="", help="review note")
    _idempotency(submit)

    report = sub.add_parser("report", help="report a community page")
    report.add_argument("slug")
    report.add_argument("--reason", required=True)
    _idempotency(report)
    return parser


async def async_main(
    argv: list[str] | None = None,
    *,
    client_factory: Callable[[WikiClientConfig], WikiClient] = WikiClient,
) -> int:
    args = build_parser().parse_args(argv)
    config = WikiClientConfig.from_sources(
        base_url=args.base_url,
        token=args.token,
        timeout=args.timeout,
        config_path=args.config,
    )
    async with client_factory(config) as client:
        return await HANDLERS[args.command](args, client)


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(async_main(argv))
    except (WikiClientError, KeyboardInterrupt) as exc:
        if isinstance(exc, KeyboardInterrupt):
            print("interrupted", file=sys.stderr)
            return 130
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
