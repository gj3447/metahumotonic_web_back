"""POST /api/kg/read · /api/kg/write — raw Cypher proxy with key-gated R/W split.

Community Neo4j has no RBAC, so the read/write separation lives here:

  - /api/kg/read   accepts the read key OR the write key, runs the query in a
                   Neo4j READ transaction. The SERVER rejects any write attempt
                   ("Writing in read access mode not allowed") → the read key is
                   genuinely read-only even with hostile Cypher.
  - /api/kg/write  accepts only the write key, runs a WRITE transaction.

An unset key disables its endpoint (503), so the proxy is opt-in and off in CI.
Auth uses constant-time comparison; the body is parameterized (bind params),
never string-interpolated.
"""

from __future__ import annotations

import secrets

import structlog
from fastapi import APIRouter, Header, HTTPException
from neo4j.exceptions import Neo4jError

from ..config import settings
from ..contracts import CypherRequest, CypherResponse
from ..kg import KGUnavailable, kg

router = APIRouter(prefix="/api/kg")

log = structlog.get_logger("mhb.kg_proxy")


def _authorize(provided: str | None, *accepted: str) -> None:
    """Constant-time check that `provided` matches one of the configured keys.

    Raises 503 if none of the accepted keys are configured (endpoint disabled),
    401 if a key is configured but the header is missing/wrong.
    """
    configured = [k for k in accepted if k]
    if not configured:
        raise HTTPException(status_code=503, detail="kg proxy disabled (no key set)")
    candidate = provided or ""
    # compare against every accepted key in constant time; OR the results so a
    # short-circuit can't leak which key matched via timing
    ok = False
    for key in configured:
        ok |= secrets.compare_digest(candidate, key)
    if not ok:
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")


async def _execute(body: CypherRequest, *, write: bool) -> CypherResponse:
    mode = "write" if write else "read"
    try:
        rows = await kg.run_cypher(body.query, body.params, write=write)
    except KGUnavailable as e:
        log.warning("kg_proxy_query_failed", mode=mode, error=str(e))
        raise HTTPException(status_code=502, detail=f"KG unreachable: {e}") from e
    except Neo4jError as e:
        # bad Cypher, or a write attempted through the read endpoint's READ tx
        log.warning("kg_proxy_query_failed", mode=mode, error=str(e.message or e))
        raise HTTPException(status_code=400, detail=str(e.message or e)) from e
    truncated = len(rows) >= settings.kg_proxy_max_rows
    log.info("kg_proxy_query", mode=mode, rows=len(rows), truncated=truncated)
    return CypherResponse(
        rows=rows, count=len(rows),
        mode=mode, truncated=truncated,
    )


@router.post("/read", response_model=CypherResponse)
async def kg_read(
    body: CypherRequest,
    x_api_key: str | None = Header(default=None),
) -> CypherResponse:
    # write key is a superset — it can also read
    _authorize(x_api_key, settings.kg_read_key, settings.kg_write_key)
    return await _execute(body, write=False)


@router.post("/write", response_model=CypherResponse)
async def kg_write(
    body: CypherRequest,
    x_api_key: str | None = Header(default=None),
) -> CypherResponse:
    _authorize(x_api_key, settings.kg_write_key)
    return await _execute(body, write=True)
