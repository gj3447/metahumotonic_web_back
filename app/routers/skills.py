"""GET /api/skills — curated skill canon surface (7군단장 + infra/meta)."""

from __future__ import annotations

from fastapi import APIRouter

from ..contracts import SkillRecord
from ..kg import kg

router = APIRouter(prefix="/api")


@router.get("/skills", response_model=list[SkillRecord])
async def get_skills() -> list[SkillRecord]:
    return await kg.get_skills()
