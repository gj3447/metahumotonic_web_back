"""Storage contracts and shared records for the wiki engine."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from .domain import OutboxEffect, PageState, WikiCommand, WikiEvent


@dataclass(frozen=True, slots=True)
class CommandReceipt:
    command_id: str
    fingerprint: str
    page_id: str
    stream_version: int
    event_ids: tuple[str, ...]
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class StoredEvent:
    index: int
    event: WikiEvent


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    effect: OutboxEffect
    status: str
    created_at: datetime
    attempts: int = 0
    delivered_at: datetime | None = None


class WikiStore(Protocol):
    async def execute(self, command: WikiCommand) -> CommandReceipt: ...

    async def get_page_by_slug(
        self, slug: str, *, include_quarantined: bool = False
    ) -> PageState | None: ...

    async def list_pages(
        self, query: str | None, limit: int, offset: int
    ) -> list[PageState]: ...

    async def history(
        self, page_id: str, limit: int, offset: int
    ) -> list[StoredEvent]: ...

    async def recent_changes(self, limit: int, offset: int) -> list[StoredEvent]: ...

    async def get_revision(
        self, page_id: str, revision_id: str
    ) -> dict[str, Any] | None: ...

    async def pending_outbox(
        self, limit: int = 100, *, effect_type: str | None = None
    ) -> list[Any]: ...

    async def resolve_outbox(
        self, effect_id: str, *, effect_type: str, resolved_at: datetime
    ) -> bool: ...

    async def ping(self) -> bool: ...

    async def close(self) -> None: ...
