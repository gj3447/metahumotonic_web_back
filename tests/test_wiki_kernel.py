from datetime import UTC, datetime

import pytest

from app.wiki.domain import (
    CreatePage,
    EditPage,
    QuarantinePage,
    ReleasePage,
    ReportPage,
    SubmitReview,
    WikiError,
    decide,
    evolve,
)
from app.wiki.memory import InMemoryWikiStore

NOW = datetime(2026, 8, 8, tzinfo=UTC)
SCOPES = frozenset({"wiki:read", "wiki:edit", "wiki:submit", "wiki:report"})


def create(command_id: str = "c1", content: str = "first") -> CreatePage:
    return CreatePage(
        command_id=command_id,
        actor_id="anonymous:a",
        scopes=SCOPES,
        occurred_at=NOW,
        page_id="00000000-0000-0000-0000-000000000001",
        revision_id="00000000-0000-0000-0000-000000000002",
        slug="hello-world",
        title="Hello",
        content=content,
    )


def test_decide_and_evolve_are_pure_and_explicitly_community_unreviewed():
    command = create()
    first = decide(None, command)
    second = decide(None, command)
    assert first == second
    assert first.effects == ()

    state = evolve(None, first.events[0])
    assert state.authority == "community"
    assert state.review_status == "unreviewed"
    assert state.moderation_status == "visible"
    assert state.head_actor_id == command.actor_id
    assert state.content == "first"


def test_edit_is_compare_and_swap_on_head_revision():
    state = evolve(None, decide(None, create()).events[0])
    stale = EditPage(
        command_id="c2",
        actor_id="anonymous:a",
        scopes=SCOPES,
        occurred_at=NOW,
        page_id=state.page_id,
        revision_id="r2",
        expected_revision_id="stale",
        title="Hello",
        content="second",
    )
    with pytest.raises(WikiError, match="head has changed") as raised:
        decide(state, stale)
    assert raised.value.code == "revision_conflict"


def test_report_is_visible_self_transition_and_moderation_uses_exact_head_guard():
    state = evolve(None, decide(None, create()).events[0])
    reported = ReportPage(
        command_id="report-command",
        actor_id="anonymous:reporter",
        scopes=frozenset({"wiki:report"}),
        occurred_at=NOW,
        page_id=state.page_id,
        report_id="report-1",
        reason="spam",
    )
    report_decision = decide(state, reported)
    assert report_decision.events[0].event_type == "page_reported"
    assert report_decision.events[0].data["content_hash"] == state.content_hash
    assert report_decision.effects[0].effect_type == "moderation_report_requested"
    assert report_decision.effects[0].payload["content_hash"] == state.content_hash
    after_report = evolve(state, report_decision.events[0])
    assert after_report.moderation_status == "visible"
    assert after_report.updated_at == state.updated_at

    quarantine = QuarantinePage(
        command_id="moderate-command",
        actor_id="moderator:one",
        scopes=frozenset({"wiki:moderate"}),
        occurred_at=NOW,
        page_id=state.page_id,
        moderation_id="moderation-1",
        expected_revision_id=state.head_revision_id,
        expected_content_hash=state.content_hash,
        reason="confirmed abuse",
    )
    quarantine_event = decide(after_report, quarantine).events[0]
    assert quarantine_event.event_type == "page_quarantined"
    quarantined = evolve(after_report, quarantine_event)
    assert quarantined.moderation_status == "quarantined"

    with pytest.raises(WikiError) as edit_error:
        decide(
            quarantined,
            EditPage(
                command_id="blocked-edit",
                actor_id="anonymous:b",
                scopes=frozenset({"wiki:edit"}),
                occurred_at=NOW,
                page_id=state.page_id,
                revision_id="revision-blocked",
                expected_revision_id=state.head_revision_id,
                title=state.title,
                content="must not commit",
            ),
        )
    assert edit_error.value.code == "page_quarantined"

    release = ReleasePage(
        command_id="release-command",
        actor_id="moderator:one",
        scopes=frozenset({"wiki:moderate"}),
        occurred_at=NOW,
        page_id=state.page_id,
        moderation_id="moderation-2",
        expected_revision_id=state.head_revision_id,
        expected_content_hash=state.content_hash,
    )
    release_event = decide(quarantined, release).events[0]
    assert release_event.event_type == "page_released"
    assert evolve(quarantined, release_event).moderation_status == "visible"


def test_moderator_must_be_distinct_and_match_exact_revision_and_hash():
    state = evolve(None, decide(None, create()).events[0])

    def command(**changes):
        values = {
            "command_id": "moderate",
            "actor_id": "moderator:one",
            "scopes": frozenset({"wiki:moderate"}),
            "occurred_at": NOW,
            "page_id": state.page_id,
            "moderation_id": "moderation",
            "expected_revision_id": state.head_revision_id,
            "expected_content_hash": state.content_hash,
            "reason": "abuse",
        }
        values.update(changes)
        return QuarantinePage(**values)

    for changed, expected_code in (
        ({"actor_id": state.head_actor_id}, "moderator_is_head_actor"),
        ({"expected_revision_id": "stale"}, "revision_conflict"),
        ({"expected_content_hash": "0" * 64}, "content_hash_conflict"),
        ({"scopes": frozenset({"wiki:report"})}, "insufficient_scope"),
    ):
        with pytest.raises(WikiError) as raised:
            decide(state, command(**changed))
        assert raised.value.code == expected_code


@pytest.mark.asyncio
async def test_memory_adapter_idempotency_and_review_outbox_are_atomic():
    store = InMemoryWikiStore()
    command = create()
    first = await store.execute(command)
    replay = await store.execute(command)
    assert first.replayed is False
    assert replay.replayed is True
    assert replay.event_ids == first.event_ids

    state = await store.get_page_by_slug("hello-world")
    receipt = await store.execute(
        SubmitReview(
            command_id="submit-1",
            actor_id="anonymous:a",
            scopes=SCOPES,
            occurred_at=NOW,
            page_id=state.page_id,
            submission_id="submission-1",
            expected_revision_id=state.head_revision_id,
            expected_content_hash=state.content_hash,
            note="please review",
        )
    )
    assert receipt.stream_version == 2
    outbox = await store.pending_outbox()
    assert len(outbox) == 1
    assert outbox[0].effect.effect_type == "review_requested"
    assert outbox[0].effect.payload["authority"] == "community"

    with pytest.raises(WikiError) as raised:
        await store.execute(create(content="different"))
    assert raised.value.code == "idempotency_conflict"
