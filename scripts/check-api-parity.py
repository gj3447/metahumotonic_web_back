#!/usr/bin/env python3
"""Read-only API parity gate for a Python origin and a TS/Effect candidate.

The two origins are required command-line inputs.  This script never discovers
an origin, follows a link from a response, sends a mutation, or writes secrets
or responses into its receipt.  It is meant for a private, staged origin before
any ingress switch.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


MAX_BODY_BYTES = 1_048_576
HEADER_NAMES = (
    "content-type",
    "cache-control",
    "etag",
    "x-data-source",
    "x-data-quality",
    "x-records-omitted",
)
# Only response fields that are expected to vary per process or evaluation are
# removed.  Do not add domain fields here merely to make a comparison pass.
CANONICAL_NOISE_FIELDS = frozenset(
    {
        "request_id",
        "instance",
        "deployedAt",
        "generated_at",
        "evaluatedAt",
        "updated",
        "last_verify_at",
    }
)


@dataclass(frozen=True)
class Check:
    identifier: str
    path: str
    expected_status: int
    json_body: bool
    require_live_research: bool = False


# This is deliberately an allowlist.  Add a route only after defining the
# compatible contract; it must stay a GET route, including negative checks.
CHECKS = (
    Check("stats", "/api/stats", 200, True),
    Check("domains", "/api/domains", 200, True),
    Check("skills", "/api/skills", 200, True),
    Check("research-summary", "/api/research/summary", 200, True, True),
    Check("research-findings", "/api/research/findings?limit=1&offset=0", 200, True),
    Check("research-lessons", "/api/research/lessons?limit=1&offset=0", 200, True),
    Check("research-papers", "/api/research/papers?limit=1&offset=0", 200, True),
    Check("research-consensus", "/api/research/consensus?limit=1", 200, True),
    Check("research-recent", "/api/research/recent?limit=1", 200, True),
    Check("research-agent", "/api/research/agent", 200, True),
    Check("mcp-discovery", "/api/mcp", 200, True),
    Check("mcp-servers", "/api/mcp/servers", 200, True),
    Check("mcp-manifest", "/api/mcp/manifest", 200, True),
    Check("mcp-health", "/api/mcp/health", 200, True),
    Check("mcp-status", "/api/mcp/status", 200, True),
    Check("well-known-mcp", "/.well-known/mcp-servers.json", 302, False),
    Check("missing-route", "/__mh_api_parity_missing__", 404, False),
)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def parse_origin(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise argparse.ArgumentTypeError("origin must be an explicit HTTP(S) origin")
    if parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise argparse.ArgumentTypeError("origin must not contain credentials, path, query, or fragment")
    return f"{parsed.scheme}://{parsed.netloc}"


def media_type(value: str | None) -> str | None:
    return value.split(";", 1)[0].strip().lower() if value else None


def normalized_headers(headers: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    for name in HEADER_NAMES:
        value = headers.get(name)
        if value is not None:
            result[name] = media_type(value) if name == "content-type" else " ".join(value.split())
    return result


def canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: canonical(item)
            for key, item in sorted(value.items())
            if key not in CANONICAL_NOISE_FIELDS
        }
    if isinstance(value, list):
        return [canonical(item) for item in value]
    return value


def fingerprint(value: Any) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    body: bytes


def fetch(origin: str, path: str, timeout: float) -> Response:
    request = Request(origin + path, method="GET", headers={"Accept": "application/json"})
    opener = build_opener(NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as raw:
            body = raw.read(MAX_BODY_BYTES + 1)
            response = Response(raw.status, normalized_headers(raw.headers), body)
    except HTTPError as error:
        body = error.read(MAX_BODY_BYTES + 1)
        response = Response(error.code, normalized_headers(error.headers), body)
    except URLError as error:
        raise RuntimeError("origin was unreachable") from error
    if len(response.body) > MAX_BODY_BYTES:
        raise RuntimeError("response exceeded read-only comparison budget")
    return response


def decode_json(response: Response, identifier: str) -> Any:
    if response.headers.get("content-type") != "application/json":
        raise RuntimeError(f"{identifier}: expected application/json")
    try:
        return json.loads(response.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"{identifier}: invalid JSON response") from error


def compare_check(check: Check, python_origin: str, ts_origin: str, timeout: float) -> dict[str, Any]:
    left = fetch(python_origin, check.path, timeout)
    right = fetch(ts_origin, check.path, timeout)
    if left.status != check.expected_status or right.status != check.expected_status:
        raise RuntimeError(f"{check.identifier}: unexpected status")
    if left.status != right.status or left.headers != right.headers:
        raise RuntimeError(f"{check.identifier}: status/header contract differs")
    result: dict[str, Any] = {"id": check.identifier, "status": left.status, "headers_match": True}
    if check.json_body:
        left_body, right_body = decode_json(left, check.identifier), decode_json(right, check.identifier)
        if check.require_live_research:
            if left_body.get("source") != "live" or right_body.get("source") != "live":
                raise RuntimeError(f"{check.identifier}: research is not live")
        left_canonical, right_canonical = canonical(left_body), canonical(right_body)
        if left_canonical != right_canonical:
            raise RuntimeError(f"{check.identifier}: canonical JSON differs")
        # A digest proves an exact canonical comparison ran without persisting a
        # response body.  It is emitted only after both sides are equal.
        result["canonical_json_digest"] = fingerprint(left_canonical)
    return result


def receipt(checks: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema": "metahumotonic/api-parity-receipt@1",
        "status": "PASS",
        "checkedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "checks": checks,
        "noiseFieldsExcluded": sorted(CANONICAL_NOISE_FIELDS),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-origin", required=True, type=parse_origin)
    parser.add_argument("--ts-origin", required=True, type=parse_origin)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=5.0)
    args = parser.parse_args(argv)
    if not 0 < args.timeout_seconds <= 30:
        parser.error("--timeout-seconds must be in (0, 30]")
    # Do not print origins, response content, tokens, or headers.  The caller
    # has the only copy of the private targets and receives a compact receipt.
    try:
        checks = [compare_check(check, args.python_origin, args.ts_origin, args.timeout_seconds) for check in CHECKS]
    except RuntimeError as error:
        print(f"FAIL {error}", file=sys.stderr)
        return 1
    payload = receipt(checks)
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print("PASS read-only API parity gate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
