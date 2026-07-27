"""MCP registry store — MongoDB collection ``mcp_servers``, fail-soft.

Same contract as the feedback store: an empty ``MHB_MONGO_URI`` or an
unreachable Mongo raises :class:`McpRegistryUnavailable` on reads, and the
HTTP layer converts that into a ``source: "snapshot"`` empty payload — the
public API never 500s on a registry outage. Writes are CLI-only (``mhb-mcp``);
the service itself only reads.

Document shape (per server)::

    { "kind": "server", "name": ..., ...manifest entry fields...,
      "status": "verified|available|unused|unreachable",
      "verified_at": "YYYY-MM-DD" | None,      # last successful probe
      "last_probe_at": ISO-8601 | None,        # last probe attempt
      "notes": str,                            # latest verify/probe context
      "updated_at": datetime }                 # last registry write

Manifest-level fields (schema/site/notes/updated) live in one meta document
(``_id="manifest_meta"``, ``kind="meta"``) so `mhb-mcp export` can reproduce
the full ``metahumotonic/mcp-registry@1`` manifest from Mongo alone.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from .breaker import Breaker
from .config import settings

log = logging.getLogger("mhb.mcp_store")

META_ID = "manifest_meta"


class McpRegistryUnavailable(RuntimeError):
    """Raised when the registry cannot reach Mongo — callers fail soft."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


class McpRegistryStore:
    """Async registry reader. Lazily connects to Mongo; fail-soft via raise."""

    def __init__(self) -> None:
        self._client = None
        self._collection = None
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
                settings.mcp_registry_collection
            ]
            return self._collection
        except Exception as e:  # pragma: no cover - infra dependent
            self._breaker.trip()
            log.warning("mongo init failed for mcp registry: %s", e)
            return None

    async def ensure_indexes(self) -> None:
        """Unique server names (partial index — the meta doc has no name)."""
        collection = await self._get_collection()
        if collection is None:
            return
        try:
            await collection.create_index(
                "name",
                name="mcp_server_name",
                unique=True,
                partialFilterExpression={"kind": "server"},
            )
        except Exception as e:  # pragma: no cover - infra dependent
            log.warning("mcp registry index ensure failed: %s", e)

    async def _col_or_raise(self):
        collection = await self._get_collection()
        if collection is None:
            raise McpRegistryUnavailable("mongo registry unavailable")
        return collection

    async def list_servers(self) -> list[dict[str, Any]]:
        collection = await self._col_or_raise()
        try:
            cursor = collection.find(
                {"kind": "server"}, {"_id": 0, "kind": 0}
            ).sort("name", 1)
            self._breaker.reset()
            return [doc async for doc in cursor]
        except Exception as e:  # pragma: no cover - infra dependent
            self._breaker.trip()
            self._collection = None
            raise McpRegistryUnavailable("mongo registry read failed") from e

    async def get_server(self, name: str) -> dict[str, Any] | None:
        collection = await self._col_or_raise()
        try:
            self._breaker.reset()
            return await collection.find_one(
                {"kind": "server", "name": name}, {"_id": 0, "kind": 0}
            )
        except Exception as e:  # pragma: no cover - infra dependent
            self._breaker.trip()
            self._collection = None
            raise McpRegistryUnavailable("mongo registry read failed") from e

    async def manifest_meta(self) -> dict[str, Any]:
        collection = await self._col_or_raise()
        try:
            self._breaker.reset()
            doc = await collection.find_one(
                {"_id": META_ID}, {"_id": 0, "kind": 0}
            )
            return doc or {}
        except Exception as e:  # pragma: no cover - infra dependent
            self._breaker.trip()
            self._collection = None
            raise McpRegistryUnavailable("mongo registry read failed") from e

    async def health(self) -> list[dict[str, Any]]:
        """Latest verify result per server (name/status/timestamps only)."""
        collection = await self._col_or_raise()
        try:
            cursor = collection.find(
                {"kind": "server"},
                {
                    "_id": 0,
                    "name": 1,
                    "status": 1,
                    "verified_at": 1,
                    "last_probe_at": 1,
                    "notes": 1,
                },
            ).sort("name", 1)
            self._breaker.reset()
            return [doc async for doc in cursor]
        except Exception as e:  # pragma: no cover - infra dependent
            self._breaker.trip()
            self._collection = None
            raise McpRegistryUnavailable("mongo registry read failed") from e

    async def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
            self._collection = None


registry = McpRegistryStore()
