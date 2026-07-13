"""Feedback store — MongoDB when configured, in-memory otherwise.

Keeps the service runnable (and testable) with zero infra: an empty
`MHB_MONGO_URI` selects the in-memory backend.
"""

from __future__ import annotations

import logging
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any

from . import trace
from .breaker import Breaker
from .config import settings

log = logging.getLogger("mhb.store")

_TTL_INDEX = "feedback_ttl"
_MEMORY_CAP = 5000  # bounded in-memory fallback (no infra) — avoids OOM


def _now() -> datetime:
    # BSON Date (tz-aware) — required for the Mongo TTL index to expire records.
    return datetime.now(timezone.utc)


class FeedbackStore:
    """Async feedback writer. Lazily connects to Mongo; falls back in-memory."""

    def __init__(self) -> None:
        self._client = None
        self._collection = None
        self._memory: deque[dict[str, Any]] = deque(maxlen=_MEMORY_CAP)
        self._breaker = Breaker()

    async def _get_collection(self):
        if not settings.mongo_uri or self._breaker.is_open():
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
            self._breaker.trip()
            log.warning("mongo init failed, using in-memory store: %s", e)
            return None

    async def ensure_indexes(self) -> None:
        """Create the TTL index so stored feedback auto-expires (PROM16 A3S2:
        unbounded MongoDB growth). Named + conflict-handling so a changed TTL
        value actually takes effect (Mongo IndexOptionsConflict otherwise)."""
        if settings.feedback_ttl_days <= 0:
            return
        collection = await self._get_collection()
        if collection is None:
            return
        ttl = settings.feedback_ttl_days * 86400
        try:
            await collection.create_index(
                "created_at", name=_TTL_INDEX, expireAfterSeconds=ttl
            )
        except Exception as e:  # pragma: no cover - infra dependent
            # most likely IndexOptionsConflict (TTL value changed) → recreate
            try:
                await collection.drop_index(_TTL_INDEX)
                await collection.create_index(
                    "created_at", name=_TTL_INDEX, expireAfterSeconds=ttl
                )
            except Exception as e2:
                log.warning("feedback TTL index ensure failed: %s / %s", e, e2)

    async def save(self, doc: dict[str, Any]) -> str:
        record_id = uuid.uuid4().hex
        record = {"_id": record_id, "created_at": _now(), **doc}
        # ooptdd 측정: `feedback_received` is always emitted (the request was handled),
        # but `feedback_durably_stored` fires ONLY on a real Mongo insert. When Mongo
        # silently degrades to the in-memory fallback the durable event is absent — so
        # a gate reading the store back goes RED even though save() still returns an id
        # (the "green and blind" self-report ooptdd refuses to trust).
        trace.emit("feedback_received", cid=record_id, kind=doc.get("type", ""))
        collection = await self._get_collection()
        if collection is not None:
            try:
                await collection.insert_one(dict(record))
                self._breaker.reset()  # healthy again
                trace.emit("feedback_durably_stored", cid=record_id, backend="mongo")
                return record_id
            except Exception as e:  # pragma: no cover - infra dependent
                self._breaker.trip()
                self._collection = None
                log.warning("mongo insert failed, using in-memory store: %s", e)
        self._memory.append(record)
        trace.emit("feedback_stored_in_memory", cid=record_id, backend="memory")
        return record_id

    @property
    def memory(self) -> deque[dict[str, Any]]:
        """In-memory records (for the in-memory backend / tests)."""
        return self._memory

    async def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
            self._collection = None


store = FeedbackStore()
