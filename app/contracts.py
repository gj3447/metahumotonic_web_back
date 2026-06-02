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
