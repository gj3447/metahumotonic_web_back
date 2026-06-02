"""GET /api/stats — live KG counts (snapshot fallback)."""

from __future__ import annotations

from fastapi import APIRouter

from ..contracts import StatsContract
from ..kg import kg

router = APIRouter(prefix="/api")


@router.get("/stats", response_model=StatsContract)
async def get_stats() -> StatsContract:
    return await kg.get_stats()
