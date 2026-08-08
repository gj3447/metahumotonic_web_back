"""HTTP boundary guards for the public community wiki."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any


class RequestBodyTooLarge(Exception):
    pass


class WikiBodyLimitMiddleware:
    """Reject oversized wiki mutation bodies, including chunked requests.

    FastAPI validates decoded strings, but that happens after the ASGI server
    has read JSON.  This wrapper bounds bytes at the receive boundary so an
    anonymous client cannot force an unbounded allocation first.
    """

    def __init__(self, app: Any, *, max_bytes: int) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if (
            scope.get("type") != "http"
            or not (
                str(scope.get("path", "")).startswith("/api/wiki/v1/")
                or str(scope.get("path", "")).startswith(
                    "/internal/wiki/moderation/"
                )
            )
            or str(scope.get("method", "GET")).upper() in {"GET", "HEAD", "OPTIONS"}
        ):
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        raw_length = headers.get(b"content-length")
        if raw_length:
            try:
                if int(raw_length) > self.max_bytes:
                    await self._reject(send)
                    return
            except ValueError:
                await self._reject(send, status=400, code="invalid_content_length")
                return

        consumed = 0
        buffered_response: list[dict[str, Any]] = []

        async def limited_receive() -> dict[str, Any]:
            nonlocal consumed
            message = await receive()
            if message.get("type") == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > self.max_bytes:
                    raise RequestBodyTooLarge
            return message

        async def buffered_send(message: dict[str, Any]) -> None:
            buffered_response.append(message)

        try:
            await self.app(scope, limited_receive, buffered_send)
        except RequestBodyTooLarge:
            await self._reject(send)
            return
        for message in buffered_response:
            await send(message)

    @staticmethod
    async def _reject(
        send: Callable[[dict[str, Any]], Awaitable[None]],
        *,
        status: int = 413,
        code: str = "request_body_too_large",
    ) -> None:
        body = (
            '{"detail":{"code":"'
            + code
            + '","message":"wiki request body exceeds the configured limit"}}'
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


class WikiRobotsMiddleware:
    """Keep public-beta UGC and the operator surface out of crawler indexes."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        path = str(scope.get("path", ""))
        protected_root = path in {"/api/wiki", "/internal/wiki/moderation"}
        protected_child = path.startswith(
            ("/api/wiki/", "/internal/wiki/moderation/")
        )
        if scope.get("type") != "http" or not (
            protected_root or protected_child
        ):
            await self.app(scope, receive, send)
            return

        async def noindex_send(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers", []))
                headers = [
                    (key, value)
                    for key, value in headers
                    if key.lower() not in {b"x-robots-tag", b"cache-control"}
                ]
                headers.append((b"x-robots-tag", b"noindex, nofollow, noarchive"))
                # A quarantined revision must disappear immediately instead of
                # surviving in a CDN or shared intermediary cache.
                headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, noindex_send)
