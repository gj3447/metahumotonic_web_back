"""GET /api/domains: preserve valid rows and disclose degraded data quality."""
from __future__ import annotations
from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse
from ..contracts import DomainRecord, ErrorResponse
from ..kg import kg

router = APIRouter(prefix="/api")

@router.get("/domains", response_model=list[DomainRecord],
            responses={503: {"model": ErrorResponse}})
async def get_domains(response: Response) -> list[DomainRecord] | JSONResponse:
    projection = await kg.get_domain_projection()
    headers = {
        "X-Data-Source": projection.source,
        "X-Data-Quality": projection.quality,
        "X-Records-Omitted": str(projection.omitted),
    }
    if projection.omitted or projection.source == "snapshot":
        headers["Cache-Control"] = "no-store"
    if projection.quality == "unavailable":
        return JSONResponse(status_code=503,
                            content={"reason": "domain_data_unavailable"},
                            headers=headers)
    response.headers.update(headers)
    return list(projection.items)
