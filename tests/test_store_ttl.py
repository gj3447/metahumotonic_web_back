from unittest.mock import AsyncMock

from app.config import settings
from app.store import FeedbackStore


class FakeCollection:
    def __init__(self, indexes):
        self.indexes = indexes
        self.created = []
        self.dropped = []

    def list_indexes(self):
        async def iterate():
            for index in self.indexes:
                yield index

        return iterate()

    async def create_index(self, key, **options):
        self.created.append((key, options))

    async def drop_index(self, name):
        self.dropped.append(name)


async def test_ensure_indexes_reuses_compatible_legacy_ttl(monkeypatch):
    collection = FakeCollection(
        [
            {"name": "_id_", "key": {"_id": 1}},
            {
                "name": "created_at_1",
                "key": {"created_at": 1},
                "expireAfterSeconds": 365 * 86400,
            },
        ]
    )
    feedback_store = FeedbackStore()
    monkeypatch.setattr(settings, "feedback_ttl_days", 365)
    monkeypatch.setattr(
        feedback_store, "_get_collection", AsyncMock(return_value=collection)
    )

    await feedback_store.ensure_indexes()

    assert collection.dropped == []
    assert collection.created == []


async def test_ensure_indexes_replaces_incompatible_legacy_ttl(monkeypatch):
    collection = FakeCollection(
        [
            {
                "name": "created_at_1",
                "key": {"created_at": 1},
                "expireAfterSeconds": 86400,
            }
        ]
    )
    feedback_store = FeedbackStore()
    monkeypatch.setattr(settings, "feedback_ttl_days", 365)
    monkeypatch.setattr(
        feedback_store, "_get_collection", AsyncMock(return_value=collection)
    )

    await feedback_store.ensure_indexes()

    assert collection.dropped == ["created_at_1"]
    assert collection.created == [
        (
            "created_at",
            {"name": "feedback_ttl", "expireAfterSeconds": 365 * 86400},
        )
    ]
