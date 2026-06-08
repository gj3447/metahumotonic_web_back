"""GET /api/research/* — live view of the crystallized research body.

Surfaces the KG's research (ResearchFinding / Lesson / Paper / Consensus, plus
ValidationResult & DecisionLog counts) so the `/research` page (humans) and the
`/agent` feed (AI agents) both read the *same live* data instead of a stale
build-time snapshot. Every path is cached + fail-soft (never 500s on KG outage).
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from ..config import settings
from ..contracts import (
    AgentFeed,
    ConsensusRecord,
    FindingRecord,
    LessonRecord,
    PaperRecord,
    RecentItem,
    ResearchSummary,
)
from ..kg import kg

router = APIRouter(prefix="/api/research")


@router.get("/summary", response_model=ResearchSummary)
async def get_summary() -> ResearchSummary:
    """Top-line counts of the living research body."""
    return await kg.get_research_summary()


@router.get("/findings", response_model=list[FindingRecord])
async def get_findings(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0, le=settings.research_max_offset),
    cycle: str = Query("", max_length=200),
) -> list[FindingRecord]:
    """Newest research findings (PROM cycle outputs), optionally filtered by cycle."""
    return await kg.get_findings(limit=limit, offset=offset, cycle=cycle)


@router.get("/lessons", response_model=list[LessonRecord])
async def get_lessons(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0, le=settings.research_max_offset),
) -> list[LessonRecord]:
    """Newest lessons (agent feedback-loop 오답노트 — wrong/truth pairs)."""
    return await kg.get_lessons(limit=limit, offset=offset)


@router.get("/papers", response_model=list[PaperRecord])
async def get_papers(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0, le=settings.research_max_offset),
    domain: str = Query("", max_length=200),
) -> list[PaperRecord]:
    """Papers (source corpus / published work), newest year first."""
    return await kg.get_papers(limit=limit, offset=offset, domain=domain)


@router.get("/consensus", response_model=list[ConsensusRecord])
async def get_consensus(
    limit: int = Query(25, ge=1, le=100),
) -> list[ConsensusRecord]:
    """PROM-cycle consensus convergences."""
    return await kg.get_consensus(limit=limit)


@router.get("/recent", response_model=list[RecentItem])
async def get_recent(
    limit: int = Query(30, ge=1, le=100),
) -> list[RecentItem]:
    """Unified newest-first activity feed across research types."""
    return await kg.get_recent(limit=limit)


@router.get("/agent", response_model=AgentFeed)
async def get_agent_feed() -> AgentFeed:
    """Compact machine-readable feed for AI agents.

    Canonical doctrine is /llms.txt; this is the *live* layer (current counts +
    newest findings/lessons) so an agent can cite up-to-date research.
    """
    summary = await kg.get_research_summary()
    findings = await kg.get_findings(limit=10)
    lessons = await kg.get_lessons(limit=10)
    return AgentFeed(
        doctrine_url="https://metahumotonic.com/llms.txt",
        canonical_source="https://metahumotonic.com/api/research/agent",
        generated_hint="live KG query, cached ~5min; re-fetch for the newest state",
        summary=summary,
        recent_findings=findings,
        recent_lessons=lessons,
        how_to_cite=(
            "Cite findings by name + cycleId and link "
            "https://metahumotonic.com/research. The body is the metahumotonic "
            "knowledge graph; /llms.txt defines the canon."
        ),
    )
