"""Concurrency-safe in-memory wiki adapter for tests and local development."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from .domain import (
    CreatePage,
    WikiCommand,
    WikiError,
    command_intent,
    decide,
    evolve,
)
from .store import CommandReceipt, OutboxRecord, StoredEvent


def intent_fingerprint(command: WikiCommand) -> str:
    material = json.dumps(
        command_intent(command),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(material.encode("utf-8")).hexdigest()


class InMemoryWikiStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._pages: dict[str, Any] = {}
        self._slug_to_id: dict[str, str] = {}
        self._events: dict[str, list[StoredEvent]] = {}
        self._receipts: dict[str, CommandReceipt] = {}
        self._outbox: dict[str, OutboxRecord] = {}

    async def execute(self, command: WikiCommand) -> CommandReceipt:
        fingerprint = intent_fingerprint(command)
        async with self._lock:
            prior = self._receipts.get(command.command_id)
            if prior is not None:
                if prior.fingerprint != fingerprint:
                    raise WikiError(
                        "idempotency_conflict",
                        "command id was already used for different intent",
                    )
                return replace(prior, replayed=True)

            if isinstance(command, CreatePage):
                if command.slug in self._slug_to_id:
                    raise WikiError("slug_taken", "slug is already in use")
                state = None
            else:
                state = self._pages.get(command.page_id)

            decision = decide(state, command)
            next_state = state
            existing = self._events.get(command.page_id, [])
            stored: list[StoredEvent] = []
            for offset, event in enumerate(decision.events, start=1):
                next_state = evolve(next_state, event)
                stored.append(StoredEvent(index=len(existing) + offset, event=event))
            if (
                next_state is None
            ):  # pragma: no cover - every accepted command emits an event
                raise RuntimeError("accepted command produced no state")

            if isinstance(command, CreatePage):
                self._slug_to_id[command.slug] = command.page_id
            self._pages[command.page_id] = next_state
            self._events.setdefault(command.page_id, []).extend(stored)
            for effect in decision.effects:
                self._outbox[effect.effect_id] = OutboxRecord(
                    effect=effect,
                    status="pending",
                    created_at=command.occurred_at.astimezone(UTC),
                )
            receipt = CommandReceipt(
                command_id=command.command_id,
                fingerprint=fingerprint,
                page_id=command.page_id,
                stream_version=next_state.stream_version,
                event_ids=tuple(event.event_id for event in decision.events),
            )
            self._receipts[command.command_id] = receipt
            return receipt

    async def get_page_by_slug(self, slug: str, *, include_quarantined: bool = False):
        async with self._lock:
            page_id = self._slug_to_id.get(slug)
            page = self._pages.get(page_id) if page_id else None
            if (
                page is not None
                and page.moderation_status == "quarantined"
                and not include_quarantined
            ):
                return None
            return page

    async def list_pages(self, query: str | None, limit: int, offset: int):
        async with self._lock:
            pages = [
                page
                for page in self._pages.values()
                if page.moderation_status == "visible"
            ]
            if query:
                needle = query.casefold()
                pages = [
                    p
                    for p in pages
                    if needle in p.slug.casefold() or needle in p.title.casefold()
                ]
            pages.sort(key=lambda p: (p.updated_at, p.page_id), reverse=True)
            return pages[offset : offset + limit]

    async def history(self, page_id: str, limit: int, offset: int):
        async with self._lock:
            page = self._pages.get(page_id)
            if page is None or page.moderation_status == "quarantined":
                return []
            events = [
                item
                for item in reversed(self._events.get(page_id, []))
                if item.event.event_type in {"page_created", "revision_committed"}
            ]
            return events[offset : offset + limit]

    async def recent_changes(self, limit: int, offset: int):
        async with self._lock:
            visible_ids = {
                page_id
                for page_id, page in self._pages.items()
                if page.moderation_status == "visible"
            }
            events = [
                item
                for page_id, stream in self._events.items()
                if page_id in visible_ids
                for item in stream
            ]
            events = [
                item
                for item in events
                if item.event.event_type in {"page_created", "revision_committed"}
            ]
            events.sort(
                key=lambda item: (item.event.occurred_at, item.event.event_id),
                reverse=True,
            )
            return events[offset : offset + limit]

    async def get_revision(
        self, page_id: str, revision_id: str
    ) -> dict[str, Any] | None:
        async with self._lock:
            page = self._pages.get(page_id)
            if page is None or page.moderation_status == "quarantined":
                return None
            for item in self._events.get(page_id, []):
                event = item.event
                if (
                    event.event_type in {"page_created", "revision_committed"}
                    and event.data.get("revision_id") == revision_id
                ):
                    return {
                        "revision_id": revision_id,
                        "title": event.data["title"],
                        "content": event.data["content"],
                        "content_hash": event.data["content_hash"],
                        "occurred_at": event.occurred_at,
                        "actor_id": event.actor_id,
                    }
            return None

    async def pending_outbox(
        self, limit: int = 100, *, effect_type: str | None = None
    ) -> list[OutboxRecord]:
        async with self._lock:
            records = [
                record
                for record in self._outbox.values()
                if record.status == "pending"
                and (effect_type is None or record.effect.effect_type == effect_type)
            ]
            return records[:limit]

    async def resolve_outbox(
        self, effect_id: str, *, effect_type: str, resolved_at: datetime
    ) -> bool:
        async with self._lock:
            record = self._outbox.get(effect_id)
            if record is None or record.effect.effect_type != effect_type:
                return False
            if record.status == "delivered":
                return True
            if record.status not in {"pending", "failed"}:
                return False
            self._outbox[effect_id] = replace(
                record,
                status="delivered",
                delivered_at=resolved_at.astimezone(UTC),
            )
            return True

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        return None
