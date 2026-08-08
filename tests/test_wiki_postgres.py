"""PostgreSQL integration tests for the wiki event store.

These tests are opt-in and never use the application's production DSN. Set
``MHB_WIKI_TEST_DATABASE_URL`` to a disposable PostgreSQL database. Every test
creates a randomly named schema, pins every pooled connection to that schema,
and drops the schema afterward.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio

from app.wiki.domain import (
    CreatePage,
    EditPage,
    QuarantinePage,
    ReleasePage,
    ReportPage,
    SubmitReview,
    WikiError,
)
from app.wiki.postgres import PostgresWikiStore

TEST_DSN_ENV = "MHB_WIKI_TEST_DATABASE_URL"


@pytest_asyncio.fixture
async def postgres_store() -> AsyncIterator[PostgresWikiStore]:
    dsn = os.getenv(TEST_DSN_ENV)
    if not dsn:
        pytest.skip(f"set {TEST_DSN_ENV} to run PostgreSQL wiki integration tests")

    asyncpg = pytest.importorskip("asyncpg")
    schema = f"wiki_test_{uuid4().hex}"
    admin = await asyncpg.connect(dsn)
    store: PostgresWikiStore | None = None
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')

        store = await PostgresWikiStore.connect(
            dsn,
            min_size=1,
            max_size=6,
            # A pool release runs RESET ALL, so an init callback would lose
            # search_path after the first checkout. Startup server settings
            # remain the session defaults and survive that reset.
            server_settings={"search_path": f'"{schema}"'},
        )
        yield store
    finally:
        if store is not None:
            await store.close()
        try:
            await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        finally:
            await admin.close()


def _now(offset: int = 0) -> datetime:
    return datetime(2026, 8, 8, 12, 0, tzinfo=UTC) + timedelta(seconds=offset)


def _create(
    *,
    command_id: str | None = None,
    page_id: str | None = None,
    revision_id: str | None = None,
    slug: str = "integration-page",
    title: str = "Integration page",
    content: str = "first revision",
) -> CreatePage:
    return CreatePage(
        command_id=command_id or str(uuid4()),
        actor_id="integration-test",
        scopes=frozenset({"wiki:edit"}),
        occurred_at=_now(),
        page_id=page_id or str(uuid4()),
        revision_id=revision_id or str(uuid4()),
        slug=slug,
        title=title,
        content=content,
        summary="integration create",
    )


async def _fetchval(store: PostgresWikiStore, query: str, *args: object) -> Any:
    async with store._pool.acquire() as connection:
        return await connection.fetchval(query, *args)


@pytest.mark.asyncio
async def test_migration_is_complete_and_idempotent(
    postgres_store: PostgresWikiStore,
) -> None:
    await postgres_store.ensure_schema()
    await postgres_store.ensure_schema()

    table_names = await _fetchval(
        postgres_store,
        """
        SELECT array_agg(tablename ORDER BY tablename)
        FROM pg_tables
        WHERE schemaname = current_schema()
          AND tablename LIKE 'wiki_%'
        """,
    )
    migration_count = await _fetchval(
        postgres_store,
        "SELECT count(*) FROM wiki_schema_migrations",
    )
    checksum = await _fetchval(
        postgres_store,
        "SELECT checksum FROM wiki_schema_migrations WHERE version='001_wiki_event_store'",
    )

    assert table_names == [
        "wiki_command_receipts",
        "wiki_events",
        "wiki_outbox",
        "wiki_pages",
        "wiki_schema_migrations",
    ]
    assert migration_count == 2
    assert re.fullmatch(r"[0-9a-f]{64}", str(checksum))
    assert await postgres_store.ping() is True


@pytest.mark.asyncio
async def test_migration_checksum_tampering_fails_closed_and_can_be_restored(
    postgres_store: PostgresWikiStore,
) -> None:
    await postgres_store.ensure_schema()
    version = "001_wiki_event_store"
    original_checksum = await _fetchval(
        postgres_store,
        "SELECT checksum FROM wiki_schema_migrations WHERE version=$1",
        version,
    )
    assert re.fullmatch(r"[0-9a-f]{64}", str(original_checksum))

    async with postgres_store._pool.acquire() as connection:
        await connection.execute(
            "UPDATE wiki_schema_migrations SET checksum=$1 WHERE version=$2",
            "0" * 64,
            version,
        )

    try:
        assert await postgres_store.ping() is False
        with pytest.raises(
            RuntimeError,
            match=r"wiki migration checksum mismatch: 001_wiki_event_store",
        ):
            await postgres_store.ensure_schema()
    finally:
        # Restore the test schema even if one of the fail-closed assertions
        # changes, so teardown never leaves a deliberately corrupt fixture.
        async with postgres_store._pool.acquire() as connection:
            await connection.execute(
                "UPDATE wiki_schema_migrations SET checksum=$1 WHERE version=$2",
                original_checksum,
                version,
            )

    assert await postgres_store.ping() is True
    await postgres_store.ensure_schema()


@pytest.mark.asyncio
async def test_schema_contract_rejects_forged_fk_and_unknown_migration(
    postgres_store: PostgresWikiStore,
) -> None:
    await postgres_store.ensure_schema()

    async with postgres_store._pool.acquire() as connection:
        await connection.execute(
            "ALTER TABLE wiki_events DROP CONSTRAINT wiki_events_page_id_fkey"
        )
        await connection.execute(
            """
            ALTER TABLE wiki_events
            ADD CONSTRAINT wiki_events_page_id_fkey
            FOREIGN KEY (page_id) REFERENCES wiki_pages(page_id) NOT DEFERRABLE
            """
        )
    try:
        assert await postgres_store.ping() is False
        with pytest.raises(
            RuntimeError,
            match="wiki PostgreSQL schema does not match the runtime contract",
        ):
            await postgres_store.ensure_schema()
    finally:
        async with postgres_store._pool.acquire() as connection:
            await connection.execute(
                "ALTER TABLE wiki_events DROP CONSTRAINT wiki_events_page_id_fkey"
            )
            await connection.execute(
                """
                ALTER TABLE wiki_events
                ADD CONSTRAINT wiki_events_page_id_fkey
                FOREIGN KEY (page_id) REFERENCES wiki_pages(page_id)
                DEFERRABLE INITIALLY DEFERRED
                """
            )

    assert await postgres_store.ping() is True
    async with postgres_store._pool.acquire() as connection:
        await connection.execute(
            """
            INSERT INTO wiki_schema_migrations (version, checksum)
            VALUES ('999_unknown_future', $1)
            """,
            "f" * 64,
        )
    try:
        assert await postgres_store.ping() is False
        with pytest.raises(
            RuntimeError,
            match="wiki PostgreSQL schema does not match the runtime contract",
        ):
            await postgres_store.ensure_schema()
    finally:
        async with postgres_store._pool.acquire() as connection:
            await connection.execute(
                "DELETE FROM wiki_schema_migrations WHERE version='999_unknown_future'"
            )

    assert await postgres_store.ping() is True


@pytest.mark.asyncio
async def test_command_receipt_replay_is_idempotent(
    postgres_store: PostgresWikiStore,
) -> None:
    await postgres_store.ensure_schema()
    command = _create()

    first = await postgres_store.execute(command)
    replay = await postgres_store.execute(command)

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.command_id == first.command_id
    assert replay.event_ids == first.event_ids
    assert replay.stream_version == first.stream_version == 1
    assert await _fetchval(postgres_store, "SELECT count(*) FROM wiki_pages") == 1
    assert await _fetchval(postgres_store, "SELECT count(*) FROM wiki_events") == 1
    assert (
        await _fetchval(postgres_store, "SELECT count(*) FROM wiki_command_receipts")
        == 1
    )

    changed_intent = _create(
        command_id=command.command_id,
        page_id=command.page_id,
        revision_id=command.revision_id,
        slug=command.slug,
        content="different intent",
    )
    with pytest.raises(WikiError) as exc_info:
        await postgres_store.execute(changed_intent)
    assert exc_info.value.code == "idempotency_conflict"


@pytest.mark.asyncio
async def test_concurrent_edits_allow_exactly_one_cas_winner(
    postgres_store: PostgresWikiStore,
) -> None:
    await postgres_store.ensure_schema()
    create = _create()
    await postgres_store.execute(create)

    edits = [
        EditPage(
            command_id=str(uuid4()),
            actor_id=f"agent-{index}",
            scopes=frozenset({"wiki:edit"}),
            occurred_at=_now(index + 1),
            page_id=create.page_id,
            revision_id=str(uuid4()),
            expected_revision_id=create.revision_id,
            title=f"Concurrent edit {index}",
            content=f"candidate {index}",
            summary="CAS race",
        )
        for index in range(2)
    ]

    outcomes = await asyncio.gather(
        *(postgres_store.execute(edit) for edit in edits),
        return_exceptions=True,
    )
    accepted = [
        outcome for outcome in outcomes if not isinstance(outcome, BaseException)
    ]
    rejected = [outcome for outcome in outcomes if isinstance(outcome, WikiError)]

    assert len(accepted) == 1
    assert len(rejected) == 1
    assert rejected[0].code == "revision_conflict"

    page = await postgres_store.get_page_by_slug(create.slug)
    assert page is not None

    report = ReportPage(
        command_id=str(uuid4()),
        actor_id="anonymous:reporter",
        scopes=frozenset({"wiki:report"}),
        occurred_at=_now(19),
        page_id=page.page_id,
        report_id=str(uuid4()),
        reason="operator should inspect",
    )
    await postgres_store.execute(report)
    reports = await postgres_store.pending_outbox(
        effect_type="moderation_report_requested"
    )
    assert len(reports) == 1
    assert reports[0].effect.payload["revision_id"] == page.head_revision_id
    assert reports[0].effect.payload["content_hash"] == page.content_hash
    assert page.stream_version == 2
    assert page.head_revision_id in {edit.revision_id for edit in edits}
    after_report = await postgres_store.get_page_by_slug(create.slug)
    assert after_report is not None
    assert after_report.stream_version == 3
    assert after_report.updated_at == page.updated_at
    assert await _fetchval(postgres_store, "SELECT count(*) FROM wiki_events") == 3
    assert (
        await _fetchval(postgres_store, "SELECT count(*) FROM wiki_command_receipts")
        == 3
    )


@pytest.mark.asyncio
async def test_event_projection_receipt_and_outbox_commit_or_rollback_together(
    postgres_store: PostgresWikiStore,
) -> None:
    await postgres_store.ensure_schema()
    create = _create()
    await postgres_store.execute(create)
    initial = await postgres_store.get_page_by_slug(create.slug)
    assert initial is not None

    # A test-only constraint forces the final outbox step to fail. The event,
    # projection update, and receipt written earlier in execute() must all roll
    # back with it.
    async with postgres_store._pool.acquire() as connection:
        await connection.execute(
            """
            ALTER TABLE wiki_outbox
            ADD CONSTRAINT wiki_test_reject_review
            CHECK (effect_type <> 'review_requested')
            """
        )

    failed_submit = SubmitReview(
        command_id=str(uuid4()),
        actor_id="review-agent",
        scopes=frozenset({"wiki:submit"}),
        occurred_at=_now(10),
        page_id=create.page_id,
        submission_id=str(uuid4()),
        expected_revision_id=initial.head_revision_id,
        expected_content_hash=initial.content_hash,
        note="atomic rollback probe",
    )
    with pytest.raises(Exception) as exc_info:
        await postgres_store.execute(failed_submit)
    assert not isinstance(exc_info.value, WikiError)
    assert getattr(exc_info.value, "constraint_name", None) == "wiki_test_reject_review"

    after_failure = await postgres_store.get_page_by_slug(create.slug)
    assert after_failure is not None
    assert after_failure.review_status == "unreviewed"
    assert after_failure.stream_version == 1
    assert await _fetchval(postgres_store, "SELECT count(*) FROM wiki_events") == 1
    assert (
        await _fetchval(postgres_store, "SELECT count(*) FROM wiki_command_receipts")
        == 1
    )
    assert await _fetchval(postgres_store, "SELECT count(*) FROM wiki_outbox") == 0

    async with postgres_store._pool.acquire() as connection:
        await connection.execute(
            "ALTER TABLE wiki_outbox DROP CONSTRAINT wiki_test_reject_review"
        )

    receipt = await postgres_store.execute(failed_submit)
    final = await postgres_store.get_page_by_slug(create.slug)
    outbox = await postgres_store.pending_outbox()

    assert receipt.stream_version == 2
    assert final is not None
    assert final.review_status == "submitted"
    assert final.stream_version == 2
    assert await _fetchval(postgres_store, "SELECT count(*) FROM wiki_events") == 2
    assert (
        await _fetchval(postgres_store, "SELECT count(*) FROM wiki_command_receipts")
        == 2
    )
    assert len(outbox) == 1
    assert outbox[0].effect.effect_type == "review_requested"
    assert outbox[0].effect.event_id == failed_submit.submission_id
    assert outbox[0].effect.payload["revision_id"] == initial.head_revision_id


@pytest.mark.asyncio
async def test_quarantine_visibility_and_release_are_durable(
    postgres_store: PostgresWikiStore,
) -> None:
    await postgres_store.ensure_schema()
    create = _create()
    await postgres_store.execute(create)
    page = await postgres_store.get_page_by_slug(create.slug)
    assert page is not None

    quarantine = QuarantinePage(
        command_id=str(uuid4()),
        actor_id="moderator:integration",
        scopes=frozenset({"wiki:moderate"}),
        occurred_at=_now(20),
        page_id=page.page_id,
        moderation_id=str(uuid4()),
        expected_revision_id=page.head_revision_id,
        expected_content_hash=page.content_hash,
        reason="confirmed abuse",
    )
    quarantined_receipt = await postgres_store.execute(quarantine)
    assert quarantined_receipt.stream_version == 2
    assert await postgres_store.get_page_by_slug(create.slug) is None
    operator_page = await postgres_store.get_page_by_slug(
        create.slug, include_quarantined=True
    )
    assert operator_page is not None
    assert operator_page.moderation_status == "quarantined"
    assert await postgres_store.list_pages(None, 10, 0) == []
    assert await postgres_store.history(page.page_id, 10, 0) == []
    assert await postgres_store.recent_changes(10, 0) == []
    assert await postgres_store.get_revision(page.page_id, page.head_revision_id) is None

    release = ReleasePage(
        command_id=str(uuid4()),
        actor_id="moderator:integration",
        scopes=frozenset({"wiki:moderate"}),
        occurred_at=_now(21),
        page_id=page.page_id,
        moderation_id=str(uuid4()),
        expected_revision_id=page.head_revision_id,
        expected_content_hash=page.content_hash,
        note="false positive",
    )
    await postgres_store.execute(release)
    visible = await postgres_store.get_page_by_slug(create.slug)
    assert visible is not None
    assert visible.moderation_status == "visible"
    assert visible.stream_version == 3
