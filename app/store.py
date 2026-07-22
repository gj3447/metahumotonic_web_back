"""Feedback store — MongoDB when configured, in-memory otherwise.

Keeps the service runnable (and testable) with zero infra: an empty
`MHB_MONGO_URI` selects the in-memory backend.
"""

from __future__ import annotations

import logging
import uuid
from collections import deque
from dataclasses import dataclass
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


@dataclass(frozen=True)
class FeedbackSaveResult:
    """Storage receipt used by the HTTP layer to distinguish durable writes."""

    id: str
    durable: bool


class FeedbackStoreUnavailable(RuntimeError):
    """Raised when an operator read/write cannot prove durable Mongo access."""


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

    async def save_result(self, doc: dict[str, Any]) -> FeedbackSaveResult:
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
                return FeedbackSaveResult(id=record_id, durable=True)
            except Exception as e:  # pragma: no cover - infra dependent
                self._breaker.trip()
                self._collection = None
                log.warning("mongo insert failed, using in-memory store: %s", e)
        self._memory.append(record)
        trace.emit("feedback_stored_in_memory", cid=record_id, backend="memory")
        return FeedbackSaveResult(id=record_id, durable=False)

    async def save(self, doc: dict[str, Any]) -> str:
        """Backward-compatible id-only writer used by existing internal callers."""
        return (await self.save_result(doc)).id

    async def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return newest feedback for the authenticated operator inbox only."""
        limit = max(1, min(limit, 100))
        collection = await self._get_collection()
        if collection is not None:
            try:
                cursor = collection.find(
                    {},
                    {
                        "source_ip": 0,
                        "user_agent": 0,
                    },
                ).sort("created_at", -1).limit(limit)
                return [doc async for doc in cursor]
            except Exception as e:  # pragma: no cover - infra dependent
                self._breaker.trip()
                self._collection = None
                if settings.feedback_require_durable:
                    raise FeedbackStoreUnavailable("mongo feedback read failed") from e
                log.warning("mongo feedback read failed, using in-memory store: %s", e)
        elif settings.feedback_require_durable:
            raise FeedbackStoreUnavailable("durable feedback store unavailable")
        return list(reversed(self._memory))[:limit]

    async def triage(
        self,
        record_id: str,
        status: str,
        operator_note: str = "",
    ) -> dict[str, Any] | None:
        """Apply a bounded inbox state transition and return the updated record."""
        allowed_from = {
            "reviewed": {"new"},
            "archived": {"new", "reviewed"},
            "spam": {"new", "reviewed"},
        }[status]
        reviewed_at = _now()
        collection = await self._get_collection()
        if collection is not None:
            state_filter: dict[str, Any] = {"status": {"$in": sorted(allowed_from)}}
            if "new" in allowed_from:
                state_filter = {
                    "$or": [
                        {"status": {"$in": sorted(allowed_from)}},
                        {"status": {"$exists": False}},
                    ]
                }
            try:
                result = await collection.update_one(
                    {"_id": record_id, **state_filter},
                    {
                        "$set": {
                            "status": status,
                            "operator_note": operator_note,
                            "reviewed_at": reviewed_at,
                        }
                    },
                )
                if not result.matched_count:
                    return None
                return await collection.find_one(
                    {"_id": record_id}, {"source_ip": 0, "user_agent": 0}
                )
            except Exception as e:  # pragma: no cover - infra dependent
                self._breaker.trip()
                self._collection = None
                if settings.feedback_require_durable:
                    raise FeedbackStoreUnavailable("mongo feedback update failed") from e
                log.warning("mongo feedback update failed, using in-memory store: %s", e)
        elif settings.feedback_require_durable:
            raise FeedbackStoreUnavailable("durable feedback store unavailable")

        for record in reversed(self._memory):
            if record.get("_id") != record_id:
                continue
            current = record.get("status", "new")
            if current not in allowed_from:
                return None
            record["status"] = status
            record["operator_note"] = operator_note
            record["reviewed_at"] = reviewed_at
            return dict(record)
        return None

    async def delete(self, record_id: str) -> bool:
        """Permanently erase one feedback record after an operator decision."""
        collection = await self._get_collection()
        if collection is not None:
            try:
                result = await collection.delete_one({"_id": record_id})
                if result.deleted_count:
                    trace.emit("feedback_deleted", cid=record_id, backend="mongo")
                    return True
                return False
            except Exception as e:  # pragma: no cover - infra dependent
                self._breaker.trip()
                self._collection = None
                if settings.feedback_require_durable:
                    raise FeedbackStoreUnavailable("mongo feedback delete failed") from e
                log.warning("mongo feedback delete failed, using in-memory store: %s", e)
        elif settings.feedback_require_durable:
            raise FeedbackStoreUnavailable("durable feedback store unavailable")

        for record in tuple(self._memory):
            if record.get("_id") == record_id:
                self._memory.remove(record)
                trace.emit("feedback_deleted", cid=record_id, backend="memory")
                return True
        return False

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
