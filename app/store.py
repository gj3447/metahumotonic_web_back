"""Feedback store — MongoDB when configured, in-memory otherwise.

Keeps the service runnable (and testable) with zero infra: an empty
`MHB_MONGO_URI` selects the in-memory backend.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from .config import settings

log = logging.getLogger("mhb.store")


def _now() -> datetime:
    # BSON Date (tz-aware) — required for the Mongo TTL index to expire records.
    return datetime.now(timezone.utc)


class FeedbackStore:
    """Async feedback writer. Lazily connects to Mongo; falls back in-memory."""

    def __init__(self) -> None:
        self._client = None
        self._collection = None
        self._memory: list[dict[str, Any]] = []
        self._failed = False

    async def _get_collection(self):
        if not settings.mongo_uri or self._failed:
            return None
        if self._collection is not None:
            return self._collection
        try:
            from motor.motor_asyncio import AsyncIOMotorClient

            self._client = AsyncIOMotorClient(settings.mongo_uri)
            self._collection = self._client[settings.mongo_db][
                settings.mongo_feedback_collection
            ]
            return self._collection
        except Exception as e:  # pragma: no cover - infra dependent
            self._failed = True
            log.warning("mongo init failed, using in-memory store: %s", e)
            return None

    async def ensure_indexes(self) -> None:
        """Create the TTL index so stored feedback auto-expires (PROM16 A3S2:
        unbounded MongoDB growth). No-op for the in-memory backend or ttl<=0."""
        if settings.feedback_ttl_days <= 0:
            return
        collection = await self._get_collection()
        if collection is None:
            return
        try:
            await collection.create_index(
                "created_at", expireAfterSeconds=settings.feedback_ttl_days * 86400
            )
        except Exception as e:  # pragma: no cover - infra dependent
            log.warning("feedback TTL index create failed: %s", e)

    async def save(self, doc: dict[str, Any]) -> str:
        record_id = uuid.uuid4().hex
        record = {"_id": record_id, "created_at": _now(), **doc}
        collection = await self._get_collection()
        if collection is not None:
            try:
                await collection.insert_one(dict(record))
                return record_id
            except Exception as e:  # pragma: no cover - infra dependent
                self._failed = True
                log.warning("mongo insert failed, using in-memory store: %s", e)
        self._memory.append(record)
        return record_id

    @property
    def memory(self) -> list[dict[str, Any]]:
        """In-memory records (for the in-memory backend / tests)."""
        return self._memory

    async def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
            self._collection = None


store = FeedbackStore()
