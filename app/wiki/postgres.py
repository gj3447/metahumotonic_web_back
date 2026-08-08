"""PostgreSQL adapter with atomic events, receipt, projection, and outbox.

The repository does not yet declare a PostgreSQL driver. Production wiring may
inject any asyncpg-compatible pool; :meth:`connect` lazy-loads ``asyncpg`` so
the rest of the backend remains importable until that dependency is installed.
"""

from __future__ import annotations

import json
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import UUID

from .domain import (
    CreatePage,
    OutboxEffect,
    PageState,
    WikiCommand,
    WikiError,
    WikiEvent,
    decide,
    evolve,
)
from .memory import intent_fingerprint
from .store import CommandReceipt, OutboxRecord, StoredEvent


class PostgresWikiStore:
    def __init__(self, pool: Any) -> None:
        self._pool = pool

    @classmethod
    async def connect(cls, dsn: str, **kwargs: Any) -> PostgresWikiStore:
        try:
            import asyncpg  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - production dependency boundary
            raise RuntimeError(
                "PostgresWikiStore.connect requires the 'asyncpg' package"
            ) from exc
        return cls(await asyncpg.create_pool(dsn, **kwargs))

    async def close(self) -> None:
        await self._pool.close()

    async def ensure_schema(self, migration_dir: Path | None = None) -> None:
        """Apply each packaged migration once under a database-wide lock."""

        root = (
            migration_dir or Path(__file__).resolve().parents[2] / "migrations" / "wiki"
        )
        migrations = _migration_specs(root)
        if not migrations:
            raise RuntimeError(f"no wiki migrations found in {root}")
        async with self._pool.acquire() as connection:  # noqa: SIM117
            async with connection.transaction():
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    "metahumotonic-wiki-schema-migrations-v1",
                )
                await connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS wiki_schema_migrations (
                        version TEXT PRIMARY KEY,
                        checksum CHAR(64) NOT NULL,
                        applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                )
                # A pre-release checkout briefly created this table without a
                # checksum. Keep startup fail-closed if such an unverified row
                # exists; an operator must inspect it instead of silently stamping.
                await connection.execute(
                    "ALTER TABLE wiki_schema_migrations ADD COLUMN IF NOT EXISTS checksum CHAR(64)"
                )
                for version, checksum, sql in migrations:
                    applied = await connection.fetchrow(
                        "SELECT checksum FROM wiki_schema_migrations WHERE version=$1",
                        version,
                    )
                    if applied is not None:
                        if str(applied["checksum"] or "") != checksum:
                            raise RuntimeError(
                                f"wiki migration checksum mismatch: {version}"
                            )
                        continue
                    await connection.execute(sql)
                    await connection.execute(
                        "INSERT INTO wiki_schema_migrations (version, checksum) VALUES ($1, $2)",
                        version,
                        checksum,
                    )
                if not await _schema_is_current(connection, migrations):
                    raise RuntimeError(
                        "wiki PostgreSQL schema does not match the runtime contract"
                    )

    async def ping(self) -> bool:
        root = Path(__file__).resolve().parents[2] / "migrations" / "wiki"
        migrations = _migration_specs(root)
        if not migrations:
            return False
        async with self._pool.acquire() as connection:
            return await _schema_is_current(connection, migrations)

    async def execute(self, command: WikiCommand) -> CommandReceipt:
        fingerprint = intent_fingerprint(command)
        async with self._pool.acquire() as connection:  # noqa: SIM117
            async with connection.transaction():
                # Serializes retries carrying the same command id before checking
                # the receipt, preventing concurrent duplicates from becoming a
                # slug/CAS conflict instead of an idempotent replay.
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    command.command_id,
                )
                prior = await connection.fetchrow(
                    """
                    SELECT command_id, fingerprint, page_id, stream_version, event_ids
                    FROM wiki_command_receipts WHERE command_id = $1
                    """,
                    command.command_id,
                )
                if prior:
                    if prior["fingerprint"] != fingerprint:
                        raise WikiError(
                            "idempotency_conflict",
                            "command id was already used for different intent",
                        )
                    return CommandReceipt(
                        command_id=str(prior["command_id"]),
                        fingerprint=str(prior["fingerprint"]),
                        page_id=str(prior["page_id"]),
                        stream_version=int(prior["stream_version"]),
                        event_ids=tuple(_json_value(prior["event_ids"])),
                        replayed=True,
                    )

                if isinstance(command, CreatePage):
                    by_slug = await connection.fetchval(
                        "SELECT page_id FROM wiki_pages WHERE slug = $1 FOR UPDATE",
                        command.slug,
                    )
                    if by_slug is not None:
                        raise WikiError("slug_taken", "slug is already in use")
                    state = None
                else:
                    row = await connection.fetchrow(
                        "SELECT * FROM wiki_pages WHERE page_id = $1 FOR UPDATE",
                        _uuid(command.page_id),
                    )
                    state = _page_from_row(row) if row else None

                expected_version = state.stream_version if state else 0
                decision = decide(state, command)
                next_state = state
                for offset, event in enumerate(decision.events, start=1):
                    await connection.execute(
                        """
                        INSERT INTO wiki_events
                          (page_id, event_index, event_id, command_id, event_type,
                           schema_version, occurred_at, actor_id, data)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb)
                        """,
                        _uuid(event.page_id),
                        expected_version + offset,
                        event.event_id,
                        event.command_id,
                        event.event_type,
                        event.schema_version,
                        event.occurred_at,
                        event.actor_id,
                        json.dumps(
                            event.data, ensure_ascii=False, separators=(",", ":")
                        ),
                    )
                    next_state = evolve(next_state, event)
                if next_state is None:  # pragma: no cover
                    raise RuntimeError("accepted command produced no state")

                if isinstance(command, CreatePage):
                    try:
                        await connection.execute(
                            """
                            INSERT INTO wiki_pages
                              (page_id, slug, title, content, head_revision_id,
                               content_hash, head_actor_id, stream_version, authority,
                               review_status, moderation_status, created_at, updated_at)
                            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'community',$9,$10,$11,$12)
                            """,
                            _uuid(next_state.page_id),
                            next_state.slug,
                            next_state.title,
                            next_state.content,
                            _uuid(next_state.head_revision_id),
                            next_state.content_hash,
                            next_state.head_actor_id,
                            next_state.stream_version,
                            next_state.review_status,
                            next_state.moderation_status,
                            next_state.created_at,
                            next_state.updated_at,
                        )
                    except Exception as exc:
                        if (
                            getattr(exc, "constraint_name", None)
                            == "wiki_pages_slug_key"
                        ):
                            raise WikiError(
                                "slug_taken", "slug is already in use"
                            ) from exc
                        raise
                else:
                    status = await connection.execute(
                        """
                        UPDATE wiki_pages SET
                          title=$2, content=$3, head_revision_id=$4, content_hash=$5,
                          head_actor_id=$6, stream_version=$7, review_status=$8,
                          moderation_status=$9, updated_at=$10
                        WHERE page_id=$1 AND stream_version=$11
                        """,
                        _uuid(next_state.page_id),
                        next_state.title,
                        next_state.content,
                        _uuid(next_state.head_revision_id),
                        next_state.content_hash,
                        next_state.head_actor_id,
                        next_state.stream_version,
                        next_state.review_status,
                        next_state.moderation_status,
                        next_state.updated_at,
                        expected_version,
                    )
                    if status != "UPDATE 1":
                        raise WikiError(
                            "stream_conflict", "the page stream changed concurrently"
                        )

                for effect in decision.effects:
                    await connection.execute(
                        """
                        INSERT INTO wiki_outbox
                          (effect_id, event_id, effect_type, payload, status, created_at)
                        VALUES ($1,$2,$3,$4::jsonb,'pending',$5)
                        ON CONFLICT (effect_id) DO NOTHING
                        """,
                        effect.effect_id,
                        effect.event_id,
                        effect.effect_type,
                        json.dumps(
                            effect.payload, ensure_ascii=False, separators=(",", ":")
                        ),
                        command.occurred_at,
                    )

                event_ids = tuple(event.event_id for event in decision.events)
                await connection.execute(
                    """
                    INSERT INTO wiki_command_receipts
                      (command_id, fingerprint, page_id, stream_version, event_ids, created_at)
                    VALUES ($1,$2,$3,$4,$5::jsonb,$6)
                    """,
                    command.command_id,
                    fingerprint,
                    _uuid(command.page_id),
                    next_state.stream_version,
                    json.dumps(event_ids),
                    command.occurred_at,
                )
                return CommandReceipt(
                    command_id=command.command_id,
                    fingerprint=fingerprint,
                    page_id=command.page_id,
                    stream_version=next_state.stream_version,
                    event_ids=event_ids,
                )

    async def get_page_by_slug(
        self, slug: str, *, include_quarantined: bool = False
    ) -> PageState | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT * FROM wiki_pages
                WHERE slug=$1 AND ($2::boolean OR moderation_status='visible')
                """,
                slug,
                include_quarantined,
            )
        return _page_from_row(row) if row else None

    async def list_pages(
        self, query: str | None, limit: int, offset: int
    ) -> list[PageState]:
        async with self._pool.acquire() as connection:
            if query:
                rows = await connection.fetch(
                    """
                    SELECT * FROM wiki_pages
                    WHERE moderation_status='visible'
                      AND (slug ILIKE '%' || $1 || '%' OR title ILIKE '%' || $1 || '%')
                    ORDER BY updated_at DESC, page_id DESC LIMIT $2 OFFSET $3
                    """,
                    query,
                    limit,
                    offset,
                )
            else:
                rows = await connection.fetch(
                    """
                    SELECT * FROM wiki_pages WHERE moderation_status='visible'
                    ORDER BY updated_at DESC, page_id DESC LIMIT $1 OFFSET $2
                    """,
                    limit,
                    offset,
                )
        return [_page_from_row(row) for row in rows]

    async def history(self, page_id: str, limit: int, offset: int) -> list[StoredEvent]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT e.* FROM wiki_events e
                JOIN wiki_pages p ON p.page_id=e.page_id
                WHERE e.page_id=$1 AND p.moderation_status='visible'
                  AND e.event_type IN ('page_created','revision_committed')
                ORDER BY e.event_index DESC LIMIT $2 OFFSET $3
                """,
                _uuid(page_id),
                limit,
                offset,
            )
        return [_event_from_row(row) for row in rows]

    async def recent_changes(self, limit: int, offset: int) -> list[StoredEvent]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT e.* FROM wiki_events e
                JOIN wiki_pages p ON p.page_id=e.page_id
                WHERE p.moderation_status='visible'
                  AND e.event_type IN ('page_created','revision_committed')
                ORDER BY e.occurred_at DESC, e.event_id DESC LIMIT $1 OFFSET $2
                """,
                limit,
                offset,
            )
        return [_event_from_row(row) for row in rows]

    async def get_revision(
        self, page_id: str, revision_id: str
    ) -> dict[str, Any] | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT e.data, e.occurred_at, e.actor_id FROM wiki_events e
                JOIN wiki_pages p ON p.page_id=e.page_id
                WHERE e.page_id=$1 AND p.moderation_status='visible'
                  AND e.event_type IN ('page_created','revision_committed')
                  AND e.data->>'revision_id'=$2
                """,
                _uuid(page_id),
                revision_id,
            )
        if not row:
            return None
        data = _json_value(row["data"])
        return {
            "revision_id": revision_id,
            "title": data["title"],
            "content": data["content"],
            "content_hash": data["content_hash"],
            "occurred_at": row["occurred_at"],
            "actor_id": str(row["actor_id"]),
        }

    async def pending_outbox(
        self, limit: int = 100, *, effect_type: str | None = None
    ) -> list[OutboxRecord]:
        """Read the durable operator queue without claiming or publishing it."""

        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT effect_id, event_id, effect_type, payload, status, attempts,
                       next_attempt_at, created_at
                FROM wiki_outbox
                WHERE status IN ('pending','failed')
                  AND (next_attempt_at IS NULL OR next_attempt_at <= now())
                  AND ($2::text IS NULL OR effect_type=$2)
                ORDER BY created_at ASC, effect_id ASC
                LIMIT $1
                """,
                limit,
                effect_type,
            )
        return [
            OutboxRecord(
                effect=OutboxEffect(
                    effect_id=str(row["effect_id"]),
                    event_id=str(row["event_id"]),
                    effect_type=str(row["effect_type"]),
                    payload=dict(_json_value(row["payload"])),
                ),
                status=str(row["status"]),
                attempts=int(row["attempts"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    async def resolve_outbox(
        self, effect_id: str, *, effect_type: str, resolved_at: datetime
    ) -> bool:
        async with self._pool.acquire() as connection:
            status = await connection.execute(
                """
                UPDATE wiki_outbox
                SET status='delivered', delivered_at=COALESCE(delivered_at, $3)
                WHERE effect_id=$1 AND effect_type=$2
                  AND status IN ('pending','failed','delivered')
                """,
                effect_id,
                effect_type,
                resolved_at,
            )
        return status == "UPDATE 1"


def _json_value(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _migration_specs(root: Path) -> list[tuple[str, str, str]]:
    specs: list[tuple[str, str, str]] = []
    for migration in sorted(root.glob("[0-9][0-9][0-9]_*.sql")):
        content = migration.read_bytes()
        specs.append(
            (
                migration.stem,
                sha256(content).hexdigest(),
                content.decode("utf-8"),
            )
        )
    return specs


_REQUIRED_COLUMNS: dict[str, dict[str, tuple[str, bool, str | None]]] = {
    "wiki_schema_migrations": {
        "version": ("text", True, None),
        "checksum": ("character(64)", True, None),
        "applied_at": ("timestamp with time zone", True, "now()"),
    },
    "wiki_pages": {
        "page_id": ("uuid", True, None),
        "slug": ("text", True, None),
        "title": ("text", True, None),
        "content": ("text", True, None),
        "head_revision_id": ("uuid", True, None),
        "content_hash": ("character(64)", True, None),
        "head_actor_id": ("text", True, None),
        "stream_version": ("bigint", True, None),
        "authority": ("text", True, "'community'::text"),
        "review_status": ("text", True, "'unreviewed'::text"),
        "moderation_status": ("text", True, "'visible'::text"),
        "created_at": ("timestamp with time zone", True, None),
        "updated_at": ("timestamp with time zone", True, None),
    },
    "wiki_events": {
        "page_id": ("uuid", True, None),
        "event_index": ("bigint", True, None),
        "event_id": ("text", True, None),
        "command_id": ("text", True, None),
        "event_type": ("text", True, None),
        "schema_version": ("integer", True, None),
        "occurred_at": ("timestamp with time zone", True, None),
        "actor_id": ("text", True, None),
        "data": ("jsonb", True, None),
    },
    "wiki_command_receipts": {
        "command_id": ("text", True, None),
        "fingerprint": ("character(64)", True, None),
        "page_id": ("uuid", True, None),
        "stream_version": ("bigint", True, None),
        "event_ids": ("jsonb", True, None),
        "created_at": ("timestamp with time zone", True, None),
    },
    "wiki_outbox": {
        "effect_id": ("text", True, None),
        "event_id": ("text", True, None),
        "effect_type": ("text", True, None),
        "payload": ("jsonb", True, None),
        "status": ("text", True, "'pending'::text"),
        "attempts": ("integer", True, "0"),
        "next_attempt_at": ("timestamp with time zone", False, None),
        "created_at": ("timestamp with time zone", True, None),
        "delivered_at": ("timestamp with time zone", False, None),
    },
}

# table, type, deferrable, initially deferred, validated, canonical definition
_REQUIRED_CONSTRAINTS: dict[
    str,
    tuple[str, str, bool, bool, bool, str],
] = {
    "wiki_schema_migrations_pkey": (
        "wiki_schema_migrations",
        "p",
        False,
        False,
        True,
        "PRIMARY KEY (version)",
    ),
    "wiki_pages_pkey": (
        "wiki_pages",
        "p",
        False,
        False,
        True,
        "PRIMARY KEY (page_id)",
    ),
    "wiki_pages_slug_key": (
        "wiki_pages",
        "u",
        False,
        False,
        True,
        "UNIQUE (slug)",
    ),
    "wiki_pages_stream_version_check": (
        "wiki_pages",
        "c",
        False,
        False,
        True,
        "CHECK (stream_version > 0)",
    ),
    "wiki_pages_authority_check": (
        "wiki_pages",
        "c",
        False,
        False,
        True,
        "CHECK (authority = 'community'::text)",
    ),
    "wiki_pages_review_status_check": (
        "wiki_pages",
        "c",
        False,
        False,
        True,
        "CHECK (review_status = ANY (ARRAY['unreviewed'::text, 'submitted'::text]))",
    ),
    "wiki_pages_moderation_status_check": (
        "wiki_pages",
        "c",
        False,
        False,
        True,
        "CHECK (moderation_status = ANY (ARRAY['visible'::text, 'quarantined'::text]))",
    ),
    "wiki_pages_title_size": (
        "wiki_pages",
        "c",
        False,
        False,
        True,
        "CHECK (char_length(title) >= 1 AND char_length(title) <= 200)",
    ),
    "wiki_pages_content_size": (
        "wiki_pages",
        "c",
        False,
        False,
        True,
        "CHECK (char_length(content) >= 1 AND char_length(content) <= 100000)",
    ),
    "wiki_events_pkey": (
        "wiki_events",
        "p",
        False,
        False,
        True,
        "PRIMARY KEY (page_id, event_index)",
    ),
    "wiki_events_event_id_key": (
        "wiki_events",
        "u",
        False,
        False,
        True,
        "UNIQUE (event_id)",
    ),
    "wiki_events_event_index_check": (
        "wiki_events",
        "c",
        False,
        False,
        True,
        "CHECK (event_index > 0)",
    ),
    "wiki_events_schema_version_check": (
        "wiki_events",
        "c",
        False,
        False,
        True,
        "CHECK (schema_version = 1)",
    ),
    "wiki_events_page_id_fkey": (
        "wiki_events",
        "f",
        True,
        True,
        True,
        "FOREIGN KEY (page_id) REFERENCES wiki_pages(page_id) DEFERRABLE INITIALLY DEFERRED",
    ),
    "wiki_command_receipts_pkey": (
        "wiki_command_receipts",
        "p",
        False,
        False,
        True,
        "PRIMARY KEY (command_id)",
    ),
    "wiki_outbox_pkey": (
        "wiki_outbox",
        "p",
        False,
        False,
        True,
        "PRIMARY KEY (effect_id)",
    ),
    "wiki_outbox_effect_type_check": (
        "wiki_outbox",
        "c",
        False,
        False,
        True,
        "CHECK (effect_type = ANY (ARRAY['review_requested'::text, 'moderation_report_requested'::text]))",
    ),
    "wiki_outbox_status_check": (
        "wiki_outbox",
        "c",
        False,
        False,
        True,
        "CHECK (status = ANY (ARRAY['pending'::text, 'processing'::text, 'delivered'::text, 'failed'::text]))",
    ),
    "wiki_outbox_attempts_check": (
        "wiki_outbox",
        "c",
        False,
        False,
        True,
        "CHECK (attempts >= 0)",
    ),
}

# table, unique, valid, ready, canonical definition, predicate
_REQUIRED_INDEXES: dict[str, tuple[str, bool, bool, bool, str, str | None]] = {
    "wiki_events_recent_idx": (
        "wiki_events",
        False,
        True,
        True,
        "CREATE INDEX wiki_events_recent_idx ON wiki_events USING btree (occurred_at DESC, event_id DESC)",
        None,
    ),
    "wiki_events_revision_idx": (
        "wiki_events",
        False,
        True,
        True,
        "CREATE INDEX wiki_events_revision_idx ON wiki_events USING btree (page_id, ((data ->> 'revision_id'::text))) WHERE (event_type = ANY (ARRAY['page_created'::text, 'revision_committed'::text]))",
        "(event_type = ANY (ARRAY['page_created'::text, 'revision_committed'::text]))",
    ),
    "wiki_outbox_delivery_idx": (
        "wiki_outbox",
        False,
        True,
        True,
        "CREATE INDEX wiki_outbox_delivery_idx ON wiki_outbox USING btree (status, COALESCE(next_attempt_at, created_at), created_at) WHERE (status = ANY (ARRAY['pending'::text, 'failed'::text]))",
        "(status = ANY (ARRAY['pending'::text, 'failed'::text]))",
    ),
    "wiki_pages_public_updated_idx": (
        "wiki_pages",
        False,
        True,
        True,
        "CREATE INDEX wiki_pages_public_updated_idx ON wiki_pages USING btree (updated_at DESC, page_id DESC) WHERE (moderation_status = 'visible'::text)",
        "(moderation_status = 'visible'::text)",
    ),
}


async def _schema_is_current(
    connection: Any,
    migrations: list[tuple[str, str, str]],
) -> bool:
    try:
        columns = await connection.fetch(
            """
            SELECT cls.relname AS table_name,
                   att.attname AS column_name,
                   format_type(att.atttypid, att.atttypmod) AS data_type,
                   att.attnotnull AS not_null,
                   pg_get_expr(def.adbin, def.adrelid) AS default_expr
            FROM pg_attribute att
            JOIN pg_class cls ON cls.oid=att.attrelid
            JOIN pg_namespace ns ON ns.oid=cls.relnamespace
            LEFT JOIN pg_attrdef def
              ON def.adrelid=att.attrelid AND def.adnum=att.attnum
            WHERE ns.nspname=current_schema()
              AND cls.relname = ANY($1::text[])
              AND cls.relkind='r'
              AND att.attnum > 0
              AND NOT att.attisdropped
            """,
            list(_REQUIRED_COLUMNS),
        )
        actual_columns: dict[str, dict[str, tuple[str, bool, str | None]]] = {}
        for row in columns:
            actual_columns.setdefault(str(row["table_name"]), {})[
                str(row["column_name"])
            ] = (
                str(row["data_type"]),
                bool(row["not_null"]),
                str(row["default_expr"]) if row["default_expr"] is not None else None,
            )
        if actual_columns != _REQUIRED_COLUMNS:
            return False

        constraint_rows = await connection.fetch(
            """
            SELECT cls.relname AS table_name,
                   con.conname,
                   con.contype::text AS constraint_type,
                   con.condeferrable,
                   con.condeferred,
                   con.convalidated,
                   pg_get_constraintdef(con.oid, true) AS definition
            FROM pg_constraint con
            JOIN pg_class cls ON cls.oid=con.conrelid
            WHERE con.connamespace = current_schema()::regnamespace
              AND cls.relname = ANY($1::text[])
              AND con.contype::text <> 'n'
            """,
            list(_REQUIRED_COLUMNS),
        )
        actual_constraints = {
            str(row["conname"]): (
                str(row["table_name"]),
                str(row["constraint_type"]),
                bool(row["condeferrable"]),
                bool(row["condeferred"]),
                bool(row["convalidated"]),
                _normalize_sql_definition(row["definition"]),
            )
            for row in constraint_rows
        }
        if actual_constraints != _REQUIRED_CONSTRAINTS:
            return False

        index_rows = await connection.fetch(
            """
            SELECT tbl.relname AS table_name,
                   idx.relname AS index_name,
                   ind.indisunique,
                   ind.indisvalid,
                   ind.indisready,
                   replace(
                       pg_get_indexdef(ind.indexrelid),
                       ' ON ' || quote_ident(current_schema()) || '.',
                       ' ON '
                   ) AS definition,
                   pg_get_expr(ind.indpred, ind.indrelid) AS predicate
            FROM pg_index ind
            JOIN pg_class idx ON idx.oid=ind.indexrelid
            JOIN pg_class tbl ON tbl.oid=ind.indrelid
            JOIN pg_namespace ns ON ns.oid=tbl.relnamespace
            WHERE ns.nspname=current_schema()
              AND idx.relname = ANY($1::text[])
            """,
            list(_REQUIRED_INDEXES),
        )
        actual_indexes = {
            str(row["index_name"]): (
                str(row["table_name"]),
                bool(row["indisunique"]),
                bool(row["indisvalid"]),
                bool(row["indisready"]),
                _normalize_sql_definition(row["definition"]),
                _normalize_sql_definition(row["predicate"])
                if row["predicate"] is not None
                else None,
            )
            for row in index_rows
        }
        if actual_indexes != _REQUIRED_INDEXES:
            return False

        applied_rows = await connection.fetch(
            "SELECT version, checksum FROM wiki_schema_migrations"
        )
        applied = {
            str(row["version"]): str(row["checksum"] or "") for row in applied_rows
        }
        expected_migrations = {
            version: checksum for version, checksum, _sql in migrations
        }
        return applied == expected_migrations
    except Exception:  # noqa: BLE001 - a readiness probe must return false for any DB/schema failure
        return False


def _normalize_sql_definition(value: Any) -> str:
    return " ".join(str(value).split())


def _uuid(value: str | UUID) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def _page_from_row(row: Any) -> PageState:
    return PageState(
        page_id=str(row["page_id"]),
        slug=str(row["slug"]),
        title=str(row["title"]),
        content=str(row["content"]),
        head_revision_id=str(row["head_revision_id"]),
        content_hash=str(row["content_hash"]),
        head_actor_id=str(row["head_actor_id"]),
        stream_version=int(row["stream_version"]),
        review_status=str(row["review_status"]),
        moderation_status=str(row["moderation_status"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        authority="community",
    )


def _event_from_row(row: Any) -> StoredEvent:
    event = WikiEvent(
        event_id=str(row["event_id"]),
        command_id=str(row["command_id"]),
        page_id=str(row["page_id"]),
        event_type=str(row["event_type"]),
        occurred_at=row["occurred_at"],
        actor_id=str(row["actor_id"]),
        schema_version=int(row["schema_version"]),
        data=dict(_json_value(row["data"])),
    )
    return StoredEvent(index=int(row["event_index"]), event=event)
