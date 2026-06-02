"""GET /api/domains — domain hub list (snapshot fallback)."""

from __future__ import annotations

from fastapi import APIRouter

from ..contracts import DomainRecord
from ..kg import kg

router = APIRouter(prefix="/api")


@router.get("/domains", response_model=list[DomainRecord])
async def get_domains() -> list[DomainRecord]:
    return await kg.get_domains()
