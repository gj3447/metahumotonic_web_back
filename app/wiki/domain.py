"""Pure command -> event kernel for the public community wiki.

This module deliberately has no clock, UUID, database, HTTP, or KG dependency.
Those nondeterministic values are completed by the application boundary and
recorded in the command/event envelope before this kernel is called.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from hashlib import sha256
from typing import Any, Literal

Authority = Literal["community"]
ReviewStatus = Literal["unreviewed", "submitted"]
ModerationStatus = Literal["visible", "quarantined"]


class WikiError(Exception):
    """A stable domain rejection safe to map to an API response."""

    def __init__(self, code: str, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class PageState:
    page_id: str
    slug: str
    title: str
    content: str
    head_revision_id: str
    content_hash: str
    head_actor_id: str
    stream_version: int
    review_status: ReviewStatus
    moderation_status: ModerationStatus
    created_at: datetime
    updated_at: datetime
    authority: Authority = "community"


@dataclass(frozen=True, slots=True)
class CommandBase:
    command_id: str
    actor_id: str
    scopes: frozenset[str]
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class CreatePage(CommandBase):
    page_id: str
    revision_id: str
    slug: str
    title: str
    content: str
    summary: str = ""


@dataclass(frozen=True, slots=True)
class EditPage(CommandBase):
    page_id: str
    revision_id: str
    expected_revision_id: str
    title: str
    content: str
    summary: str = ""


@dataclass(frozen=True, slots=True)
class SubmitReview(CommandBase):
    page_id: str
    submission_id: str
    expected_revision_id: str
    expected_content_hash: str
    note: str = ""


@dataclass(frozen=True, slots=True)
class ReportPage(CommandBase):
    page_id: str
    report_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class QuarantinePage(CommandBase):
    page_id: str
    moderation_id: str
    expected_revision_id: str
    expected_content_hash: str
    reason: str


@dataclass(frozen=True, slots=True)
class ReleasePage(CommandBase):
    page_id: str
    moderation_id: str
    expected_revision_id: str
    expected_content_hash: str
    note: str = ""


WikiCommand = (
    CreatePage | EditPage | SubmitReview | ReportPage | QuarantinePage | ReleasePage
)


@dataclass(frozen=True, slots=True)
class WikiEvent:
    event_id: str
    command_id: str
    page_id: str
    event_type: str
    occurred_at: datetime
    actor_id: str
    schema_version: int
    data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class OutboxEffect:
    effect_id: str
    event_id: str
    effect_type: str
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Decision:
    events: tuple[WikiEvent, ...]
    effects: tuple[OutboxEffect, ...] = field(default_factory=tuple)


def content_digest(content: str) -> str:
    return sha256(content.encode("utf-8")).hexdigest()


def _require(command: WikiCommand, scope: str) -> None:
    if scope not in command.scopes:
        raise WikiError(
            "insufficient_scope", f"required scope: {scope}", status_code=403
        )


def decide(state: PageState | None, command: WikiCommand) -> Decision:
    """Decide events without I/O or hidden nondeterminism."""

    if isinstance(command, CreatePage):
        _require(command, "wiki:edit")
        if state is not None:
            raise WikiError("page_exists", "the page already exists")
        event = WikiEvent(
            event_id=command.revision_id,
            command_id=command.command_id,
            page_id=command.page_id,
            event_type="page_created",
            occurred_at=command.occurred_at,
            actor_id=command.actor_id,
            schema_version=1,
            data={
                "slug": command.slug,
                "title": command.title,
                "content": command.content,
                "content_hash": content_digest(command.content),
                "revision_id": command.revision_id,
                "summary": command.summary,
                "edit_summary": command.summary,
                "authority": "community",
                "review_status": "unreviewed",
                "moderation_status": "visible",
            },
        )
        return Decision((event,))

    if state is None:
        raise WikiError("page_not_found", "the page does not exist", status_code=404)

    if isinstance(command, EditPage):
        _require(command, "wiki:edit")
        if state.moderation_status == "quarantined":
            raise WikiError(
                "page_quarantined", "quarantined pages cannot be edited", status_code=423
            )
        if command.expected_revision_id != state.head_revision_id:
            raise WikiError("revision_conflict", "the page head has changed")
        event = WikiEvent(
            event_id=command.revision_id,
            command_id=command.command_id,
            page_id=command.page_id,
            event_type="revision_committed",
            occurred_at=command.occurred_at,
            actor_id=command.actor_id,
            schema_version=1,
            data={
                "revision_id": command.revision_id,
                "parent_revision_id": state.head_revision_id,
                "slug": state.slug,
                "title": command.title,
                "content": command.content,
                "content_hash": content_digest(command.content),
                "summary": command.summary,
                "edit_summary": command.summary,
                "review_status": "unreviewed",
            },
        )
        return Decision((event,))

    if isinstance(command, SubmitReview):
        _require(command, "wiki:submit")
        if state.moderation_status == "quarantined":
            raise WikiError(
                "page_quarantined",
                "quarantined pages cannot be submitted for review",
                status_code=423,
            )
        if command.expected_revision_id != state.head_revision_id:
            raise WikiError("revision_conflict", "the page head has changed")
        if command.expected_content_hash != state.content_hash:
            raise WikiError(
                "content_hash_conflict", "the page content hash has changed"
            )
        if state.review_status == "submitted":
            raise WikiError(
                "already_submitted", "the current revision is already submitted"
            )
        event = WikiEvent(
            event_id=command.submission_id,
            command_id=command.command_id,
            page_id=command.page_id,
            event_type="review_submitted",
            occurred_at=command.occurred_at,
            actor_id=command.actor_id,
            schema_version=1,
            data={
                "submission_id": command.submission_id,
                "revision_id": state.head_revision_id,
                "note": command.note,
                "authority": "community",
                "review_status": "submitted",
            },
        )
        effect = OutboxEffect(
            effect_id=f"review:{command.submission_id}",
            event_id=event.event_id,
            effect_type="review_requested",
            payload={
                "submission_id": command.submission_id,
                "page_id": state.page_id,
                "slug": state.slug,
                "revision_id": state.head_revision_id,
                "actor_id": command.actor_id,
                "note": command.note,
                "authority": "community",
            },
        )
        return Decision((event,), (effect,))

    if isinstance(command, ReportPage):
        _require(command, "wiki:report")
        if state.moderation_status == "quarantined":
            raise WikiError(
                "page_quarantined", "the page is not publicly reportable", status_code=404
            )
        event = WikiEvent(
            event_id=command.report_id,
            command_id=command.command_id,
            page_id=command.page_id,
            event_type="page_reported",
            occurred_at=command.occurred_at,
            actor_id=command.actor_id,
            schema_version=1,
            data={
                "report_id": command.report_id,
                "revision_id": state.head_revision_id,
                "content_hash": state.content_hash,
                "reason": command.reason,
                "authority": "community",
            },
        )
        effect = OutboxEffect(
            effect_id=f"report:{command.report_id}",
            event_id=event.event_id,
            effect_type="moderation_report_requested",
            payload={
                "report_id": command.report_id,
                "page_id": state.page_id,
                "slug": state.slug,
                "revision_id": state.head_revision_id,
                "content_hash": state.content_hash,
                "actor_id": command.actor_id,
                "reason": command.reason,
                "authority": "community",
            },
        )
        return Decision((event,), (effect,))

    if isinstance(command, (QuarantinePage, ReleasePage)):
        _require(command, "wiki:moderate")
        if command.actor_id == state.head_actor_id:
            raise WikiError(
                "moderator_is_head_actor",
                "the head revision author cannot moderate that revision",
                status_code=403,
            )
        if command.expected_revision_id != state.head_revision_id:
            raise WikiError("revision_conflict", "the page head has changed")
        if command.expected_content_hash != state.content_hash:
            raise WikiError("content_hash_conflict", "the page content hash has changed")

        if isinstance(command, QuarantinePage):
            if state.moderation_status == "quarantined":
                raise WikiError("already_quarantined", "the page is already quarantined")
            event_type = "page_quarantined"
            data = {
                "moderation_id": command.moderation_id,
                "revision_id": state.head_revision_id,
                "content_hash": state.content_hash,
                "reason": command.reason,
                "moderation_status": "quarantined",
                "authority": "community",
            }
        else:
            if state.moderation_status != "quarantined":
                raise WikiError("page_not_quarantined", "the page is not quarantined")
            event_type = "page_released"
            data = {
                "moderation_id": command.moderation_id,
                "revision_id": state.head_revision_id,
                "content_hash": state.content_hash,
                "note": command.note,
                "moderation_status": "visible",
                "authority": "community",
            }
        return Decision(
            (
                WikiEvent(
                    event_id=command.moderation_id,
                    command_id=command.command_id,
                    page_id=command.page_id,
                    event_type=event_type,
                    occurred_at=command.occurred_at,
                    actor_id=command.actor_id,
                    schema_version=1,
                    data=data,
                ),
            )
        )

    raise WikiError("unsupported_command", "unsupported wiki command", status_code=400)


def evolve(state: PageState | None, event: WikiEvent) -> PageState:
    """Fold one recorded event into the current page projection."""

    if event.event_type == "page_created":
        if state is not None:
            raise WikiError(
                "invalid_event_stream", "page_created must be the first event"
            )
        return PageState(
            page_id=event.page_id,
            slug=str(event.data["slug"]),
            title=str(event.data["title"]),
            content=str(event.data["content"]),
            head_revision_id=str(event.data["revision_id"]),
            content_hash=str(event.data["content_hash"]),
            head_actor_id=event.actor_id,
            stream_version=1,
            review_status="unreviewed",
            moderation_status="visible",
            created_at=event.occurred_at,
            updated_at=event.occurred_at,
        )
    if state is None:
        raise WikiError(
            "invalid_event_stream", "event stream does not start with page_created"
        )
    if event.event_type == "revision_committed":
        return PageState(
            page_id=state.page_id,
            slug=state.slug,
            title=str(event.data["title"]),
            content=str(event.data["content"]),
            head_revision_id=str(event.data["revision_id"]),
            content_hash=str(event.data["content_hash"]),
            head_actor_id=event.actor_id,
            stream_version=state.stream_version + 1,
            review_status="unreviewed",
            moderation_status="visible",
            created_at=state.created_at,
            updated_at=event.occurred_at,
        )
    if event.event_type == "review_submitted":
        return PageState(
            **{
                **asdict(state),
                "stream_version": state.stream_version + 1,
                "review_status": "submitted",
                "updated_at": event.occurred_at,
            }
        )
    if event.event_type == "page_reported":
        return PageState(
            **{
                **asdict(state),
                "stream_version": state.stream_version + 1,
            }
        )
    if event.event_type in {"page_quarantined", "page_released"}:
        return PageState(
            **{
                **asdict(state),
                "stream_version": state.stream_version + 1,
                "moderation_status": str(event.data["moderation_status"]),
            }
        )
    raise WikiError("unknown_event", f"unknown event type: {event.event_type}")


def replay(events: list[WikiEvent] | tuple[WikiEvent, ...]) -> PageState | None:
    state: PageState | None = None
    for event in events:
        state = evolve(state, event)
    return state


def command_intent(command: WikiCommand) -> dict[str, Any]:
    """Stable client-intent material; excludes server clock and generated event IDs."""

    common = {
        "kind": type(command).__name__,
        "actor_id": command.actor_id,
        "page_id": command.page_id,
    }
    if isinstance(command, CreatePage):
        common.update(
            slug=command.slug,
            title=command.title,
            content=command.content,
            summary=command.summary,
        )
    elif isinstance(command, EditPage):
        common.update(
            expected_revision_id=command.expected_revision_id,
            title=command.title,
            content=command.content,
            summary=command.summary,
        )
    elif isinstance(command, SubmitReview):
        common.update(
            expected_revision_id=command.expected_revision_id,
            expected_content_hash=command.expected_content_hash,
            note=command.note,
        )
    elif isinstance(command, ReportPage):
        common.update(reason=command.reason)
    elif isinstance(command, QuarantinePage):
        common.update(
            expected_revision_id=command.expected_revision_id,
            expected_content_hash=command.expected_content_hash,
            reason=command.reason,
        )
    elif isinstance(command, ReleasePage):
        common.update(
            expected_revision_id=command.expected_revision_id,
            expected_content_hash=command.expected_content_hash,
            note=command.note,
        )
    return common
