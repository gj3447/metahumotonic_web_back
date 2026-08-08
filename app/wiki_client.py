"""HTTP-only client for the public community wiki API.

This module is the single transport boundary shared by the ``mhb-wiki`` CLI
and the stdio MCP server.  It deliberately has no database or knowledge-graph
dependencies.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx

DEFAULT_BASE_URL = "https://metahumotonic.com"
API_PREFIX = "/api/wiki/v1"
DEFAULT_CONFIG_PATH = Path.home() / ".config" / "mhb" / "wiki.json"


class WikiClientError(RuntimeError):
    """A safe, user-facing configuration or API failure."""


class WikiAPIError(WikiClientError):
    """A non-success response from the wiki API."""

    def __init__(self, status_code: int, message: str, *, code: str | None = None):
        self.status_code = status_code
        self.code = code
        prefix = f"wiki API returned HTTP {status_code}"
        if code:
            prefix += f" ({code})"
        super().__init__(f"{prefix}: {message}")


@dataclass(frozen=True)
class WikiClientConfig:
    base_url: str = DEFAULT_BASE_URL
    token: str = ""
    timeout: float = 20.0

    @classmethod
    def from_sources(
        cls,
        *,
        base_url: str | None = None,
        token: str | None = None,
        timeout: float | None = None,
        config_path: str | Path | None = None,
    ) -> WikiClientConfig:
        """Resolve explicit values, environment, then a local JSON config.

        Recognized environment variables are ``MHB_WIKI_BASE_URL``,
        ``MHB_WIKI_TOKEN``, ``MHB_WIKI_TIMEOUT``, and ``MHB_WIKI_CONFIG``.
        The config file accepts ``base_url``, ``token``, and ``timeout``.
        """

        selected_path = Path(
            config_path
            or os.environ.get("MHB_WIKI_CONFIG", "").strip()
            or DEFAULT_CONFIG_PATH
        ).expanduser()
        stored: dict[str, Any] = {}
        if selected_path.is_file():
            try:
                loaded = json.loads(selected_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise WikiClientError(
                    f"cannot read wiki config {selected_path}: {exc}"
                ) from exc
            if not isinstance(loaded, dict):
                raise WikiClientError(
                    f"wiki config {selected_path} must contain a JSON object"
                )
            stored = loaded

        raw_timeout: Any = (
            timeout
            if timeout is not None
            else os.environ.get("MHB_WIKI_TIMEOUT")
            or stored.get("timeout")
            or cls.timeout
        )
        try:
            parsed_timeout = float(raw_timeout)
        except (TypeError, ValueError) as exc:
            raise WikiClientError("wiki timeout must be a positive number") from exc
        if parsed_timeout <= 0:
            raise WikiClientError("wiki timeout must be a positive number")

        resolved_url = str(
            base_url
            or os.environ.get("MHB_WIKI_BASE_URL", "").strip()
            or stored.get("base_url")
            or DEFAULT_BASE_URL
        ).rstrip("/")
        _validate_base_url(resolved_url)
        resolved_token = str(
            token
            if token is not None
            else os.environ.get("MHB_WIKI_TOKEN", "").strip()
            or stored.get("token")
            or ""
        ).strip()
        return cls(base_url=resolved_url, token=resolved_token, timeout=parsed_timeout)

    def save(self, path: str | Path | None = None) -> Path:
        """Atomically save URL/token with owner-only permissions.

        Callers must make the user opt in before invoking this method.  The
        method never logs or prints the token.
        """

        target = Path(
            path or os.environ.get("MHB_WIKI_CONFIG", "").strip() or DEFAULT_CONFIG_PATH
        ).expanduser()
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = (
            json.dumps(
                {"base_url": self.base_url, "token": self.token},
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
        temp_name = ""
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=target.parent,
                prefix=f".{target.name}.",
                delete=False,
            ) as handle:
                temp_name = handle.name
                os.chmod(temp_name, 0o600)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, target)
            os.chmod(target, 0o600)
        except OSError as exc:
            if temp_name:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass
            raise WikiClientError(f"cannot save wiki config {target}: {exc}") from exc
        return target


def _validate_base_url(value: str) -> None:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise WikiClientError("wiki base URL must be a credential-free HTTP(S) URL")


def _without_none(values: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


class WikiClient:
    """Typed convenience methods over ``/api/wiki/v1``."""

    def __init__(
        self,
        config: WikiClientConfig | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config or WikiClientConfig.from_sources()
        headers = {"Accept": "application/json", "User-Agent": "mhb-wiki/1"}
        if self.config.token:
            headers["Authorization"] = f"Bearer {self.config.token}"
        root = self.config.base_url
        if not root.endswith(API_PREFIX):
            root += API_PREFIX
        self._http = httpx.AsyncClient(
            base_url=root.rstrip("/") + "/",
            headers=headers,
            timeout=self.config.timeout,
            transport=transport,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._http.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        target = (
            str(self._http.base_url).rstrip("/") if path == "" else path.lstrip("/")
        )
        try:
            response = await self._http.request(
                method,
                target,
                params=_without_none(params or {}),
                json=_without_none(json_body or {}) if json_body is not None else None,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise WikiClientError(
                f"wiki API request failed: {exc.__class__.__name__}"
            ) from exc

        try:
            payload: Any = response.json() if response.content else {}
        except ValueError as exc:
            if response.is_success:
                raise WikiClientError(
                    "wiki API returned a non-JSON success response"
                ) from exc
            payload = {}
        if not response.is_success:
            body = payload if isinstance(payload, dict) else {}
            detail = body.get("detail")
            detail_body = detail if isinstance(detail, dict) else {}
            message = (
                body.get("message")
                or detail_body.get("message")
                or detail
                or response.reason_phrase
            )
            if isinstance(message, (dict, list)):
                message = json.dumps(message, ensure_ascii=False)
            raise WikiAPIError(
                response.status_code,
                str(message),
                code=(
                    str(body.get("code") or detail_body.get("code"))
                    if body.get("code") or detail_body.get("code")
                    else None
                ),
            )
        if not isinstance(payload, dict):
            raise WikiClientError("wiki API response must be a JSON object")
        return payload

    async def index(self) -> dict[str, Any]:
        return await self._request("GET", "")

    async def search(
        self, query: str, *, limit: int | None = None, offset: int | None = None
    ) -> dict[str, Any]:
        """Search pages, or list all pages when ``query`` is empty."""

        return await self._request(
            "GET", "pages", params={"q": query, "limit": limit, "offset": offset}
        )

    async def get_page(self, slug: str) -> dict[str, Any]:
        return await self._request("GET", f"pages/{_slug(slug)}")

    async def create_session(
        self,
        *,
        display_name: str,
        actor_kind: str,
        agent_url: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "sessions",
            json_body={
                "display_name": display_name,
                "actor_kind": actor_kind,
                "agent_url": agent_url,
            },
        )

    async def create_page(
        self,
        *,
        slug: str,
        title: str,
        content: str,
        edit_summary: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "pages",
            json_body={
                "slug": slug,
                "title": title,
                "content": content,
                "edit_summary": edit_summary,
            },
            headers={"Idempotency-Key": _idempotency_key(idempotency_key)},
        )

    async def create_revision(
        self,
        *,
        slug: str,
        title: str | None = None,
        content: str,
        edit_summary: str,
        expected_head_revision_id: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if not expected_head_revision_id.strip():
            raise WikiClientError(
                "expected_head_revision_id is required for a safe edit"
            )
        return await self._request(
            "POST",
            f"pages/{_slug(slug)}/revisions",
            json_body={
                "title": title,
                "content": content,
                "edit_summary": edit_summary,
                "expected_head_revision_id": expected_head_revision_id,
            },
            headers={"Idempotency-Key": _idempotency_key(idempotency_key)},
        )

    async def history(
        self, slug: str, *, limit: int | None = None, offset: int | None = None
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"pages/{_slug(slug)}/history",
            params={"limit": limit, "offset": offset},
        )

    async def diff(
        self,
        slug: str,
        *,
        from_revision_id: str,
        to_revision_id: str,
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"pages/{_slug(slug)}/diff",
            params={
                "from_revision_id": from_revision_id,
                "to_revision_id": to_revision_id,
            },
        )

    async def recent(
        self, *, limit: int | None = None, offset: int | None = None
    ) -> dict[str, Any]:
        return await self._request(
            "GET", "recent-changes", params={"limit": limit, "offset": offset}
        )

    async def submit_for_review(
        self,
        slug: str,
        *,
        revision_id: str | None = None,
        content_hash: str | None = None,
        note: str = "",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"pages/{_slug(slug)}/submit-review",
            json_body={
                "revision_id": revision_id,
                "content_hash": content_hash,
                "note": note,
            },
            headers={"Idempotency-Key": _idempotency_key(idempotency_key)},
        )

    async def report_page(
        self,
        slug: str,
        *,
        reason: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"pages/{_slug(slug)}/report",
            json_body={"reason": reason},
            headers={"Idempotency-Key": _idempotency_key(idempotency_key)},
        )


def _slug(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise WikiClientError("wiki slug must not be empty")
    return quote(stripped, safe="")


def _idempotency_key(value: str | None) -> str:
    key = value.strip() if value else str(uuid4())
    if not key or len(key) > 128 or any(char.isspace() for char in key):
        raise WikiClientError("idempotency key must be 1-128 non-whitespace characters")
    return key
