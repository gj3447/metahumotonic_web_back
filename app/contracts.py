"""Pydantic Contracts — typed DTOs matching the front-end's `/api/*` shapes.

These mirror `metahumotonic-web/src/lib/kg.ts` + `feedback-form.js` so the
backend is a drop-in for the Astro build-time endpoints.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class StatsContract(BaseModel):
    """GET /api/stats — matches stats.json.ts fallback shape."""

    nodes: int
    rels: int
    labels: int
    relTypes: int
    domains: int
    skills: int


class DomainRecord(BaseModel):
    """One item of GET /api/domains."""

    name: str
    displayName: str
    nodeCount: int
    description: str = ""


class SkillRecord(BaseModel):
    """One item of GET /api/skills."""

    name: str
    description: str
    category: str = "methodology"


class FeedbackRequest(BaseModel):
    """POST /api/feedback body — FormData fields from feedback-form.js.

    `honeypot` must stay empty; a non-empty value marks a bot (handled silently).
    """

    type: Literal["general", "bug", "feature"] = "general"
    subject: str = Field(min_length=1, max_length=255)
    body: str = Field(min_length=1, max_length=5000)
    email: str = Field(default="", max_length=255)
    honeypot: str = Field(default="", max_length=255)
    # Cloudflare Turnstile token (only required when turnstile_secret is set)
    turnstile_token: str = Field(default="", max_length=4096)

    @field_validator("subject", "email")
    @classmethod
    def _single_line(cls, v: str) -> str:
        # single-line fields: reject CR/LF & control chars (header/log injection)
        if any(ord(ch) < 32 for ch in v):
            raise ValueError("control characters are not allowed")
        return v

    @field_validator("body")
    @classmethod
    def _body_ctrl(cls, v: str) -> str:
        # body is multi-line (textarea): allow \n \r \t, reject other control chars
        if any(ord(ch) < 32 and ch not in "\n\r\t" for ch in v):
            raise ValueError("control characters are not allowed")
        return v


class FeedbackResponse(BaseModel):
    ok: bool = True
    id: str | None = None


class ErrorResponse(BaseModel):
    """Front-end reads `err.reason` on non-2xx."""

    reason: str


# --------------------------------------------------------------------------- #
# Research surface — live view of the crystallized research body in the KG.    #
# These power /api/research/* (humans via /research page, agents via /agent).  #
# --------------------------------------------------------------------------- #


class ResearchSummary(BaseModel):
    """GET /api/research/summary — top-line counts of the living research body.

    `source` lets the client tell a live KG query ('live') apart from the
    offline fallback ('snapshot') so stale magnitudes are never shown as live.
    """

    findings: int
    lessons: int
    papers: int
    validations: int
    consensus: int
    decisions: int
    apostles: int
    domains: int
    source: Literal["live", "snapshot"] = "snapshot"


class FindingRecord(BaseModel):
    """One ResearchFinding (PROM cycle output)."""

    name: str
    finding: str
    axis: str = ""
    subAxis: str = ""
    # confidence is categorical ("HIGH"/"MEDIUM"/"LOW") OR numeric ("0.82") in the
    # KG — kept as a string so neither form is lost.
    confidence: str = ""
    cycleId: str = ""
    verified: bool | None = None
    lakatosMechanism: str = ""
    citationUrl: str = ""
    createdAt: str = ""


class LessonRecord(BaseModel):
    """One Lesson (agent feedback-loop 오답노트 — symmetric wrong/truth pair)."""

    name: str
    problem: str = ""
    solution: str = ""
    wrongAssumption: str = ""
    truth: str = ""
    category: str = ""
    severity: str = ""
    lakatosMechanism: str = ""
    createdAt: str = ""


class PaperRecord(BaseModel):
    """One Paper (source corpus / published work)."""

    title: str
    author: str = ""
    year: int | None = None
    journal: str = ""
    doi: str = ""
    domain: str = ""
    coreThesis: str = ""
    status: str = ""


class ConsensusRecord(BaseModel):
    """One Consensus node (PROM cycle convergence)."""

    name: str
    summary: str = ""
    createdAt: str = ""


class RecentItem(BaseModel):
    """One item of the unified recent-activity feed (newest research across types)."""

    type: Literal["finding", "lesson", "paper", "consensus", "validation", "decision"]
    name: str
    title: str
    summary: str = ""
    createdAt: str = ""


class GraphNeighbor(BaseModel):
    """One typed edge from a node (the connective tissue Longinus binds)."""

    direction: Literal["out", "in"]
    type: str
    name: str = ""
    labels: list[str] = []


class NodeNeighbors(BaseModel):
    """GET /api/research/neighbors — a node's living connections (capped).

    Makes any node a doorway: walk from it to its real typed neighbors instead
    of reading a flat card. `truncated` is true when degree exceeds the cap.
    """

    name: str
    found: bool
    degree: int
    neighbors: list[GraphNeighbor]
    truncated: bool = False


class AgentFeed(BaseModel):
    """GET /api/research/agent — compact, machine-readable feed for AI agents.

    The canonical doctrine lives in /llms.txt; this is the *live* layer: current
    counts + the newest findings/lessons so an agent can cite up-to-date research.
    """

    doctrine_url: str
    canonical_source: str
    generated_hint: str
    summary: ResearchSummary
    recent_findings: list[FindingRecord]
    recent_lessons: list[LessonRecord]
    how_to_cite: str
