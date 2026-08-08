"""Typed public community wiki HTTP API.

All mutations obtain actor, scopes, timestamp, command id, and event ids from
the server boundary. Request bodies intentionally reject actor/capability data.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import secrets
import unicodedata
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator

from app.netutil import client_key
from app.ratelimit import RateLimiter, RateLimitUnavailable
from app.wiki.domain import (
    CreatePage,
    EditPage,
    QuarantinePage,
    ReleasePage,
    ReportPage,
    SubmitReview,
    WikiError,
)
from app.wiki.memory import InMemoryWikiStore
from app.wiki.render import render_markdown
from app.wiki.security import SessionIdentity, SessionSigner
from app.wiki.store import CommandReceipt, StoredEvent, WikiStore

COOKIE_NAME = "mhb_wiki_session"
MAX_TITLE = 200
MAX_CONTENT = 100_000
MAX_SLUG = 80
MAX_OFFSET = 10_000
MAX_CONTENT_BYTES = 400_000
RESERVED_SLUGS = frozenset(
    {"api", "authority", "canonical", "history", "recent-changes", "session"}
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionRequest(StrictModel):
    display_name: str = Field(default="Anonymous", min_length=1, max_length=80)
    actor_kind: Literal["human", "agent"] = "human"
    agent_url: AnyHttpUrl | None = None

    @field_validator("display_name")
    @classmethod
    def non_blank_display_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("display_name must not be blank")
        return value


class SessionResponse(StrictModel):
    actor_id: str
    display_name: str
    actor_kind: Literal["human", "agent"]
    agent_url: str | None
    access_token: str | None
    bearer_token: str | None
    token_type: Literal["bearer"] = "bearer"
    csrf_token: str
    scopes: list[str]
    expires_at: datetime


class CreatePageRequest(StrictModel):
    slug: str = Field(min_length=1, max_length=MAX_SLUG * 3)
    title: str = Field(min_length=1, max_length=MAX_TITLE)
    content: str = Field(min_length=1, max_length=MAX_CONTENT)
    edit_summary: str = Field(default="", max_length=500)

    @field_validator("title")
    @classmethod
    def non_blank_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("title must not be blank")
        return value

    @field_validator("content")
    @classmethod
    def bounded_content_bytes(cls, value: str) -> str:
        if len(value.encode("utf-8")) > MAX_CONTENT_BYTES:
            raise ValueError("content exceeds the UTF-8 byte limit")
        return value


class EditPageRequest(StrictModel):
    expected_head_revision_id: str = Field(min_length=1, max_length=128)
    title: str | None = Field(default=None, min_length=1, max_length=MAX_TITLE)
    content: str = Field(min_length=1, max_length=MAX_CONTENT)
    edit_summary: str = Field(default="", max_length=500)

    @field_validator("title")
    @classmethod
    def non_blank_title(cls, value: str) -> str:
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("title must not be blank")
        return value

    @field_validator("content")
    @classmethod
    def bounded_content_bytes(cls, value: str) -> str:
        if len(value.encode("utf-8")) > MAX_CONTENT_BYTES:
            raise ValueError("content exceeds the UTF-8 byte limit")
        return value


class SubmitReviewRequest(StrictModel):
    revision_id: str | None = Field(default=None, min_length=1, max_length=128)
    content_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    note: str = Field(default="", max_length=2000)


class ReportRequest(StrictModel):
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("reason")
    @classmethod
    def non_blank_reason(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reason must not be blank")
        return value


class QuarantineRequest(StrictModel):
    expected_head_revision_id: str = Field(min_length=1, max_length=128)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("reason")
    @classmethod
    def non_blank_reason(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reason must not be blank")
        return value


class ReleaseRequest(StrictModel):
    expected_head_revision_id: str = Field(min_length=1, max_length=128)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    note: str = Field(default="", max_length=2000)


class ResolveReportResponse(StrictModel):
    effect_id: str
    resolved: Literal[True] = True


class ModerationPageResponse(StrictModel):
    page_id: str
    slug: str
    title: str
    content: str
    head_revision_id: str
    content_hash: str
    head_actor_id: str
    stream_version: int
    moderation_status: Literal["visible", "quarantined"]
    updated_at: datetime


class ModerationReportResponse(StrictModel):
    effect_id: str
    event_id: str
    status: str
    attempts: int
    created_at: datetime
    payload: dict[str, Any]


class ModerationReportListResponse(StrictModel):
    items: list[ModerationReportResponse]
    limit: int


class PageResponse(StrictModel):
    page_id: str
    slug: str
    title: str
    content: str
    sanitized_html: str
    head_revision_id: str
    content_hash: str
    stream_version: int
    authority: Literal["community"] = "community"
    review_status: Literal["unreviewed", "submitted"]
    canonical: Literal[False] = False
    created_at: datetime
    updated_at: datetime


class PageSummaryResponse(StrictModel):
    page_id: str
    slug: str
    title: str
    head_revision_id: str
    content_hash: str
    stream_version: int
    authority: Literal["community"] = "community"
    review_status: Literal["unreviewed", "submitted"]
    canonical: Literal[False] = False
    created_at: datetime
    updated_at: datetime


class PageListResponse(StrictModel):
    items: list[PageSummaryResponse]
    limit: int
    offset: int
    q: str | None


class CommandResponse(StrictModel):
    command_id: str
    page_id: str
    stream_version: int
    event_ids: list[str]
    replayed: bool
    authority: Literal["community"] = "community"


class EventResponse(StrictModel):
    event_index: int
    event_id: str
    event_type: str
    actor_id: str
    occurred_at: datetime
    schema_version: int
    data: dict[str, Any]
    slug: str | None = None
    title: str | None = None
    revision_id: str | None = None
    parent_revision_id: str | None = None
    content_hash: str | None = None
    edit_summary: str | None = None
    committed_at: datetime | None = None


class EventListResponse(StrictModel):
    items: list[EventResponse]
    limit: int
    offset: int


class DiffResponse(StrictModel):
    page_id: str
    slug: str
    from_revision_id: str
    to_revision_id: str
    unified_diff: str


class WikiRuntime:
    def __init__(
        self,
        store: WikiStore,
        signer: SessionSigner,
        *,
        now: Callable[[], datetime] | None = None,
        writes_enabled: bool = False,
        secure_cookie: bool = True,
        allowed_origins: frozenset[str] | None = None,
        session_limiter: RateLimiter | None = None,
        mutation_limiter: RateLimiter | None = None,
        read_limiter: RateLimiter | None = None,
        moderation_key: str = "",
        moderator_actor: Callable[[Request], str] | None = None,
    ) -> None:
        self.store = store
        self.signer = signer
        self.now = now or (lambda: datetime.now(UTC))
        self.writes_enabled = writes_enabled
        self.secure_cookie = secure_cookie
        self.allowed_origins = allowed_origins or frozenset()
        self.session_limiter = session_limiter
        self.mutation_limiter = mutation_limiter
        self.read_limiter = read_limiter
        self.moderation_key = moderation_key
        self.moderator_actor = moderator_actor
        self.ready = False


def normalize_slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    normalized = re.sub(r"[\s_]+", "-", normalized)
    normalized = "".join(char for char in normalized if char.isalnum() or char == "-")
    normalized = re.sub(r"-+", "-", normalized).strip("-")
    if not normalized or len(normalized) > MAX_SLUG:
        raise WikiError(
            "invalid_slug",
            f"slug must normalize to 1..{MAX_SLUG} characters",
            status_code=422,
        )
    if normalized in RESERVED_SLUGS:
        raise WikiError("reserved_slug", "slug is reserved", status_code=422)
    return normalized


def _default_signer() -> SessionSigner:
    configured = os.getenv("MHB_WIKI_SESSION_SECRET")
    secret = configured.encode("utf-8") if configured else secrets.token_bytes(32)
    return SessionSigner(secret)


def _command_id(actor_id: str, idempotency_key: str | None) -> str:
    if not idempotency_key:
        return f"cmd:{uuid.uuid4()}"
    if len(idempotency_key) > 200:
        raise WikiError(
            "invalid_idempotency_key", "Idempotency-Key is too long", status_code=422
        )
    digest = hashlib.sha256(f"{actor_id}\0{idempotency_key}".encode()).hexdigest()
    return f"idem:{digest}"


def _stable_id(command_id: str, purpose: str) -> str:
    return str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"metahumotonic-wiki:{command_id}:{purpose}")
    )


def _http_error(error: WikiError) -> HTTPException:
    return HTTPException(
        status_code=error.status_code,
        detail={"code": error.code, "message": error.message},
    )


def _page_response(page: Any) -> PageResponse:
    return PageResponse(
        page_id=page.page_id,
        slug=page.slug,
        title=page.title,
        content=page.content,
        sanitized_html=render_markdown(page.content),
        head_revision_id=page.head_revision_id,
        content_hash=page.content_hash,
        stream_version=page.stream_version,
        review_status=page.review_status,
        created_at=page.created_at,
        updated_at=page.updated_at,
    )


def _page_summary_response(page: Any) -> PageSummaryResponse:
    return PageSummaryResponse(
        page_id=page.page_id,
        slug=page.slug,
        title=page.title,
        head_revision_id=page.head_revision_id,
        content_hash=page.content_hash,
        stream_version=page.stream_version,
        review_status=page.review_status,
        created_at=page.created_at,
        updated_at=page.updated_at,
    )


def _command_response(receipt: CommandReceipt) -> CommandResponse:
    return CommandResponse(
        command_id=receipt.command_id,
        page_id=receipt.page_id,
        stream_version=receipt.stream_version,
        event_ids=list(receipt.event_ids),
        replayed=receipt.replayed,
    )


def _event_response(item: StoredEvent) -> EventResponse:
    event = item.event
    public_data = {
        key: event.data[key]
        for key in (
            "slug",
            "title",
            "revision_id",
            "parent_revision_id",
            "content_hash",
            "edit_summary",
            "summary",
            "authority",
            "review_status",
        )
        if key in event.data
    }
    return EventResponse(
        event_index=item.index,
        event_id=event.event_id,
        event_type=event.event_type,
        actor_id=event.actor_id,
        occurred_at=event.occurred_at,
        schema_version=event.schema_version,
        data=public_data,
        slug=event.data.get("slug"),
        title=event.data.get("title"),
        revision_id=event.data.get("revision_id"),
        parent_revision_id=event.data.get("parent_revision_id"),
        content_hash=event.data.get("content_hash"),
        edit_summary=event.data.get("edit_summary", event.data.get("summary")),
        committed_at=event.occurred_at
        if event.event_type in {"page_created", "revision_committed"}
        else None,
    )


def _moderation_page_response(page: Any) -> ModerationPageResponse:
    return ModerationPageResponse(
        page_id=page.page_id,
        slug=page.slug,
        title=page.title,
        content=page.content,
        head_revision_id=page.head_revision_id,
        content_hash=page.content_hash,
        head_actor_id=page.head_actor_id,
        stream_version=page.stream_version,
        moderation_status=page.moderation_status,
        updated_at=page.updated_at,
    )


def create_router(runtime: WikiRuntime | None = None) -> APIRouter:
    runtime = runtime or WikiRuntime(InMemoryWikiStore(), _default_signer())
    api = APIRouter(prefix="/api/wiki/v1", tags=["wiki"])

    def require_writes() -> None:
        if not runtime.writes_enabled:
            raise _http_error(
                WikiError(
                    "wiki_writes_disabled",
                    "community wiki writes are disabled",
                    status_code=503,
                )
            )

    def validate_origin(request: Request, *, required: bool) -> None:
        origin = request.headers.get("origin")
        if not origin:
            if required:
                raise _http_error(
                    WikiError(
                        "origin_required", "Origin header is required", status_code=403
                    )
                )
            return
        if origin not in runtime.allowed_origins:
            raise _http_error(
                WikiError(
                    "origin_forbidden", "request origin is not allowed", status_code=403
                )
            )

    async def enforce_limit(limiter: RateLimiter | None, *keys: str) -> None:
        if limiter is None:
            return
        try:
            for key in keys:
                if not await limiter.allow(key):
                    raise WikiError(
                        "rate_limited", "wiki request rate exceeded", status_code=429
                    )
        except RateLimitUnavailable as error:
            raise _http_error(
                WikiError(
                    "rate_limit_unavailable",
                    "wiki rate limiter is unavailable",
                    status_code=503,
                )
            ) from error
        except WikiError as error:
            raise _http_error(error) from error

    async def identity_for(
        request: Request, csrf: str | None, required_scope: str
    ) -> SessionIdentity:
        require_writes()
        authorization = request.headers.get("authorization", "")
        bearer = (
            authorization[7:].strip()
            if authorization.lower().startswith("bearer ")
            else None
        )
        cookie = request.cookies.get(COOKIE_NAME)
        token = bearer or cookie
        if not token:
            raise _http_error(
                WikiError(
                    "session_required", "wiki session is required", status_code=401
                )
            )
        try:
            identity = runtime.signer.verify(token, now=runtime.now())
            if required_scope not in identity.scopes:
                raise WikiError(
                    "insufficient_scope",
                    f"required scope: {required_scope}",
                    status_code=403,
                )
            if bearer:
                validate_origin(request, required=False)
            else:
                validate_origin(request, required=True)
                if not secrets.compare_digest(csrf or "", identity.csrf_token):
                    raise WikiError(
                        "csrf_failed",
                        "valid X-CSRF-Token header is required",
                        status_code=403,
                    )
            ip = client_key(request)
            await enforce_limit(
                runtime.mutation_limiter,
                f"wiki:mutation:actor:{identity.actor_id}",
                f"wiki:mutation:ip:{ip}",
            )
            return identity
        except WikiError as error:
            raise _http_error(error) from error

    async def enforce_read(request: Request, surface: str) -> None:
        await enforce_limit(
            runtime.read_limiter, f"wiki:read:{surface}:ip:{client_key(request)}"
        )

    async def page_or_404(slug: str):
        try:
            normalized = normalize_slug(slug)
        except WikiError as error:
            raise _http_error(error) from error
        page = await runtime.store.get_page_by_slug(normalized)
        if page is None:
            raise _http_error(
                WikiError("page_not_found", "the page does not exist", status_code=404)
            )
        return page

    async def execute(command: Any) -> CommandResponse:
        try:
            return _command_response(await runtime.store.execute(command))
        except WikiError as error:
            raise _http_error(error) from error

    @api.get("")
    async def index(request: Request) -> dict[str, Any]:
        await enforce_read(request, "index")
        return {
            "service": "metahumotonic-community-wiki",
            "version": "v1",
            "authority": "community",
            "canonical": False,
            "writes_enabled": runtime.writes_enabled,
            "identity_claims": "self_asserted",
            "kg_publish": "disabled-unbound-v1",
            "endpoints": {
                "pages": "/api/wiki/v1/pages",
                "recent_changes": "/api/wiki/v1/recent-changes",
                "sessions": "/api/wiki/v1/sessions",
            },
        }

    @api.post("/sessions", response_model=SessionResponse, status_code=201)
    async def issue_session(
        request: Request, response: Response, body: SessionRequest | None = None
    ) -> SessionResponse:
        require_writes()
        validate_origin(request, required=False)
        await enforce_limit(
            runtime.session_limiter, f"wiki:session:ip:{client_key(request)}"
        )
        body = body or SessionRequest()
        try:
            token, identity = runtime.signer.issue(
                actor_id=f"anonymous:{uuid.uuid4()}",
                display_name=body.display_name,
                actor_kind=body.actor_kind,
                agent_url=str(body.agent_url) if body.agent_url else None,
                now=runtime.now(),
            )
        except WikiError as error:
            raise _http_error(error) from error
        response.set_cookie(
            COOKIE_NAME,
            token,
            max_age=max(0, int((identity.expires_at - runtime.now()).total_seconds())),
            httponly=True,
            secure=runtime.secure_cookie,
            samesite="lax",
            path="/api/wiki/v1",
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        # Browser sessions authenticate with the HttpOnly cookie. Do not mirror
        # that bearer credential into JavaScript-readable response data. CLI and
        # MCP clients omit Origin and receive the token explicitly.
        exposed_token = None if request.headers.get("origin") else token
        return SessionResponse(
            actor_id=identity.actor_id,
            display_name=identity.display_name,
            actor_kind=identity.actor_kind,
            agent_url=identity.agent_url,
            access_token=exposed_token,
            bearer_token=exposed_token,
            csrf_token=identity.csrf_token,
            scopes=sorted(identity.scopes),
            expires_at=identity.expires_at,
        )

    @api.get("/pages", response_model=PageListResponse)
    async def list_pages(
        request: Request,
        q: Annotated[str | None, Query(max_length=200)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 25,
        offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
    ) -> PageListResponse:
        await enforce_read(request, "pages")
        pages = await runtime.store.list_pages(q, limit, offset)
        return PageListResponse(
            items=[_page_summary_response(page) for page in pages],
            limit=limit,
            offset=offset,
            q=q,
        )

    @api.post("/pages", response_model=CommandResponse, status_code=201)
    async def create_page(
        body: CreatePageRequest,
        request: Request,
        x_csrf_token: Annotated[str | None, Header()] = None,
        idempotency_key: Annotated[str | None, Header()] = None,
    ) -> CommandResponse:
        identity = await identity_for(request, x_csrf_token, "wiki:edit")
        now = runtime.now()
        try:
            command_id = _command_id(identity.actor_id, idempotency_key)
            slug = normalize_slug(body.slug)
        except WikiError as error:
            raise _http_error(error) from error
        return await execute(
            CreatePage(
                command_id=command_id,
                actor_id=identity.actor_id,
                scopes=identity.scopes,
                occurred_at=now,
                page_id=_stable_id(command_id, "page"),
                revision_id=_stable_id(command_id, "revision"),
                slug=slug,
                title=body.title,
                content=body.content,
                summary=body.edit_summary,
            )
        )

    @api.get("/recent-changes", response_model=EventListResponse)
    async def recent_changes(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=100)] = 25,
        offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
    ) -> EventListResponse:
        await enforce_read(request, "recent")
        events = await runtime.store.recent_changes(limit, offset)
        return EventListResponse(
            items=[_event_response(item) for item in events], limit=limit, offset=offset
        )

    @api.get("/pages/{slug}", response_model=PageResponse)
    async def get_page(slug: str, request: Request) -> PageResponse:
        await enforce_read(request, "page")
        return _page_response(await page_or_404(slug))

    async def edit_impl(
        slug: str,
        body: EditPageRequest,
        request: Request,
        x_csrf_token: str | None,
        idempotency_key: str | None,
    ) -> CommandResponse:
        identity = await identity_for(request, x_csrf_token, "wiki:edit")
        page = await page_or_404(slug)
        try:
            command_id = _command_id(identity.actor_id, idempotency_key)
        except WikiError as error:
            raise _http_error(error) from error
        return await execute(
            EditPage(
                command_id=command_id,
                actor_id=identity.actor_id,
                scopes=identity.scopes,
                occurred_at=runtime.now(),
                page_id=page.page_id,
                revision_id=_stable_id(command_id, "revision"),
                expected_revision_id=body.expected_head_revision_id,
                title=body.title if body.title is not None else page.title,
                content=body.content,
                summary=body.edit_summary,
            )
        )

    @api.post("/pages/{slug}/revisions", response_model=CommandResponse)
    async def edit_page(
        slug: str,
        body: EditPageRequest,
        request: Request,
        x_csrf_token: Annotated[str | None, Header()] = None,
        idempotency_key: Annotated[str | None, Header()] = None,
    ) -> CommandResponse:
        return await edit_impl(slug, body, request, x_csrf_token, idempotency_key)

    @api.put("/pages/{slug}", response_model=CommandResponse, include_in_schema=False)
    async def edit_page_alias(
        slug: str,
        body: EditPageRequest,
        request: Request,
        x_csrf_token: Annotated[str | None, Header()] = None,
        idempotency_key: Annotated[str | None, Header()] = None,
    ) -> CommandResponse:
        return await edit_impl(slug, body, request, x_csrf_token, idempotency_key)

    @api.get("/pages/{slug}/history", response_model=EventListResponse)
    async def history(
        slug: str,
        request: Request,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
    ) -> EventListResponse:
        await enforce_read(request, "history")
        page = await page_or_404(slug)
        events = await runtime.store.history(page.page_id, limit, offset)
        return EventListResponse(
            items=[_event_response(item) for item in events], limit=limit, offset=offset
        )

    @api.get("/pages/{slug}/diff", response_model=DiffResponse)
    async def diff(
        slug: str,
        request: Request,
        from_revision_id: Annotated[str, Query(min_length=1, max_length=128)],
        to_revision_id: Annotated[str, Query(min_length=1, max_length=128)],
    ) -> DiffResponse:
        await enforce_read(request, "diff")
        page = await page_or_404(slug)
        before = await runtime.store.get_revision(page.page_id, from_revision_id)
        after = await runtime.store.get_revision(page.page_id, to_revision_id)
        if before is None or after is None:
            raise _http_error(
                WikiError(
                    "revision_not_found",
                    "one or more revisions do not exist",
                    status_code=404,
                )
            )
        unified = "".join(
            difflib.unified_diff(
                str(before["content"]).splitlines(keepends=True),
                str(after["content"]).splitlines(keepends=True),
                fromfile=from_revision_id,
                tofile=to_revision_id,
            )
        )
        return DiffResponse(
            page_id=page.page_id,
            slug=page.slug,
            from_revision_id=from_revision_id,
            to_revision_id=to_revision_id,
            unified_diff=unified,
        )

    @api.post(
        "/pages/{slug}/submit-review", response_model=CommandResponse, status_code=202
    )
    async def submit_review(
        slug: str,
        body: SubmitReviewRequest,
        request: Request,
        x_csrf_token: Annotated[str | None, Header()] = None,
        idempotency_key: Annotated[str | None, Header()] = None,
    ) -> CommandResponse:
        identity = await identity_for(request, x_csrf_token, "wiki:submit")
        page = await page_or_404(slug)
        try:
            command_id = _command_id(identity.actor_id, idempotency_key)
        except WikiError as error:
            raise _http_error(error) from error
        return await execute(
            SubmitReview(
                command_id=command_id,
                actor_id=identity.actor_id,
                scopes=identity.scopes,
                occurred_at=runtime.now(),
                page_id=page.page_id,
                submission_id=_stable_id(command_id, "submission"),
                expected_revision_id=body.revision_id or page.head_revision_id,
                expected_content_hash=body.content_hash or page.content_hash,
                note=body.note,
            )
        )

    @api.post("/pages/{slug}/report", response_model=CommandResponse, status_code=202)
    async def report_page(
        slug: str,
        body: ReportRequest,
        request: Request,
        x_csrf_token: Annotated[str | None, Header()] = None,
        idempotency_key: Annotated[str | None, Header()] = None,
    ) -> CommandResponse:
        identity = await identity_for(request, x_csrf_token, "wiki:report")
        page = await page_or_404(slug)
        try:
            command_id = _command_id(identity.actor_id, idempotency_key)
        except WikiError as error:
            raise _http_error(error) from error
        return await execute(
            ReportPage(
                command_id=command_id,
                actor_id=identity.actor_id,
                scopes=identity.scopes,
                occurred_at=runtime.now(),
                page_id=page.page_id,
                report_id=_stable_id(command_id, "report"),
                reason=body.reason,
            )
        )

    return api


def create_moderation_router(runtime: WikiRuntime) -> APIRouter:
    """Create the opt-in operator surface; production must wire it explicitly."""

    api = APIRouter(
        prefix="/internal/wiki/moderation",
        tags=["wiki-moderation"],
        include_in_schema=False,
    )

    async def moderator_for(request: Request, supplied_key: str | None) -> str:
        if (
            not runtime.writes_enabled
            or not runtime.moderation_key
            or runtime.moderator_actor is None
        ):
            raise _http_error(
                WikiError(
                    "moderation_disabled",
                    "wiki moderation is not configured",
                    status_code=503,
                )
            )
        if not secrets.compare_digest(supplied_key or "", runtime.moderation_key):
            raise _http_error(
                WikiError(
                    "moderator_auth_failed",
                    "valid moderator credentials are required",
                    status_code=403,
                )
            )
        actor_id = runtime.moderator_actor(request).strip()
        if not actor_id:
            raise _http_error(
                WikiError(
                    "moderator_identity_unavailable",
                    "moderator identity could not be derived",
                    status_code=503,
                )
            )
        if runtime.mutation_limiter is not None:
            try:
                for key in (
                    f"wiki:moderation:actor:{actor_id}",
                    f"wiki:moderation:ip:{client_key(request)}",
                ):
                    if not await runtime.mutation_limiter.allow(key):
                        raise WikiError(
                            "rate_limited",
                            "wiki moderation rate exceeded",
                            status_code=429,
                        )
            except RateLimitUnavailable as error:
                raise _http_error(
                    WikiError(
                        "rate_limit_unavailable",
                        "wiki rate limiter is unavailable",
                        status_code=503,
                    )
                ) from error
            except WikiError as error:
                raise _http_error(error) from error
        return actor_id

    async def operator_page(slug: str):
        try:
            normalized = normalize_slug(slug)
        except WikiError as error:
            raise _http_error(error) from error
        page = await runtime.store.get_page_by_slug(
            normalized, include_quarantined=True
        )
        if page is None:
            raise _http_error(
                WikiError("page_not_found", "the page does not exist", status_code=404)
            )
        return page

    async def execute(command: Any) -> CommandResponse:
        try:
            return _command_response(await runtime.store.execute(command))
        except WikiError as error:
            raise _http_error(error) from error

    @api.get("/reports", response_model=ModerationReportListResponse)
    async def reports(
        request: Request,
        response: Response,
        limit: Annotated[int, Query(ge=1, le=100)] = 25,
        x_wiki_moderator_key: Annotated[str | None, Header()] = None,
    ) -> ModerationReportListResponse:
        await moderator_for(request, x_wiki_moderator_key)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        records = await runtime.store.pending_outbox(
            limit, effect_type="moderation_report_requested"
        )
        return ModerationReportListResponse(
            items=[
                ModerationReportResponse(
                    effect_id=record.effect.effect_id,
                    event_id=record.effect.event_id,
                    status=record.status,
                    attempts=record.attempts,
                    created_at=record.created_at,
                    payload=record.effect.payload,
                )
                for record in records
            ],
            limit=limit,
        )

    @api.post(
        "/reports/{effect_id}/resolve", response_model=ResolveReportResponse
    )
    async def resolve_report(
        effect_id: str,
        request: Request,
        x_wiki_moderator_key: Annotated[str | None, Header()] = None,
    ) -> ResolveReportResponse:
        await moderator_for(request, x_wiki_moderator_key)
        resolved = await runtime.store.resolve_outbox(
            effect_id,
            effect_type="moderation_report_requested",
            resolved_at=runtime.now(),
        )
        if not resolved:
            raise _http_error(
                WikiError(
                    "report_not_found",
                    "the moderation report does not exist",
                    status_code=404,
                )
            )
        return ResolveReportResponse(effect_id=effect_id)

    @api.get("/pages/{slug}", response_model=ModerationPageResponse)
    async def get_operator_page(
        slug: str,
        request: Request,
        response: Response,
        x_wiki_moderator_key: Annotated[str | None, Header()] = None,
    ) -> ModerationPageResponse:
        await moderator_for(request, x_wiki_moderator_key)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return _moderation_page_response(await operator_page(slug))

    async def moderation_command(
        *,
        slug: str,
        request: Request,
        supplied_key: str | None,
        idempotency_key: str | None,
        body: QuarantineRequest | ReleaseRequest,
    ) -> CommandResponse:
        actor_id = await moderator_for(request, supplied_key)
        page = await operator_page(slug)
        try:
            command_id = _command_id(actor_id, idempotency_key)
        except WikiError as error:
            raise _http_error(error) from error
        common = {
            "command_id": command_id,
            "actor_id": actor_id,
            "scopes": frozenset({"wiki:moderate"}),
            "occurred_at": runtime.now(),
            "page_id": page.page_id,
            "moderation_id": _stable_id(command_id, "moderation"),
            "expected_revision_id": body.expected_head_revision_id,
            "expected_content_hash": body.content_hash,
        }
        command = (
            QuarantinePage(**common, reason=body.reason)
            if isinstance(body, QuarantineRequest)
            else ReleasePage(**common, note=body.note)
        )
        return await execute(command)

    @api.post("/pages/{slug}/quarantine", response_model=CommandResponse)
    async def quarantine_page(
        slug: str,
        body: QuarantineRequest,
        request: Request,
        x_wiki_moderator_key: Annotated[str | None, Header()] = None,
        idempotency_key: Annotated[str | None, Header()] = None,
    ) -> CommandResponse:
        return await moderation_command(
            slug=slug,
            request=request,
            supplied_key=x_wiki_moderator_key,
            idempotency_key=idempotency_key,
            body=body,
        )

    @api.post("/pages/{slug}/release", response_model=CommandResponse)
    async def release_page(
        slug: str,
        body: ReleaseRequest,
        request: Request,
        x_wiki_moderator_key: Annotated[str | None, Header()] = None,
        idempotency_key: Annotated[str | None, Header()] = None,
    ) -> CommandResponse:
        return await moderation_command(
            slug=slug,
            request=request,
            supplied_key=x_wiki_moderator_key,
            idempotency_key=idempotency_key,
            body=body,
        )

    return api


router = create_router(
    WikiRuntime(InMemoryWikiStore(), _default_signer(), writes_enabled=False)
)
